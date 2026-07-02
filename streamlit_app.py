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
    arrival_deadline_seconds: Optional[int] = None


@dataclass
class DriverRoute:
    driver_number: int
    stops: List[Location]
    travel_seconds: int
    travel_meters: int
    google_maps_url: str
    latest_start_seconds: Optional[int] = None
    finish_deadline_seconds: Optional[int] = None
    stop_arrival_seconds: Optional[List[int]] = None


# -----------------------------------------------------------------------------
# Settings and secrets helpers
# -----------------------------------------------------------------------------

def get_secret_or_env(name: str, default: str = "") -> str:
    """Read a Streamlit secret first, then an environment variable, then a default."""
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
    """Try to follow Google Maps short links to the full URL."""
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
    """Extract coordinates from raw coordinates or common Google Maps URL formats."""
    text = text.strip()

    # Raw coordinates, e.g. 24.4539,54.3773
    raw_match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if raw_match:
        return float(raw_match.group(1)), float(raw_match.group(2))

    # Google Maps place URLs often include !3dLAT!4dLNG for the actual selected place.
    # Prefer this over @LAT,LNG because @ can be just the camera / viewport center.
    bang_match = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", text)
    if bang_match:
        return float(bang_match.group(1)), float(bang_match.group(2))

    # Google Maps /@lat,lng,zoom format
    at_match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", text)
    if at_match:
        return float(at_match.group(1)), float(at_match.group(2))

    # URL query parameters like q=lat,lng, query=lat,lng, destination=lat,lng
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
    """Pull a readable place/address candidate from a Google Maps URL when coords are absent."""
    parsed = urlparse(url)
    path = unquote(parsed.path or "")

    # /maps/place/Some+Place/...
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
    """Free geocoding through OpenStreetMap Nominatim."""
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
            f"Could not geocode '{address}' using free OpenStreetMap/Nominatim. "
            "Try pasting coordinates instead."
        )

    result = data[0]
    formatted = result.get("display_name", address)
    return float(result["lat"]), float(result["lon"]), formatted


_last_uncached_geocode_at = 0.0

def geocode_address(address: str) -> Tuple[float, float, str]:
    """Rate-limit uncached geocoding requests."""
    global _last_uncached_geocode_at

    # If cached, Streamlit returns immediately. If not cached, keep Nominatim-friendly delay.
    elapsed = time.monotonic() - _last_uncached_geocode_at
    if elapsed < GEOCODE_DELAY_SECONDS:
        time.sleep(GEOCODE_DELAY_SECONDS - elapsed)

    value = geocode_address_cached(address.strip(), NOMINATIM_URL, APP_USER_AGENT)
    _last_uncached_geocode_at = time.monotonic()
    return value


def parse_location(line: str, index: int, label_prefix: str = "Stop") -> Location:
    original = line.strip()
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
    """Straight-line fallback: estimated at 35 km/h average speed."""
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
            f"Too many locations for the public OSRM server ({len(coords)} > {max_osrm_locations}). "
            "Use fewer stops, set ROUTING_PROVIDER=haversine, or self-host OSRM."
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


def make_virtual_depot_matrix(
    stop_locations: List[Location],
    stop_time_matrix: List[List[int]],
    stop_distance_matrix: List[List[int]],
) -> Tuple[List[Location], List[List[int]], List[List[int]]]:
    """Add a zero-cost virtual depot when the user does not provide a real start/end point."""
    virtual_depot = Location(label="Virtual depot", lat=0, lng=0, original_input="")
    locations = [virtual_depot] + stop_locations
    n = len(locations)
    time_matrix = [[0 for _ in range(n)] for _ in range(n)]
    distance_matrix = [[0 for _ in range(n)] for _ in range(n)]

    for i in range(1, n):
        for j in range(1, n):
            time_matrix[i][j] = stop_time_matrix[i - 1][j - 1]
            distance_matrix[i][j] = stop_distance_matrix[i - 1][j - 1]

    return locations, time_matrix, distance_matrix


