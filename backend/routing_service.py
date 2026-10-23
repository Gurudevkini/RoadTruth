import os
import json
import urllib.request
import urllib.error
from typing import Tuple, List, Dict, Any
from backend.proximity import find_incidents_near_route

# Configurable routing provider (defaults to public OpenStreetMap OSRM router)
ROUTING_PROVIDER_URL = os.environ.get(
    "ROUTING_PROVIDER_URL",
    "https://router.project-osrm.org"
).rstrip("/")

# Incident severity weights for route risk calculation
INCIDENT_SEVERITY_WEIGHTS = {
    "closure": 100.0,
    "road_closure": 100.0,
    "roadblock": 100.0,
    "accident": 35.0,
    "congestion": 25.0,
    "traffic_congestion": 25.0,
    "hazard": 20.0,
    "road_hazard": 20.0,
    "weather": 20.0,
    "weather_disruption": 20.0,
    "roadwork": 15.0,
    "construction": 15.0,
    "breakdown": 10.0,
    "vehicle_breakdown": 10.0,
    "other": 5.0
}


def calculate_route_risk(incidents: List[Dict[str, Any]]) -> Tuple[float, bool, bool]:
    """
    Computes an explainable route risk score based on proximity, severity, and verification.
    Returns: (risk_score, has_confirmed_closure, has_any_closure)
    """
    total_risk = 0.0
    has_confirmed_closure = False
    has_any_closure = False

    for inc in incidents:
        inc_type = inc["incident_type"].lower()
        weight = INCIDENT_SEVERITY_WEIGHTS.get(inc_type, 10.0)
        dist_m = inc["distance_from_route_meters"]

        # Proximity decay factor
        if dist_m <= 50.0:
            prox_factor = 1.0
        elif dist_m <= 150.0:
            prox_factor = 0.75
        elif dist_m <= 300.0:
            prox_factor = 0.50
        else:
            prox_factor = 0.25

        # Trust score and verification multiplier
        trust = inc.get("trust_score", 0.5)
        status = inc.get("verification_status", "pending")
        if status == "verified":
            verif_factor = 1.0
        elif status == "pending":
            verif_factor = 0.70
        else:  # disputed
            verif_factor = 0.20

        if inc.get("is_closure"):
            has_any_closure = True
            if inc.get("is_confirmed_closure") or status == "verified":
                has_confirmed_closure = True

        risk_contribution = weight * prox_factor * (trust * verif_factor)
        total_risk += risk_contribution

    return round(total_risk, 2), has_confirmed_closure, has_any_closure


