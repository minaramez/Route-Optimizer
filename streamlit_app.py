import datetime
import hashlib
import hmac
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
import streamlit as st
from ortools.constraint_solver import pywrapcp, routing_enums_pb2


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------

@dataclass
class Location:
    label: str
    lat: float
    lng: float
    original_input: str


@dataclass
class AuditorStop:
    label: str
    facility: Location
    arrival_deadline_seconds: int
    pickup_earliest_seconds: int
    pickup_latest_seconds: int


@dataclass
class DailyDriverRoute:
    driver_number: int
    auditors: List[AuditorStop]
    morning_order: List[AuditorStop]
    pickup_order: List[AuditorStop]
    morning_drop_times: Dict[str, int]
    pickup_times: Dict[str, int]
    latest_morning_start_seconds: Optional[int]
    return_depot_seconds: Optional[int]
    total_driving_seconds: int
    total_distance_meters: int
    morning_google_url: str
    pickup_google_url: str


# -----------------------------------------------------------------------------
# Settings and secrets helpers
# -----------------------------------------------------------------------------

def get_secret_or_env(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name)  # type: ignore[attr-defined]
        if value is not None:
            return str(value).strip()
    except Exception:
        pass
    return os.getenv(name, default).strip()


def int_setting(name: str, default: int) -> int:
    value = get_secret_or_env(name, str(default))
    try:
        return int(value)
    except ValueError:
        return default


def float_setting(name: str, default: float) -> float:
    value = get_secret_or_env(name, str(default))
    try:
        return float(value)
    except ValueError:
        return default


ROUTING_PROVIDER = get_secret_or_env("ROUTING_PROVIDER", "osrm").lower()
OSRM_TABLE_URL = get_secret_or_env(
    "OSRM_TABLE_URL", "https://router.project-osrm.org/table/v1/driving"
).rstrip("/")
NOMINATIM_URL = get_secret_or_env(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
)
APP_USER_AGENT = get_secret_or_env(
    "APP_USER_AGENT",
    "RouteOptimizerStreamlit/1.0 (small personal routing app; contact: local-user@example.com)",
)
GEOCODE_DELAY_SECONDS = float_setting("GEOCODE_DELAY_SECONDS", 1.05)
MAX_OSRM_LOCATIONS = int_setting("MAX_OSRM_LOCATIONS", 80)
APP_STATE_VERSION = "2026-07-02-v4-auditor-round-trip"


def reset_stale_session_state() -> None:
    if st.session_state.get("_route_optimizer_state_version") == APP_STATE_VERSION:
        return
    for key in ("routes", "totals"):
        st.session_state.pop(key, None)
    st.session_state["_route_optimizer_state_version"] = APP_STATE_VERSION


# -----------------------------------------------------------------------------
# Simple password gate
# -----------------------------------------------------------------------------

def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_is_valid(candidate: str) -> bool:
    plain_password = get_secret_or_env("APP_PASSWORD", "")
    password_hash = get_secret_or_env("APP_PASSWORD_HASH", "")

    if plain_password:
        return hmac.compare_digest(candidate, plain_password)
    if password_hash:
        return hmac.compare_digest(sha256_hex(candidate), password_hash.lower())
    return True


def require_password_if_configured() -> None:
    has_password = bool(get_secret_or_env("APP_PASSWORD", "") or get_secret_or_env("APP_PASSWORD_HASH", ""))
    if not has_password:
        return

    if st.session_state.get("route_optimizer_authenticated"):
        return

    st.title("Route Optimizer")
    st.caption("This app is password protected.")

    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Enter")

    if submitted:
        if password_is_valid(password):
            st.session_state["route_optimizer_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()


# -----------------------------------------------------------------------------
# Input parsing and geocoding
# -----------------------------------------------------------------------------

@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def expand_short_url_cached(url: str, user_agent: str) -> str:
    try:
        response = requests.get(
            url,
            allow_redirects=True,
            timeout=15,
            headers={"User-Agent": user_agent},
        )
        return response.url or url
    except requests.RequestException:
        return url


def expand_short_url(url: str) -> str:
    return expand_short_url_cached(url, APP_USER_AGENT)


def extract_lat_lng_from_text(text: str) -> Optional[Tuple[float, float]]:
    text = text.strip()

    raw_match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if raw_match:
        return float(raw_match.group(1)), float(raw_match.group(2))

    # Google Maps place URLs usually store the selected place in !3dLAT!4dLNG.
    # Prefer this over @LAT,LNG because @ can be only the camera/viewport center.
    bang_match = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", text)
    if bang_match:
        return float(bang_match.group(1)), float(bang_match.group(2))

    at_match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", text)
    if at_match:
        return float(at_match.group(1)), float(at_match.group(2))

    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    for key in ("q", "query", "destination", "origin", "ll"):
        values = query.get(key, [])
        for value in values:
            param_match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value)
            if param_match:
                return float(param_match.group(1)), float(param_match.group(2))

    return None