# -----------------------------------------------------------------------------
# Optimization
# -----------------------------------------------------------------------------

def solve_vrp(
    locations: List[Location],
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
    driver_count: int,
    return_to_depot: bool,
    use_virtual_depot: bool,
    finish_deadline_seconds: Optional[int] = None,
    require_all_drivers_if_possible: bool = True,
) -> List[DriverRoute]:
    if driver_count < 1:
        raise ValueError("Driver count must be at least 1.")
    if len(locations) <= 1:
        raise ValueError("Add at least one stop.")

    depot_index = 0

    # If drivers do not return to the depot, the final leg back to depot is cost 0.
    # This creates open routes: office -> stops -> final stop.
    effective_time_matrix = [row[:] for row in time_matrix]
    effective_distance_matrix = [row[:] for row in distance_matrix]
    if not return_to_depot or use_virtual_depot:
        for i in range(len(locations)):
            if i != depot_index:
                effective_time_matrix[i][depot_index] = 0
                effective_distance_matrix[i][depot_index] = 0

    manager = pywrapcp.RoutingIndexManager(len(locations), driver_count, depot_index)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return int(effective_time_matrix[from_node][to_node])

    transit_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    horizon_seconds = max(1, sum(max(row) for row in effective_time_matrix) * 2)
    routing.AddDimension(transit_callback_index, 0, horizon_seconds, True, "Time")
    time_dimension = routing.GetDimensionOrDie("Time")

    # Main objective: finish the whole job as early as possible.
    # This minimizes the longest driver's route, not equal stop counts.
    # Arc cost remains as a smaller tie-breaker against unnecessary driving.
    time_dimension.SetGlobalSpanCostCoefficient(1_000_000)

    # For this use case, an unused driver can cause auditors to wait unnecessarily
    # while another driver handles multiple far-away stops. If there are at least
    # as many stops as drivers, force every driver to get at least one stop.
    if require_all_drivers_if_possible:
        vehicles_to_force = min(driver_count, len(locations) - 1)
        solver = routing.solver()
        for vehicle_id in range(vehicles_to_force):
            solver.Add(routing.NextVar(routing.Start(vehicle_id)) != routing.End(vehicle_id))

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 25

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        raise ValueError("No route solution found. Try fewer drivers, fewer stops, or check unreachable locations.")

    driver_routes: List[DriverRoute] = []
    for vehicle_id in range(driver_count):
        index = routing.Start(vehicle_id)
        route_nodes = []
        arrival_offsets = []
        route_seconds = 0
        route_meters = 0
        previous_node = None

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if previous_node is not None:
                route_seconds += effective_time_matrix[previous_node][node]
                route_meters += effective_distance_matrix[previous_node][node]
            if node != depot_index:
                route_nodes.append(node)
                arrival_offsets.append(route_seconds)
            previous_node = node
            index = solution.Value(routing.NextVar(index))

        end_node = manager.IndexToNode(index)
        if previous_node is not None:
            route_seconds += effective_time_matrix[previous_node][end_node]
            route_meters += effective_distance_matrix[previous_node][end_node]

        stops = [locations[node] for node in route_nodes]
        maps_url = build_google_maps_directions_url(stops, locations[0], return_to_depot, use_virtual_depot)

        latest_start_seconds = None
        stop_arrival_seconds = None
        if route_nodes:
            latest_start_candidates: List[int] = []
            for node, offset in zip(route_nodes, arrival_offsets):
                stop_deadline = locations[node].arrival_deadline_seconds
                if stop_deadline is None:
                    stop_deadline = finish_deadline_seconds
                if stop_deadline is not None:
                    latest_start_candidates.append(stop_deadline - offset)

            if latest_start_candidates:
                # Start as late as possible while still reaching every assigned auditor/facility
                # by that stop's own arrival time.
                latest_start_seconds = min(latest_start_candidates)
                stop_arrival_seconds = [latest_start_seconds + offset for offset in arrival_offsets]
        elif finish_deadline_seconds is not None:
            latest_start_seconds = finish_deadline_seconds
            stop_arrival_seconds = []

        driver_routes.append(
            DriverRoute(
                driver_number=vehicle_id + 1,
                stops=stops,
                travel_seconds=route_seconds,
                travel_meters=route_meters,
                google_maps_url=maps_url,
                latest_start_seconds=latest_start_seconds,
                finish_deadline_seconds=finish_deadline_seconds,
                stop_arrival_seconds=stop_arrival_seconds,
            )
        )

    return driver_routes