def fetch_osrm_routes(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float, timeout_sec: float = 6.0):
    """
    Requests alternative routes from the configured OSRM routing provider.
    Returns: (is_success: bool, raw_data_or_error: dict, status_code: int)
    """
    # OSRM coordinate sequence: {lon1},{lat1};{lon2},{lat2}
    query_url = (
        f"{ROUTING_PROVIDER_URL}/route/v1/driving/"
        f"{origin_lon:.6f},{origin_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
        f"?overview=full&geometries=geojson&alternatives=true&steps=true"
    )

    headers = {
        "User-Agent": "RoadTruth-Traffic-Intelligence/2.0",
        "Accept": "application/json"
    }
    req = urllib.request.Request(query_url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            if response.status != 200:
                return False, {
                    "error": "Routing Provider Error",
                    "message": f"External routing provider returned HTTP status {response.status}."
                }, 502
            raw_text = response.read().decode("utf-8")
            data = json.loads(raw_text)
            return True, data, 200
    except (urllib.error.URLError, TimeoutError, ConnectionError) as net_err:
        return False, {
            "error": "Routing Service Unavailable",
            "message": "The external routing engine is currently unreachable or unavailable. "
                       "RoadTruth does not invent synthetic routes when the real routing engine fails.",
            "provider": ROUTING_PROVIDER_URL,
            "details": str(net_err)
        }, 503
    except Exception as exc:
        return False, {
            "error": "Routing Error",
            "message": "An unexpected error occurred while communicating with the routing engine.",
            "details": str(exc)
        }, 500


def get_alternative_routes_with_intelligence(
    origin: Dict[str, float],
    destination: Dict[str, float],
    corridor_buffer_meters: float = 200.0
) -> Tuple[bool, Dict[str, Any], int]:
    """
    Fetches real alternative routes from the routing provider, evaluates active RoadTruth incidents,
    detects confirmed roadblocks, computes risk scores, and ranks routes.
    """
    orig_lat = origin["latitude"]
    orig_lon = origin["longitude"]
    dest_lat = destination["latitude"]
    dest_lon = destination["longitude"]

    # 1. Fetch real geometries from routing provider
    success, osrm_resp, status_code = fetch_osrm_routes(orig_lat, orig_lon, dest_lat, dest_lon)
    if not success:
        return False, osrm_resp, status_code

    code = osrm_resp.get("code")
    routes_list = osrm_resp.get("routes", [])

    if code != "Ok" or not routes_list:
        return False, {
            "error": "No Route Found",
            "message": "No drivable route could be found between the specified coordinates.",
            "osrm_code": code
        }, 404

    evaluated_routes = []

    # 2. Analyze each route against RoadTruth active incidents
    for index, r in enumerate(routes_list):
        if not isinstance(r, dict):
            continue
        geometry = r.get("geometry", {})
        coords = geometry.get("coordinates", []) if isinstance(geometry, dict) else []
        dist_m = float(r.get("distance", 0.0))
        dur_s = float(r.get("duration", 0.0))

        # Identify incidents along route corridor
        corridor_incidents = find_incidents_near_route(
            coords,
            buffer_meters=corridor_buffer_meters,
            include_cleared=False
        )

        risk_score, has_confirmed_closure, has_any_closure = calculate_route_risk(corridor_incidents)

        # Extract route summary from legs if available
        legs = r.get("legs", [])
        summary_name = legs[0].get("summary", "") if legs else ""
        if not summary_name:
            summary_name = f"Route Option {index + 1}"

        evaluated_routes.append({
            "route_id": f"route_{index + 1}",
            "route_name": summary_name,
            "route_index": index,
            "distance_meters": round(dist_m, 1),
            "distance_km": round(dist_m / 1000.0, 2),
            "estimated_duration_seconds": round(dur_s, 1),
            "estimated_duration_minutes": max(1, round(dur_s / 60.0)),
            "duration_type": "routing_engine_estimate",
            "risk_score": risk_score,
            "has_confirmed_closure": has_confirmed_closure,
            "has_pending_closure": has_any_closure and not has_confirmed_closure,
            "incident_count": len(corridor_incidents),
            "relevant_incidents": corridor_incidents,
            "geometry": geometry
        })

    # 3. Rank routes: Penalize confirmed closures, then rank by combined risk and duration
    def ranking_key(item):
        closure_penalty = 100000.0 if item["has_confirmed_closure"] else 0.0
        pending_penalty = 5000.0 if item["has_pending_closure"] else 0.0
        return closure_penalty + pending_penalty + (item["risk_score"] * 2.0) + item["estimated_duration_minutes"]

    evaluated_routes.sort(key=ranking_key)

    # 4. Assign recommendation flags & explanations
    for rank, item in enumerate(evaluated_routes, start=1):
        item["rank"] = rank
        if rank == 1:
            item["is_recommended"] = True
            if item["has_confirmed_closure"]:
                item["recommendation_reason"] = (
                    "Warning: All available routes encounter road closures. This route minimizes distance."
                )
            elif item["risk_score"] == 0:
                item["recommendation_reason"] = (
                    "Recommended: Fastest and clearest route with 0 reported traffic incidents."
                )
            else:
                item["recommendation_reason"] = (
                    f"Recommended: Optimal alternative avoiding road closures (Risk score: {item['risk_score']})."
                )
        else:
            item["is_recommended"] = False
            if item["has_confirmed_closure"]:
                item["recommendation_reason"] = (
                    "Discouraged: Verified road closure or roadblock detected directly along this corridor."
                )
            else:
                item["recommendation_reason"] = (
                    f"Alternative: Higher traffic risk score ({item['risk_score']}) with "
                    f"{item['incident_count']} active incident alerts."
                )

    return True, {
        "origin": origin,
        "destination": destination,
        "corridor_buffer_meters": corridor_buffer_meters,
        "total_routes_found": len(evaluated_routes),
        "recommended_route_id": evaluated_routes[0]["route_id"] if evaluated_routes else None,
        "traffic_disclaimer": "Travel times are routing estimates based on road network speed limits. "
                              "Road closures and incident risk scores reflect live RoadTruth community and authority reports.",
        "routes": evaluated_routes
    }, 200