def extract_address_from_google_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    path = unquote(parsed.path or "")

    match = re.search(r"/place/([^/]+)", path)
    if match:
        candidate = match.group(1).replace("+", " ").strip()
        if candidate:
            return candidate

    query = parse_qs(parsed.query)
    for key in ("q", "query", "destination", "origin"):
        values = query.get(key, [])
        if values:
            candidate = values[0].replace("+", " ").strip()
            if candidate and not re.fullmatch(r"-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?", candidate):
                return candidate

    return None


@st.cache_data(ttl=7 * 24 * 60 * 60, show_spinner=False)
def geocode_address_cached(address: str, nominatim_url: str, user_agent: str) -> Tuple[float, float, str]:
    response = requests.get(
        nominatim_url,
        params={"q": address, "format": "jsonv2", "limit": 1},
        timeout=25,
        headers={"User-Agent": user_agent},
    )
    response.raise_for_status()
    data = response.json()

    if not data:
        raise ValueError(
            f"Could not geocode '{address}' using free OpenStreetMap/Nominatim. Try pasting coordinates instead."
        )

    result = data[0]
    formatted = result.get("display_name", address)
    return float(result["lat"]), float(result["lon"]), formatted


_last_uncached_geocode_at = 0.0


def geocode_address(address: str) -> Tuple[float, float, str]:
    global _last_uncached_geocode_at
    elapsed = time.monotonic() - _last_uncached_geocode_at
    if elapsed < GEOCODE_DELAY_SECONDS:
        time.sleep(GEOCODE_DELAY_SECONDS - elapsed)

    value = geocode_address_cached(address.strip(), NOMINATIM_URL, APP_USER_AGENT)
    _last_uncached_geocode_at = time.monotonic()
    return value


def parse_location(value: str, index: int, label_prefix: str = "Stop") -> Location:
    original = value.strip()
    if not original:
        raise ValueError("Empty location line")

    candidate = original
    if "maps.app.goo.gl" in candidate or "goo.gl/maps" in candidate:
        candidate = expand_short_url(candidate)

    coords = extract_lat_lng_from_text(candidate)
    if coords:
        lat, lng = coords
        return Location(label=f"{label_prefix} {index}", lat=lat, lng=lng, original_input=original)

    if candidate.startswith("http://") or candidate.startswith("https://"):
        address = extract_address_from_google_url(candidate)
        if not address:
            raise ValueError(
                "Could not find coordinates or a place name in this Google Maps link. "
                "Open the place in Google Maps and copy a link that contains @lat,lng, "
                "or paste coordinates/address instead."
            )
    else:
        address = candidate

    lat, lng, formatted = geocode_address(address)
    return Location(label=formatted, lat=lat, lng=lng, original_input=original)


# -----------------------------------------------------------------------------
# Matrix building
# -----------------------------------------------------------------------------

def haversine_meters(a: Location, b: Location) -> int:
    radius = 6371000
    lat1, lon1 = math.radians(a.lat), math.radians(a.lng)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lng)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return int(2 * radius * math.asin(math.sqrt(h)))