# -----------------------------------------------------------------------------
# Formatting and links
# -----------------------------------------------------------------------------

def build_google_maps_directions_url(
    stops: List[Location], depot: Location, return_to_depot: bool, use_virtual_depot: bool
) -> str:
    if not stops:
        return ""

    def coord(loc: Location) -> str:
        return f"{loc.lat},{loc.lng}"

    if use_virtual_depot:
        origin = coord(stops[0])
        destination = coord(stops[-1])
        waypoints = [coord(stop) for stop in stops[1:-1]]
    else:
        origin = coord(depot)
        if return_to_depot:
            destination = coord(depot)
            waypoints = [coord(stop) for stop in stops]
        else:
            destination = coord(stops[-1])
            waypoints = [coord(stop) for stop in stops[:-1]]

    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(destination)}"
        "&travelmode=driving"
    )
    if waypoints:
        url += "&waypoints=" + quote_plus("|".join(waypoints))
    return url


def format_duration(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_distance(meters: int) -> str:
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{meters} m"


def parse_time_of_day(value: str) -> int:
    value = (value or "09:00").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        raise ValueError("Time must be in HH:MM format, for example 09:00.")
    hours = int(match.group(1))
    minutes = int(match.group(2))
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError("Time must be a valid time of day.")
    return hours * 3600 + minutes * 60


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


# -----------------------------------------------------------------------------
# Streamlit UI
# -----------------------------------------------------------------------------


def optimize_routes(
    stop_rows: List[Tuple[str, int]],
    depot_input: str,
    driver_count: int,
    return_to_depot: bool,
    require_all_drivers_if_possible: bool,
    default_arrival_deadline_seconds: int,
) -> Tuple[List[DriverRoute], Dict[str, Optional[int]]]:
    cleaned_rows = [(raw.strip(), deadline) for raw, deadline in stop_rows if raw.strip()]
    if not cleaned_rows:
        raise ValueError("Add at least one Google Maps link, coordinate, or address.")

    stop_locations: List[Location] = []
    for idx, (raw, deadline_seconds) in enumerate(cleaned_rows, start=1):
        loc = parse_location(raw, idx)
        loc.arrival_deadline_seconds = deadline_seconds
        stop_locations.append(loc)

    if depot_input.strip():
        depot = parse_location(depot_input.strip(), 0, label_prefix="Depot")
        all_locations = [depot] + stop_locations
        time_matrix, distance_matrix = compute_route_matrix(all_locations)
        use_virtual_depot = False
    else:
        stop_time_matrix, stop_distance_matrix = compute_route_matrix(stop_locations)
        all_locations, time_matrix, distance_matrix = make_virtual_depot_matrix(
            stop_locations, stop_time_matrix, stop_distance_matrix
        )
        use_virtual_depot = True
        return_to_depot = False

    routes = solve_vrp(
        all_locations,
        time_matrix,
        distance_matrix,
        driver_count,
        return_to_depot,
        use_virtual_depot,
        default_arrival_deadline_seconds,
        require_all_drivers_if_possible=require_all_drivers_if_possible,
    )

    used_routes = [route for route in routes if route.stops]
    longest_route_seconds = max((route.travel_seconds for route in used_routes), default=0)
    latest_common_start_candidates = [
        route.latest_start_seconds for route in used_routes if route.latest_start_seconds is not None
    ]
    latest_common_start = min(latest_common_start_candidates) if latest_common_start_candidates else None
    earliest_deadline = min((loc.arrival_deadline_seconds or default_arrival_deadline_seconds) for loc in stop_locations)
    latest_deadline = max((loc.arrival_deadline_seconds or default_arrival_deadline_seconds) for loc in stop_locations)

    totals: Dict[str, Optional[int]] = {
        "time": sum(route.travel_seconds for route in used_routes),
        "distance": sum(route.travel_meters for route in used_routes),
        "stops": sum(len(route.stops) for route in used_routes),
        "drivers_used": len(used_routes),
        "driver_count": driver_count,
        "longest_route": longest_route_seconds,
        "latest_common_start": latest_common_start,
        "earliest_deadline": earliest_deadline,
        "latest_deadline": latest_deadline,
    }
    return routes, totals


def render_results(routes: List[DriverRoute], totals: Dict[str, Optional[int]]) -> None:
    st.subheader("Summary")
    cols = st.columns(7)
    cols[0].metric("Total stops", totals.get("stops") or 0)
    cols[1].metric("Drivers used", f"{totals.get('drivers_used') or 0}/{totals.get('driver_count') or 0}")
    cols[2].metric("Longest driver route", format_duration(int(totals.get("longest_route") or 0)))
    cols[3].metric("Latest common start", format_clock(totals.get("latest_common_start")))
    cols[4].metric(
        "Arrival window",
        f"{format_clock(totals.get('earliest_deadline'))}–{format_clock(totals.get('latest_deadline'))}",
    )
    cols[5].metric("Total driving time", format_duration(int(totals.get("time") or 0)))
    cols[6].metric("Total distance", format_distance(int(totals.get("distance") or 0)))

    warnings = []
    for route in routes:
        if route.stops and route.latest_start_seconds is not None and route.latest_start_seconds < 0:
            warnings.append(f"Driver {route.driver_number} would need to start before midnight to satisfy all arrival times.")
    if warnings:
        st.warning(" ".join(warnings))

    st.subheader("Driver routes")
    for route in routes:
        with st.container(border=True):
            left, right = st.columns([2, 1])
            with left:
                st.markdown(f"### Driver {route.driver_number}")
                st.write(
                    f"{len(route.stops)} stops · {format_duration(route.travel_seconds)} · "
                    f"{format_distance(route.travel_meters)}"
                )
                if route.latest_start_seconds is not None and route.stops:
                    st.write(f"Start at latest: **{format_clock(route.latest_start_seconds)}**")
            with right:
                if route.google_maps_url:
                    st.link_button("Open route in Google Maps", route.google_maps_url, use_container_width=True)

            if not route.stops:
                st.caption("No stops assigned. This should only happen when there are more drivers than stops, or when 'force all drivers' is off.")
                continue

            rows = []
            for idx, stop in enumerate(route.stops, start=1):
                eta = ""
                if route.stop_arrival_seconds:
                    eta = format_clock(route.stop_arrival_seconds[idx - 1])
                deadline = stop.arrival_deadline_seconds
                rows.append(
                    {
                        "#": idx,
                        "Stop": stop.label,
                        "Arrive by": format_clock(deadline),
                        "ETA if leaving at latest start": eta,
                        "Coordinates": f"{stop.lat:.6f}, {stop.lng:.6f}",
                        "Original input": stop.original_input,
                    }
                )
            st.dataframe(rows, hide_index=True, use_container_width=True)


def _default_stop_values() -> List[str]:
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


def main() -> None:
    st.set_page_config(page_title="Multi-Driver Route Optimizer", page_icon="🚗", layout="wide")
    require_password_if_configured()

    st.title("Multi-Driver Route Optimizer")
    st.caption(
        "Plan auditor drop-offs/pickups with multiple drivers. Each stop can have its own required arrival time. "
        "No paid Google API key is required."
    )

    with st.sidebar:
        st.header("Settings")
        st.write(f"Routing provider: **{ROUTING_PROVIDER}**")
        if ROUTING_PROVIDER == "osrm":
            st.caption("Using OSRM/OpenStreetMap road travel times. No live traffic.")
        else:
            st.caption("Using straight-line estimates only.")
        st.divider()
        st.caption("For morning drop-offs: arrival time means the auditor must reach the facility by that time.")
        st.caption("For afternoon pickup planning: use the facility as the stop and set the arrival time to the latest pickup time, e.g. 17:00.")

    defaults = _default_stop_values()

    st.markdown("### Trip setup")
    trip_mode = st.radio(
        "Trip type",
        ["Morning drop-off", "Afternoon pickup / return"],
        horizontal=True,
        help="The optimizer is the same, but this changes the default return-to-depot behavior and labels.",
    )

    c_top1, c_top2, c_top3, c_top4 = st.columns([1, 1, 1, 2])
    with c_top1:
        stop_count = st.number_input("Number of stops", min_value=1, max_value=40, value=7, step=1)
    with c_top2:
        driver_count = st.number_input("Number of drivers", min_value=1, max_value=20, value=4, step=1)
    with c_top3:
        default_time = st.time_input("Default arrival time", value=datetime.time(9, 0))
    with c_top4:
        depot_input = st.text_input(
            "Depot / office / starting point",
            value=st.session_state.get("depot_input", "24.485451, 54.381805"),
            placeholder="Office address, coordinates, or Google Maps link",
        )

    default_return_to_depot = trip_mode == "Afternoon pickup / return"
    c_opts1, c_opts2 = st.columns([1, 2])
    with c_opts1:
        return_to_depot = st.checkbox(
            "Include return to depot in route cost",
            value=default_return_to_depot,
            help="For morning drop-offs this is usually off. For afternoon pickups/returns this is usually on.",
        )
    with c_opts2:
        require_all_drivers_if_possible = st.checkbox(
            "Use all available drivers when possible",
            value=True,
            help="If there are at least as many stops as drivers, every driver will get at least one stop.",
        )

    st.markdown("### Stops")
    st.caption("One Google Maps link / address / coordinate per row. If you leave arrival time unchanged, it defaults to the time above.")

    stop_rows: List[Tuple[str, int]] = []
    with st.form("route_form"):
        header = st.columns([0.4, 4, 1.2])
        header[0].markdown("**#**")
        header[1].markdown("**Google Maps link / coordinates / address**")
        header[2].markdown("**Arrive by**")

        default_arrival_seconds = seconds_from_time(default_time)
        for idx in range(1, int(stop_count) + 1):
            row = st.columns([0.4, 4, 1.2])
            row[0].write(idx)
            default_value = defaults[idx - 1] if idx <= len(defaults) else ""
            raw = row[1].text_input(
                f"Stop {idx}",
                value=default_value,
                label_visibility="collapsed",
                placeholder="https://maps.app.goo.gl/... or 24.4539,54.3773",
                key=f"stop_input_{idx}",
            )
            arrival_time = row[2].time_input(
                f"Arrival time {idx}",
                value=default_time,
                label_visibility="collapsed",
                key=f"arrival_time_{idx}",
            )
            stop_rows.append((raw, seconds_from_time(arrival_time)))

        submitted = st.form_submit_button("Optimize routes", type="primary")

    if submitted:
        st.session_state["depot_input"] = depot_input
        try:
            with st.spinner("Parsing locations, fetching route matrix, and optimizing routes..."):
                routes, totals = optimize_routes(
                    stop_rows=stop_rows,
                    depot_input=depot_input,
                    driver_count=int(driver_count),
                    return_to_depot=return_to_depot,
                    require_all_drivers_if_possible=require_all_drivers_if_possible,
                    default_arrival_deadline_seconds=default_arrival_seconds,
                )
            st.session_state["routes"] = routes
            st.session_state["totals"] = totals
        except Exception as exc:  # noqa: BLE001 - display friendly app error
            st.error(str(exc))

    if "routes" in st.session_state and "totals" in st.session_state:
        render_results(st.session_state["routes"], st.session_state["totals"])


if __name__ == "__main__":
    main()