def build_haversine_matrix(locations: List[Location]) -> Tuple[List[List[int]], List[List[int]]]:
    n = len(locations)
    time_matrix = [[0 for _ in range(n)] for _ in range(n)]
    distance_matrix = [[0 for _ in range(n)] for _ in range(n)]
    avg_speed_mps = 35_000 / 3600

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            meters = haversine_meters(locations[i], locations[j])
            distance_matrix[i][j] = meters
            time_matrix[i][j] = max(1, int(meters / avg_speed_mps))

    return time_matrix, distance_matrix


@st.cache_data(ttl=60 * 60, show_spinner=False)
def compute_osrm_route_matrix_cached(
    coords: Tuple[Tuple[float, float], ...],
    osrm_table_url: str,
    user_agent: str,
    max_osrm_locations: int,
) -> Tuple[List[List[int]], List[List[int]]]:
    if len(coords) > max_osrm_locations:
        raise ValueError(
            f"Too many unique locations for the public OSRM server ({len(coords)} > {max_osrm_locations}). "
            "Use fewer auditors, set ROUTING_PROVIDER=haversine, or self-host OSRM."
        )

    coord_string = ";".join(f"{lng:.7f},{lat:.7f}" for lat, lng in coords)
    url = f"{osrm_table_url}/{coord_string}"
    response = requests.get(
        url,
        params={"annotations": "duration,distance", "fallback_speed": "35"},
        timeout=60,
        headers={"User-Agent": user_agent},
    )
    response.raise_for_status()
    data = response.json()

    if data.get("code") != "Ok":
        raise ValueError(f"OSRM matrix failed: {data.get('code')} {data.get('message', '')}")

    durations = data.get("durations")
    distances = data.get("distances")
    if not durations:
        raise ValueError("OSRM did not return a duration matrix.")

    temp_locations = [Location(label=str(i), lat=lat, lng=lng, original_input="") for i, (lat, lng) in enumerate(coords)]
    fallback_distance = build_haversine_matrix(temp_locations)[1]

    n = len(coords)
    time_matrix = [[0 for _ in range(n)] for _ in range(n)]
    distance_matrix = [[0 for _ in range(n)] for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            duration = durations[i][j] if i < len(durations) and j < len(durations[i]) else None
            distance = distances[i][j] if distances and i < len(distances) and j < len(distances[i]) else None
            if duration is None:
                meters = fallback_distance[i][j]
                time_matrix[i][j] = max(1, int(meters / (35_000 / 3600)))
                distance_matrix[i][j] = meters
            else:
                time_matrix[i][j] = max(1, int(duration))
                distance_matrix[i][j] = int(distance) if distance is not None else fallback_distance[i][j]

    return time_matrix, distance_matrix


def compute_osrm_route_matrix(locations: List[Location]) -> Tuple[List[List[int]], List[List[int]]]:
    coords = tuple((loc.lat, loc.lng) for loc in locations)
    return compute_osrm_route_matrix_cached(coords, OSRM_TABLE_URL, APP_USER_AGENT, MAX_OSRM_LOCATIONS)


def compute_route_matrix(locations: List[Location]) -> Tuple[List[List[int]], List[List[int]]]:
    if ROUTING_PROVIDER == "haversine":
        return build_haversine_matrix(locations)
    if ROUTING_PROVIDER == "osrm":
        return compute_osrm_route_matrix(locations)
    raise ValueError("Invalid ROUTING_PROVIDER. Use 'osrm' or 'haversine'.")


def expand_facility_matrix_for_drop_pickup(
    base_time_matrix: List[List[int]],
    base_distance_matrix: List[List[int]],
    stop_count: int,
) -> Tuple[List[List[int]], List[List[int]], List[int]]:
    """Expand depot+facilities matrix into depot+drop nodes+pickup nodes.

    Node 0 is depot.
    Nodes 1..N are morning drop-off nodes.
    Nodes N+1..2N are afternoon pickup nodes at the same facility coordinates.
    """
    base_index_by_node = [0] + list(range(1, stop_count + 1)) + list(range(1, stop_count + 1))
    total_nodes = 1 + 2 * stop_count
    time_matrix = [[0 for _ in range(total_nodes)] for _ in range(total_nodes)]
    distance_matrix = [[0 for _ in range(total_nodes)] for _ in range(total_nodes)]

    for i in range(total_nodes):
        for j in range(total_nodes):
            bi = base_index_by_node[i]
            bj = base_index_by_node[j]
            time_matrix[i][j] = base_time_matrix[bi][bj]
            distance_matrix[i][j] = base_distance_matrix[bi][bj]

    return time_matrix, distance_matrix, base_index_by_node


# -----------------------------------------------------------------------------
# Formatting and links
# -----------------------------------------------------------------------------

def seconds_from_time(value: datetime.time) -> int:
    return value.hour * 3600 + value.minute * 60


def format_clock(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    day_offset = math.floor(seconds / 86400)
    seconds_in_day = seconds % 86400
    hours = seconds_in_day // 3600
    minutes = (seconds_in_day % 3600) // 60
    suffix = ""
    if day_offset == -1:
        suffix = " previous day"
    elif day_offset < -1:
        suffix = f" {abs(day_offset)} days earlier"
    elif day_offset == 1:
        suffix = " next day"
    elif day_offset > 1:
        suffix = f" {day_offset} days later"
    return f"{hours:02d}:{minutes:02d}{suffix}"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_distance(meters: int) -> str:
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{meters} m"


def build_google_maps_directions_url(points: List[Location]) -> str:
    if len(points) < 2:
        return ""

    def coord(loc: Location) -> str:
        return f"{loc.lat},{loc.lng}"

    origin = coord(points[0])
    destination = coord(points[-1])
    waypoints = [coord(point) for point in points[1:-1]]
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(destination)}"
        "&travelmode=driving"
    )
    if waypoints:
        url += "&waypoints=" + quote_plus("|".join(waypoints))
    return url


# -----------------------------------------------------------------------------
# Optimization
# -----------------------------------------------------------------------------

def solve_daily_auditor_routes(
    depot: Location,
    auditors: List[AuditorStop],
    driver_count: int,
    require_all_drivers_if_possible: bool = True,
    solver_seconds: int = 30,
) -> Tuple[List[DailyDriverRoute], Dict[str, Optional[int]]]:
    if driver_count < 1:
        raise ValueError("Driver count must be at least 1.")
    if not auditors:
        raise ValueError("Add at least one auditor/facility.")

    # Build one OSRM matrix for unique physical points only, then duplicate each facility
    # into a morning drop-off node and an afternoon pickup node.
    physical_locations = [depot] + [auditor.facility for auditor in auditors]
    base_time_matrix, base_distance_matrix = compute_route_matrix(physical_locations)
    time_matrix, distance_matrix, _ = expand_facility_matrix_for_drop_pickup(
        base_time_matrix, base_distance_matrix, len(auditors)
    )

    n = len(auditors)
    total_nodes = 1 + 2 * n
    depot_node = 0
    drop_node_by_auditor = {i: 1 + i for i in range(n)}
    pickup_node_by_auditor = {i: 1 + n + i for i in range(n)}
    auditor_index_by_node = {1 + i: i for i in range(n)}
    auditor_index_by_node.update({1 + n + i: i for i in range(n)})

    manager = pywrapcp.RoutingIndexManager(total_nodes, driver_count, depot_node)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(time_matrix[from_node][to_node])

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    # Enough slack for the driver to wait between morning drop-offs and afternoon pickups.
    horizon_seconds = 24 * 3600
    routing.AddDimension(
        transit_callback_index,
        horizon_seconds,   # allow waiting
        horizon_seconds,   # daily horizon
        False,             # flexible start time; we compute/display latest feasible morning start
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    solver = routing.solver()

    # Time windows:
    # - drop-off nodes must be reached by each auditor's audit start time
    # - pickup nodes cannot be before the auditor can leave, and should not be after latest pickup
    # - soft upper bound at pickup earliest makes the solver reduce "auditor waiting after work"
    pickup_lateness_penalty_per_second = 10_000
    for i, auditor in enumerate(auditors):
        drop_node = drop_node_by_auditor[i]
        pickup_node = pickup_node_by_auditor[i]
        drop_index = manager.NodeToIndex(drop_node)
        pickup_index = manager.NodeToIndex(pickup_node)

        time_dimension.CumulVar(drop_index).SetRange(0, auditor.arrival_deadline_seconds)
        time_dimension.CumulVar(pickup_index).SetRange(
            auditor.pickup_earliest_seconds,
            auditor.pickup_latest_seconds,
        )
        time_dimension.SetCumulVarSoftUpperBound(
            pickup_index,
            auditor.pickup_earliest_seconds,
            pickup_lateness_penalty_per_second,
        )

        # Same driver must drop off and pick up the same auditor.
        solver.Add(routing.VehicleVar(drop_index) == routing.VehicleVar(pickup_index))
        # Pickup must happen after drop-off.
        solver.Add(time_dimension.CumulVar(drop_index) <= time_dimension.CumulVar(pickup_index))

    for vehicle_id in range(driver_count):
        time_dimension.CumulVar(routing.Start(vehicle_id)).SetRange(0, 12 * 3600)
        time_dimension.CumulVar(routing.End(vehicle_id)).SetRange(0, horizon_seconds)

    # Encourage actual use of all drivers where it makes sense. This matters because otherwise
    # the solver can leave one driver unused and make auditors wait for one long shared route.
    if require_all_drivers_if_possible:
        vehicles_to_force = min(driver_count, n)
        for vehicle_id in range(vehicles_to_force):
            solver.Add(routing.NextVar(routing.Start(vehicle_id)) != routing.End(vehicle_id))

    # Tie-breakers: keep the day compact and reduce unnecessary driving after satisfying the
    # pickup-waiting objective.
    time_dimension.SetGlobalSpanCostCoefficient(10)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = solver_seconds

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        raise ValueError(
            "No feasible schedule found. Try increasing the pickup latest time, using more drivers, "
            "or checking whether the facilities are too far apart for the selected time windows."
        )

    routes: List[DailyDriverRoute] = []
    for vehicle_id in range(driver_count):
        index = routing.Start(vehicle_id)
        route_nodes: List[int] = []
        route_seconds = 0
        route_meters = 0
        previous_node: Optional[int] = None

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if previous_node is not None:
                route_seconds += time_matrix[previous_node][node]
                route_meters += distance_matrix[previous_node][node]
            if node != depot_node:
                route_nodes.append(node)
            previous_node = node
            index = solution.Value(routing.NextVar(index))

        end_node = manager.IndexToNode(index)
        end_time = solution.Value(time_dimension.CumulVar(index))
        if previous_node is not None:
            route_seconds += time_matrix[previous_node][end_node]
            route_meters += distance_matrix[previous_node][end_node]

        morning_nodes = [node for node in route_nodes if 1 <= node <= n]
        pickup_nodes = [node for node in route_nodes if n + 1 <= node <= 2 * n]
        morning_order = [auditors[auditor_index_by_node[node]] for node in morning_nodes]
        pickup_order = [auditors[auditor_index_by_node[node]] for node in pickup_nodes]
        assigned_indices = sorted({auditor_index_by_node[node] for node in route_nodes})
        assigned_auditors = [auditors[i] for i in assigned_indices]

        # Compute latest possible morning start from depot while still meeting every drop-off deadline.
        latest_start: Optional[int] = None
        morning_drop_times: Dict[str, int] = {}
        if morning_nodes:
            offsets: List[Tuple[int, int]] = []
            elapsed = 0
            prev = depot_node
            for node in morning_nodes:
                elapsed += time_matrix[prev][node]
                offsets.append((node, elapsed))
                prev = node
            latest_start = min(
                auditors[auditor_index_by_node[node]].arrival_deadline_seconds - offset
                for node, offset in offsets
            )
            for node, offset in offsets:
                auditor = auditors[auditor_index_by_node[node]]
                morning_drop_times[auditor.label] = latest_start + offset

        pickup_times: Dict[str, int] = {}
        for node in pickup_nodes:
            auditor = auditors[auditor_index_by_node[node]]
            node_index = manager.NodeToIndex(node)
            pickup_times[auditor.label] = solution.Value(time_dimension.CumulVar(node_index))

        morning_points = [depot] + [auditor.facility for auditor in morning_order]
        pickup_points = [auditor.facility for auditor in pickup_order] + [depot]
        morning_url = build_google_maps_directions_url(morning_points) if len(morning_points) >= 2 else ""
        pickup_url = build_google_maps_directions_url(pickup_points) if len(pickup_points) >= 2 else ""

        routes.append(
            DailyDriverRoute(
                driver_number=vehicle_id + 1,
                auditors=assigned_auditors,
                morning_order=morning_order,
                pickup_order=pickup_order,
                morning_drop_times=morning_drop_times,
                pickup_times=pickup_times,
                latest_morning_start_seconds=latest_start,
                return_depot_seconds=end_time if assigned_auditors else None,
                total_driving_seconds=route_seconds if assigned_auditors else 0,
                total_distance_meters=route_meters if assigned_auditors else 0,
                morning_google_url=morning_url,
                pickup_google_url=pickup_url,
            )
        )

    used_routes = [route for route in routes if route.auditors]
    pickup_waits = []
    for route in used_routes:
        for auditor in route.pickup_order:
            pickup_time = route.pickup_times.get(auditor.label)
            if pickup_time is not None:
                pickup_waits.append(max(0, pickup_time - auditor.pickup_earliest_seconds))

    latest_common_start_candidates = [
        route.latest_morning_start_seconds for route in used_routes if route.latest_morning_start_seconds is not None
    ]
    totals: Dict[str, Optional[int]] = {
        "auditors": len(auditors),
        "drivers_used": len(used_routes),
        "driver_count": driver_count,
        "total_driving_time": sum(route.total_driving_seconds for route in used_routes),
        "total_distance": sum(route.total_distance_meters for route in used_routes),
        "latest_common_start": min(latest_common_start_candidates) if latest_common_start_candidates else None,
        "latest_return": max((route.return_depot_seconds for route in used_routes if route.return_depot_seconds is not None), default=None),
        "max_pickup_wait": max(pickup_waits, default=0),
        "total_pickup_wait": sum(pickup_waits),
    }
    return routes, totals


# -----------------------------------------------------------------------------
# Streamlit UI
# -----------------------------------------------------------------------------

def default_stop_values() -> List[str]:
    return [
        "25.221375, 55.285106",
        "24.782154, 54.738854",
        "24.398153, 54.554717",
        "24.222244, 55.695169",
        "24.219594, 55.776045",
        "23.659065, 53.725112",
        "23.132777, 53.799145",
        "23.915126, 52.803821",
    ]


def render_results(routes: List[DailyDriverRoute], totals: Dict[str, Optional[int]]) -> None:
    st.subheader("Summary")
    cols = st.columns(7)
    cols[0].metric("Auditors", totals.get("auditors") or 0)
    cols[1].metric("Drivers used", f"{totals.get('drivers_used') or 0}/{totals.get('driver_count') or 0}")
    cols[2].metric("Latest common morning start", format_clock(totals.get("latest_common_start")))
    cols[3].metric("Latest depot return", format_clock(totals.get("latest_return")))
    cols[4].metric("Max pickup wait", format_duration(int(totals.get("max_pickup_wait") or 0)))
    cols[5].metric("Total driving", format_duration(int(totals.get("total_driving_time") or 0)))
    cols[6].metric("Total distance", format_distance(int(totals.get("total_distance") or 0)))

    warnings = []
    for route in routes:
        if route.auditors and route.latest_morning_start_seconds is not None and route.latest_morning_start_seconds < 0:
            warnings.append(f"Driver {route.driver_number} would need to start before midnight to meet the morning arrival deadlines.")
        for auditor in route.pickup_order:
            pickup_time = route.pickup_times.get(auditor.label)
            if pickup_time is not None and pickup_time > auditor.pickup_latest_seconds:
                warnings.append(f"{auditor.label} is picked up after the allowed latest pickup time.")
    if warnings:
        st.warning(" ".join(warnings))

    st.subheader("Driver daily routes")
    for route in routes:
        with st.container(border=True):
            st.markdown(f"### Driver {route.driver_number}")
            st.write(
                f"{len(route.auditors)} auditor(s) · {format_duration(route.total_driving_seconds)} driving · "
                f"{format_distance(route.total_distance_meters)}"
            )
            if route.auditors:
                st.write(
                    f"Morning start at latest: **{format_clock(route.latest_morning_start_seconds)}** · "
                    f"Return to depot around: **{format_clock(route.return_depot_seconds)}**"
                )
            else:
                st.caption("No auditors assigned. This should only happen if there are more drivers than auditors, or if force-all-drivers is off.")
                continue

            link_cols = st.columns(2)
            if route.morning_google_url:
                link_cols[0].link_button("Open morning drop-off route", route.morning_google_url, use_container_width=True)
            if route.pickup_google_url:
                link_cols[1].link_button("Open afternoon pickup route", route.pickup_google_url, use_container_width=True)

            st.markdown("**Morning drop-off sequence**")
            morning_rows = []
            for idx, auditor in enumerate(route.morning_order, start=1):
                morning_rows.append(
                    {
                        "#": idx,
                        "Auditor/facility": auditor.label,
                        "Arrive by": format_clock(auditor.arrival_deadline_seconds),
                        "ETA if leaving at latest start": format_clock(route.morning_drop_times.get(auditor.label)),
                        "Coordinates": f"{auditor.facility.lat:.6f}, {auditor.facility.lng:.6f}",
                        "Original input": auditor.facility.original_input,
                    }
                )
            st.dataframe(morning_rows, hide_index=True, use_container_width=True)

            st.markdown("**Afternoon pickup sequence**")
            pickup_rows = []
            for idx, auditor in enumerate(route.pickup_order, start=1):
                pickup_time = route.pickup_times.get(auditor.label)
                wait_seconds = max(0, (pickup_time or 0) - auditor.pickup_earliest_seconds) if pickup_time is not None else 0
                pickup_rows.append(
                    {
                        "#": idx,
                        "Auditor/facility": auditor.label,
                        "Can leave from": format_clock(auditor.pickup_earliest_seconds),
                        "Latest pickup": format_clock(auditor.pickup_latest_seconds),
                        "Planned pickup": format_clock(pickup_time),
                        "Wait after leave time": format_duration(wait_seconds),
                        "Coordinates": f"{auditor.facility.lat:.6f}, {auditor.facility.lng:.6f}",
                    }
                )
            st.dataframe(pickup_rows, hide_index=True, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Multi-Driver Auditor Route Optimizer", page_icon="🚗", layout="wide")
    require_password_if_configured()
    reset_stale_session_state()

    st.title("Multi-Driver Auditor Route Optimizer")
    st.caption(
        "Same driver drops off and picks up the same auditor. The route starts at the depot in the morning, "
        "drops auditors at facilities, picks those same auditors up after work, then returns to the depot at end of day."
    )

    with st.sidebar:
        st.header("Settings")
        st.write(f"Routing provider: **{ROUTING_PROVIDER}**")
        if ROUTING_PROVIDER == "osrm":
            st.caption("Using OSRM/OpenStreetMap road travel times. No live traffic and no paid Google API key.")
        else:
            st.caption("Using straight-line estimates only.")
        st.divider()
        st.caption("Morning arrival = when the auditor must reach the facility.")
        st.caption("Pickup earliest = when the auditor can leave work, usually 16:00.")
        st.caption("Pickup latest = latest acceptable pickup, usually 17:00.")

    defaults = default_stop_values()

    st.markdown("### Daily setup")
    top_cols = st.columns([1, 1, 1, 1, 2])
    with top_cols[0]:
        auditor_count = st.number_input("Number of auditors/facilities", min_value=1, max_value=39, value=7, step=1)
    with top_cols[1]:
        driver_count = st.number_input("Number of drivers", min_value=1, max_value=20, value=4, step=1)
    with top_cols[2]:
        default_arrival_time = st.time_input("Default arrive by", value=datetime.time(9, 0))
    with top_cols[3]:
        default_pickup_earliest = st.time_input("Default can leave", value=datetime.time(16, 0))
    with top_cols[4]:
        depot_input = st.text_input(
            "Depot / office",
            value=st.session_state.get("depot_input", "24.485451, 54.381805"),
            placeholder="Office address, coordinates, or Google Maps link",
        )

    second_cols = st.columns([1, 1, 2])
    with second_cols[0]:
        default_pickup_latest = st.time_input("Default latest pickup", value=datetime.time(17, 0))
    with second_cols[1]:
        require_all_drivers = st.checkbox(
            "Use all drivers when possible",
            value=True,
            help="If auditors >= drivers, every driver is forced to handle at least one auditor.",
        )
    with second_cols[2]:
        st.info(
            "The optimizer prioritizes reducing pickup delay after work. A driver will only carry multiple auditors "
            "when the same-driver morning + afternoon plan still fits the time windows."
        )

    st.markdown("### Auditors / facilities")
    st.caption("Each row is one auditor's facility. The same driver that drops this auditor off will pick them up again.")

    stop_rows: List[Tuple[str, int, int, int]] = []
    with st.form("daily_route_form"):
        header = st.columns([0.3, 3.4, 1, 1, 1])
        header[0].markdown("**#**")
        header[1].markdown("**Facility Google Maps link / coordinates / address**")
        header[2].markdown("**Arrive by**")
        header[3].markdown("**Can leave**")
        header[4].markdown("**Latest pickup**")

        for idx in range(1, int(auditor_count) + 1):
            row = st.columns([0.3, 3.4, 1, 1, 1])
            row[0].write(idx)
            default_value = defaults[idx - 1] if idx <= len(defaults) else ""
            raw = row[1].text_input(
                f"Facility {idx}",
                value=default_value,
                label_visibility="collapsed",
                placeholder="https://maps.app.goo.gl/... or 24.4539,54.3773",
                key=f"facility_input_{idx}",
            )
            arrival_time = row[2].time_input(
                f"Arrive by {idx}",
                value=default_arrival_time,
                label_visibility="collapsed",
                key=f"arrival_time_{idx}",
            )
            pickup_earliest = row[3].time_input(
                f"Can leave {idx}",
                value=default_pickup_earliest,
                label_visibility="collapsed",
                key=f"pickup_earliest_{idx}",
            )
            pickup_latest = row[4].time_input(
                f"Latest pickup {idx}",
                value=default_pickup_latest,
                label_visibility="collapsed",
                key=f"pickup_latest_{idx}",
            )
            stop_rows.append(
                (
                    raw,
                    seconds_from_time(arrival_time),
                    seconds_from_time(pickup_earliest),
                    seconds_from_time(pickup_latest),
                )
            )

        submitted = st.form_submit_button("Optimize full-day routes", type="primary")

    if submitted:
        st.session_state["depot_input"] = depot_input
        try:
            cleaned_rows = [row for row in stop_rows if row[0].strip()]
            if not cleaned_rows:
                raise ValueError("Add at least one facility link, coordinate, or address.")
            if not depot_input.strip():
                raise ValueError("Depot / office is required for the full-day route model.")

            with st.spinner("Parsing locations, fetching route matrix, and optimizing full-day routes..."):
                depot = parse_location(depot_input.strip(), 0, label_prefix="Depot")
                auditors: List[AuditorStop] = []
                for idx, (raw, arrive_by, pickup_earliest, pickup_latest) in enumerate(cleaned_rows, start=1):
                    if pickup_latest < pickup_earliest:
                        raise ValueError(f"Row {idx}: latest pickup cannot be earlier than can-leave time.")
                    facility = parse_location(raw, idx, label_prefix="Facility")
                    auditors.append(
                        AuditorStop(
                            label=f"Auditor {idx}",
                            facility=facility,
                            arrival_deadline_seconds=arrive_by,
                            pickup_earliest_seconds=pickup_earliest,
                            pickup_latest_seconds=pickup_latest,
                        )
                    )

                routes, totals = solve_daily_auditor_routes(
                    depot=depot,
                    auditors=auditors,
                    driver_count=int(driver_count),
                    require_all_drivers_if_possible=require_all_drivers,
                )

            st.session_state["routes"] = routes
            st.session_state["totals"] = totals
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))

    if "routes" in st.session_state and "totals" in st.session_state:
        render_results(st.session_state["routes"], st.session_state["totals"])


if __name__ == "__main__":
    main()
