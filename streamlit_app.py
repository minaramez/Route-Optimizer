import base64
import datetime
import html
import hashlib
import itertools
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


# -----------------------------------------------------------------------------
# Frontend settings
# -----------------------------------------------------------------------------

# Google Maps route logo button sizing.
# Increase/decrease this one value to resize the rectangular logo button.
GOOGLE_LOGO_IMAGE_WIDTH = 170
GOOGLE_LOGO_BUTTON_PADDING_Y = 6
GOOGLE_LOGO_BUTTON_PADDING_X = 10


def render_google_maps_logo_button(url: str) -> None:
    """Render a centered Google Maps logo link with a tight border around the image."""
    logo_path = os.path.join(os.path.dirname(__file__), "google.png")
    safe_url = html.escape(url, quote=True)

    if os.path.exists(logo_path):
        with open(logo_path, "rb") as image_file:
            encoded_logo = base64.b64encode(image_file.read()).decode("utf-8")
        logo_html = (
            f'<img src="data:image/png;base64,{encoded_logo}" '
            f'alt="Open route in Google Maps" '
            f'style="width:{GOOGLE_LOGO_IMAGE_WIDTH}px; height:auto; object-fit:contain; display:block;" />'
        )
    else:
        logo_html = (
            '<span style="font-size:22px; font-weight:600; color:#1f1f1f; white-space:nowrap;">🗺️ Google Maps</span>'
        )

    st.markdown(
        f"""
        <div style="display:flex; justify-content:center; align-items:center; width:100%; margin:0.55rem 0 1.05rem 0;">
            <a href="{safe_url}" target="_blank" rel="noopener noreferrer" title="Open route in Google Maps"
               style="
                    display:inline-flex;
                    align-items:center;
                    justify-content:center;
                    padding:{GOOGLE_LOGO_BUTTON_PADDING_Y}px {GOOGLE_LOGO_BUTTON_PADDING_X}px;
                    border:1px solid rgba(60, 64, 67, 0.24);
                    border-radius:14px;
                    background:#ffffff;
                    box-shadow:0 2px 7px rgba(60, 64, 67, 0.14);
                    text-decoration:none;
                    box-sizing:border-box;
                    line-height:0;
               ">
                {logo_html}
            </a>
        </div>
        """,
        unsafe_allow_html=True,
    )


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
    pickup_latest_seconds: Optional[int] = None


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
APP_STATE_VERSION = "2026-07-02-v9-outbound-route-order"
WAIT_BUCKET_SECONDS = int_setting("WAIT_BUCKET_SECONDS", 600)
COMPACT_PAIR_THRESHOLD_SECONDS = int_setting("COMPACT_PAIR_THRESHOLD_SECONDS", 45 * 60)
# Morning route sanity: if facilities have similar arrival deadlines, do not drive past
# a closer facility to a farther one and then backtrack. A farther stop may still go
# first when it has a meaningfully earlier arrival time.
OUTBOUND_BACKTRACK_GRACE_SECONDS = int_setting("OUTBOUND_BACKTRACK_GRACE_SECONDS", 10 * 60)
EARLIER_DEADLINE_OVERRIDES_BACKTRACK_SECONDS = int_setting("EARLIER_DEADLINE_OVERRIDES_BACKTRACK_SECONDS", 20 * 60)
MORNING_EARLY_WAIT_COMFORT_SECONDS = int_setting("MORNING_EARLY_WAIT_COMFORT_SECONDS", 90 * 60)


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

def route_drive_time(order: List[int], time_matrix: List[List[int]]) -> int:
    if len(order) < 2:
        return 0
    return sum(time_matrix[order[i]][order[i + 1]] for i in range(len(order) - 1))


def route_drive_distance(order: List[int], distance_matrix: List[List[int]]) -> int:
    if len(order) < 2:
        return 0
    return sum(distance_matrix[order[i]][order[i + 1]] for i in range(len(order) - 1))


def objective_bucket(seconds: int, bucket_seconds: int = WAIT_BUCKET_SECONDS) -> int:
    """Put small time differences in the same bucket.

    This prevents the optimizer from making a weird geographic pairing just to save
    a few minutes of calculated wait time. Auditor time still wins, but once two
    options are roughly similar for auditors, facility clustering wins.
    """
    bucket_seconds = max(1, int(bucket_seconds))
    return math.ceil(max(0, int(seconds)) / bucket_seconds)


def group_compactness_stats(
    all_auditors: List[AuditorStop],
    group: List[AuditorStop],
    time_matrix: List[List[int]],
    threshold_seconds: int = COMPACT_PAIR_THRESHOLD_SECONDS,
) -> Dict[str, int]:
    """Measure whether auditors assigned to one car are actually near each other.

    This is the rule that fixes cases like:
        Dubai + Al Taweelah in one car
    when a much more natural pairing exists:
        Tarmeem + Al Taweelah in one car, Dubai alone

    The numbers are based on facility-to-facility road time, not depot travel time.
    """
    if len(group) < 2:
        return {
            "max_facility_gap": 0,
            "total_facility_gap": 0,
            "far_pair_penalty": 0,
        }

    pair_times: List[int] = []
    for left, right in itertools.combinations(group, 2):
        i = auditor_node(all_auditors, left)
        j = auditor_node(all_auditors, right)
        # Road times may be asymmetric. Use the average as a stable closeness score.
        pair_time = (time_matrix[i][j] + time_matrix[j][i]) // 2
        pair_times.append(pair_time)

    threshold_seconds = max(0, int(threshold_seconds))
    far_pair_penalty = sum(max(0, pair_time - threshold_seconds) for pair_time in pair_times)
    return {
        "max_facility_gap": max(pair_times, default=0),
        "total_facility_gap": sum(pair_times),
        "far_pair_penalty": far_pair_penalty,
    }


def morning_outbound_backtrack_penalty(
    all_auditors: List[AuditorStop],
    order: List[AuditorStop],
    time_matrix: List[List[int]],
) -> int:
    """Penalize morning routes that drive outward, then back toward the depot.

    Example this is meant to prevent:
        Depot -> Al Taweelah -> Tarmeem
    when Tarmeem is on the way from the depot to Al Taweelah and both audits start
    at roughly the same time.

    The penalty is waived when the farther stop has a meaningfully earlier arrival
    deadline, because then the backtrack may be necessary operationally.
    """
    if len(order) < 2:
        return 0

    penalty = 0
    for current, nxt in zip(order, order[1:]):
        current_node = auditor_node(all_auditors, current)
        next_node = auditor_node(all_auditors, nxt)

        current_from_depot = time_matrix[0][current_node]
        next_from_depot = time_matrix[0][next_node]
        backtrack_seconds = current_from_depot - next_from_depot

        # If the current/farther auditor has a much earlier start time, allow it.
        current_is_much_earlier = (
            nxt.arrival_deadline_seconds - current.arrival_deadline_seconds
            >= EARLIER_DEADLINE_OVERRIDES_BACKTRACK_SECONDS
        )

        if backtrack_seconds > OUTBOUND_BACKTRACK_GRACE_SECONDS and not current_is_much_earlier:
            penalty += backtrack_seconds

    return penalty


def auditor_node(auditors: List[AuditorStop], auditor: AuditorStop) -> int:
    # In the physical matrix, node 0 is depot and auditors are 1..N in input order.
    return auditors.index(auditor) + 1


def candidate_orders(items: List[AuditorStop], max_exact: int = 8) -> List[Tuple[AuditorStop, ...]]:
    """Return route-order candidates.

    For normal use, a driver's group is small, so exact permutations are fine.
    For very large groups, use a few sensible greedy orders to avoid freezing Streamlit.
    """
    if len(items) <= max_exact:
        return list(itertools.permutations(items))

    by_arrival = tuple(sorted(items, key=lambda a: (a.arrival_deadline_seconds, a.label)))
    by_leave = tuple(sorted(items, key=lambda a: (a.pickup_earliest_seconds, a.label)))
    by_distance_from_depot = tuple(items)
    return [by_arrival, by_leave, by_distance_from_depot]


def best_morning_sequence(
    depot: Location,
    all_auditors: List[AuditorStop],
    group: List[AuditorStop],
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
) -> Tuple[List[AuditorStop], Dict[str, int], Optional[int], int, int, int]:
    """Choose the morning drop-off order for one driver.

    We do not hard-fail if an arrival time is impossible from the depot. Instead we
    return a negative latest_start and show the user that this driver would need to
    leave before midnight / before the workday. This is much more helpful than
    "no feasible schedule".
    """
    if not group:
        return [], {}, None, 0, 0, 0

    best = None
    for order in candidate_orders(group):
        elapsed = 0
        prev_node = 0
        offsets: Dict[str, int] = {}
        for auditor in order:
            node = auditor_node(all_auditors, auditor)
            elapsed += time_matrix[prev_node][node]
            offsets[auditor.label] = elapsed
            prev_node = node

        latest_start = min(a.arrival_deadline_seconds - offsets[a.label] for a in order)
        start_deficit = max(0, -latest_start)
        drive_time = elapsed
        drive_distance = route_drive_distance(
            [0] + [auditor_node(all_auditors, a) for a in order], distance_matrix
        )
        # Prefer schedules that do not need a before-midnight start, then latest possible start,
        # then shorter driving.
        objective = (start_deficit, -latest_start, drive_time, drive_distance)
        if best is None or objective < best[0]:
            best = (objective, list(order), offsets, latest_start, drive_time, drive_distance)

    assert best is not None
    _, order, offsets, latest_start, drive_time, drive_distance = best
    drop_times = {label: latest_start + offset for label, offset in offsets.items()}
    return order, drop_times, latest_start, drive_time, drive_distance, max(0, -latest_start)


def best_pickup_sequence(
    depot: Location,
    all_auditors: List[AuditorStop],
    group: List[AuditorStop],
    morning_order: List[AuditorStop],
    morning_drop_times: Dict[str, int],
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
) -> Tuple[List[AuditorStop], Dict[str, int], Optional[int], int, int, int, int]:
    """Choose the afternoon pickup order for one driver.

    The driver does NOT return to depot after morning drop-off. The afternoon route
    starts from the driver's last morning drop-off facility, waits/repositions if
    needed, picks up the same auditors, then returns to depot.
    """
    if not group:
        return [], {}, None, 0, 0, 0, 0

    if morning_order:
        start_node = auditor_node(all_auditors, morning_order[-1])
        start_time = morning_drop_times.get(morning_order[-1].label, 0)
    else:
        start_node = 0
        start_time = 0

    best = None
    for order in candidate_orders(group):
        current_time = start_time
        prev_node = start_node
        pickup_times: Dict[str, int] = {}
        drive_time = 0
        drive_distance = 0
        waits: List[int] = []

        for auditor in order:
            node = auditor_node(all_auditors, auditor)
            travel = time_matrix[prev_node][node]
            distance = distance_matrix[prev_node][node]
            arrival_at_facility = current_time + travel
            pickup_time = max(auditor.pickup_earliest_seconds, arrival_at_facility)
            wait_after_can_leave = max(0, pickup_time - auditor.pickup_earliest_seconds)

            pickup_times[auditor.label] = pickup_time
            waits.append(wait_after_can_leave)
            drive_time += travel
            drive_distance += distance
            current_time = pickup_time
            prev_node = node

        drive_time += time_matrix[prev_node][0]
        drive_distance += distance_matrix[prev_node][0]
        return_depot_time = current_time + time_matrix[prev_node][0]

        max_wait = max(waits, default=0)
        total_wait = sum(waits)
        # Main priority: do not make one auditor wait forever. Then reduce total waiting,
        # then finish earlier, then reduce driving.
        objective = (max_wait, total_wait, return_depot_time, drive_time, drive_distance)
        if best is None or objective < best[0]:
            best = (objective, list(order), pickup_times, return_depot_time, drive_time, drive_distance, max_wait, total_wait)

    assert best is not None
    _, order, pickup_times, return_depot_time, drive_time, drive_distance, max_wait, total_wait = best
    return order, pickup_times, return_depot_time, drive_time, drive_distance, max_wait, total_wait


def evaluate_group_plan(
    depot: Location,
    all_auditors: List[AuditorStop],
    group: List[AuditorStop],
    driver_number: int,
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
) -> Tuple[DailyDriverRoute, Dict[str, int]]:
    """Evaluate one driver's full-day route as an auditor-first schedule.

    Important business rule: the same driver that drops an auditor off must pick
    that same auditor up again. Therefore the morning order and afternoon pickup
    order cannot be optimized independently. The last morning drop-off determines
    where the driver is physically waiting/repositioning before pickups begin.

    Objective inside one driver group:
      1. minimize the worst auditor inconvenience, including being dropped too early
         before the audit and being picked up too late after the audit
      2. minimize total auditor inconvenience
      3. avoid before-midnight morning starts
      4. avoid out-and-back zigzag routes
      5. reduce driving only after auditor time is handled
    """
    if not group:
        route = DailyDriverRoute(
            driver_number=driver_number,
            auditors=[],
            morning_order=[],
            pickup_order=[],
            morning_drop_times={},
            pickup_times={},
            latest_morning_start_seconds=None,
            return_depot_seconds=None,
            total_driving_seconds=0,
            total_distance_meters=0,
            morning_google_url="",
            pickup_google_url="",
        )
        stats = {
            "max_afternoon_wait": 0,
            "total_afternoon_wait": 0,
            "max_morning_early_wait": 0,
            "total_morning_early_wait": 0,
            "max_auditor_wait": 0,
            "total_auditor_wait": 0,
            "start_deficit": 0,
            "return_depot": 0,
            "drive_time": 0,
            "distance": 0,
            "route_span": 0,
            "max_facility_gap": 0,
            "total_facility_gap": 0,
            "far_pair_penalty": 0,
            "morning_backtrack_penalty": 0,
        }
        return route, stats

    compactness = group_compactness_stats(all_auditors, group, time_matrix)
    max_facility_gap = compactness["max_facility_gap"]
    total_facility_gap = compactness["total_facility_gap"]
    far_pair_penalty = compactness["far_pair_penalty"]

    best = None
    for morning_tuple in candidate_orders(group):
        morning_order = list(morning_tuple)
        elapsed = 0
        prev_node = 0
        offsets: Dict[str, int] = {}
        morning_nodes = [0]
        for auditor in morning_order:
            node = auditor_node(all_auditors, auditor)
            elapsed += time_matrix[prev_node][node]
            offsets[auditor.label] = elapsed
            morning_nodes.append(node)
            prev_node = node

        latest_start = min(a.arrival_deadline_seconds - offsets[a.label] for a in morning_order)
        start_deficit = max(0, -latest_start)
        morning_drop_times = {label: latest_start + offset for label, offset in offsets.items()}
        # Auditor time matters in the morning too. If someone is dropped at 07:50
        # for a 09:30 audit, that is 1h40 of auditor waiting and should be
        # heavily penalized. This prevents routes like Abu Dhabi -> Dubai ->
        # half-way back to Abu Dhabi just because the driving path looks efficient.
        morning_early_waits = [
            max(0, auditor.arrival_deadline_seconds - morning_drop_times[auditor.label])
            for auditor in morning_order
        ]
        max_morning_early_wait = max(morning_early_waits, default=0)
        total_morning_early_wait = sum(morning_early_waits)
        morning_drive = elapsed
        morning_distance = route_drive_distance(morning_nodes, distance_matrix)
        morning_backtrack_penalty = morning_outbound_backtrack_penalty(
            all_auditors, morning_order, time_matrix
        )
        morning_early_wait_excess = max(
            0, max_morning_early_wait - MORNING_EARLY_WAIT_COMFORT_SECONDS
        )

        # Afternoon starts from the final morning drop-off. The driver can wait there,
        # but auditors should not wait much after their Can leave time.
        start_node = morning_nodes[-1]
        start_time = morning_drop_times.get(morning_order[-1].label, 0)

        for pickup_tuple in candidate_orders(group):
            pickup_order = list(pickup_tuple)
            current_time = start_time
            prev_pickup_node = start_node
            pickup_times: Dict[str, int] = {}
            pickup_drive = 0
            pickup_distance = 0
            waits: List[int] = []

            for auditor in pickup_order:
                node = auditor_node(all_auditors, auditor)
                travel = time_matrix[prev_pickup_node][node]
                distance = distance_matrix[prev_pickup_node][node]
                arrival_at_facility = current_time + travel
                pickup_time = max(auditor.pickup_earliest_seconds, arrival_at_facility)
                wait_after_can_leave = max(0, pickup_time - auditor.pickup_earliest_seconds)

                pickup_times[auditor.label] = pickup_time
                waits.append(wait_after_can_leave)
                pickup_drive += travel
                pickup_distance += distance
                current_time = pickup_time
                prev_pickup_node = node

            pickup_drive += time_matrix[prev_pickup_node][0]
            pickup_distance += distance_matrix[prev_pickup_node][0]
            return_depot_time = current_time + time_matrix[prev_pickup_node][0]

            max_afternoon_wait = max(waits, default=0)
            total_afternoon_wait = sum(waits)
            max_auditor_wait = max(max_morning_early_wait, max_afternoon_wait)
            total_auditor_wait = total_morning_early_wait + total_afternoon_wait
            total_drive = morning_drive + pickup_drive
            total_distance = morning_distance + pickup_distance

            route_span = max_facility_gap

            # Primary objective is auditor experience, but the morning direction
            # must still make operational sense. If two stops have similar start
            # times, do not pass a nearby facility, go to the far one, then come
            # back. Only allow that when the farther stop has a much earlier
            # required arrival time.
            #
            # Ordering priorities inside one car:
            # 1) avoid impossible / before-midnight starts
            # 2) avoid late afternoon pickups
            # 3) avoid extreme early morning drop-offs beyond a comfort threshold
            # 4) avoid outbound backtracking in the morning
            # 5) then reduce exact waiting and driving
            objective = (
                start_deficit,
                max_afternoon_wait,
                morning_early_wait_excess,
                morning_backtrack_penalty,
                max_auditor_wait,
                total_auditor_wait,
                max_morning_early_wait,
                route_span,
                return_depot_time,
                total_drive,
                total_distance,
            )
            if best is None or objective < best[0]:
                best = (
                    objective,
                    morning_order,
                    pickup_order,
                    morning_drop_times,
                    pickup_times,
                    latest_start,
                    return_depot_time,
                    total_drive,
                    total_distance,
                    max_afternoon_wait,
                    total_afternoon_wait,
                    max_morning_early_wait,
                    total_morning_early_wait,
                    max_auditor_wait,
                    total_auditor_wait,
                    start_deficit,
                    route_span,
                    morning_backtrack_penalty,
                )

    assert best is not None
    (
        _,
        morning_order,
        pickup_order,
        morning_drop_times,
        pickup_times,
        latest_start,
        return_depot,
        total_drive,
        total_distance,
        max_afternoon_wait,
        total_afternoon_wait,
        max_morning_early_wait,
        total_morning_early_wait,
        max_auditor_wait,
        total_auditor_wait,
        start_deficit,
        route_span,
        morning_backtrack_penalty,
    ) = best

    morning_points = [depot] + [auditor.facility for auditor in morning_order]
    afternoon_start = morning_order[-1].facility
    pickup_points = [afternoon_start] + [auditor.facility for auditor in pickup_order] + [depot]

    route = DailyDriverRoute(
        driver_number=driver_number,
        auditors=list(group),
        morning_order=morning_order,
        pickup_order=pickup_order,
        morning_drop_times=morning_drop_times,
        pickup_times=pickup_times,
        latest_morning_start_seconds=latest_start,
        return_depot_seconds=return_depot,
        total_driving_seconds=total_drive,
        total_distance_meters=total_distance,
        morning_google_url=build_google_maps_directions_url(morning_points) if len(morning_points) >= 2 else "",
        pickup_google_url=build_google_maps_directions_url(pickup_points) if len(pickup_points) >= 2 else "",
    )
    stats = {
        "max_afternoon_wait": max_afternoon_wait,
        "total_afternoon_wait": total_afternoon_wait,
        "max_morning_early_wait": max_morning_early_wait,
        "total_morning_early_wait": total_morning_early_wait,
        "max_auditor_wait": max_auditor_wait,
        "total_auditor_wait": total_auditor_wait,
        "start_deficit": start_deficit,
        "return_depot": return_depot or 0,
        "drive_time": route.total_driving_seconds,
        "distance": route.total_distance_meters,
        "route_span": route_span,
        "max_facility_gap": max_facility_gap,
        "total_facility_gap": total_facility_gap,
        "far_pair_penalty": far_pair_penalty,
        "morning_backtrack_penalty": morning_backtrack_penalty,
    }
    return route, stats


def evaluate_assignment(
    depot: Location,
    auditors: List[AuditorStop],
    groups: List[List[AuditorStop]],
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
    require_all_drivers_if_possible: bool,
) -> Tuple[Tuple[int, ...], List[DailyDriverRoute], Dict[str, Optional[int]]]:
    routes: List[DailyDriverRoute] = []
    stats_list: List[Dict[str, int]] = []
    for i, group in enumerate(groups):
        route, stats = evaluate_group_plan(depot, auditors, group, i + 1, time_matrix, distance_matrix)
        routes.append(route)
        if group:
            stats_list.append(stats)

    used_routes = [route for route in routes if route.auditors]
    drivers_used = len(used_routes)
    required_drivers = min(len(auditors), len(groups)) if require_all_drivers_if_possible else 0
    unused_required = max(0, required_drivers - drivers_used)

    max_auditor_wait = max((s["max_auditor_wait"] for s in stats_list), default=0)
    total_auditor_wait = sum(s["total_auditor_wait"] for s in stats_list)
    max_morning_early_wait = max((s["max_morning_early_wait"] for s in stats_list), default=0)
    total_morning_early_wait = sum(s["total_morning_early_wait"] for s in stats_list)
    max_afternoon_wait = max((s["max_afternoon_wait"] for s in stats_list), default=0)
    total_afternoon_wait = sum(s["total_afternoon_wait"] for s in stats_list)
    start_deficit = sum(s["start_deficit"] for s in stats_list)
    latest_return = max((s["return_depot"] for s in stats_list), default=0)
    total_drive = sum(s["drive_time"] for s in stats_list)
    total_distance = sum(s["distance"] for s in stats_list)
    max_route_span = max((s.get("route_span", 0) for s in stats_list), default=0)
    max_facility_gap = max((s.get("max_facility_gap", 0) for s in stats_list), default=0)
    total_facility_gap = sum(s.get("total_facility_gap", 0) for s in stats_list)
    far_pair_penalty = sum(s.get("far_pair_penalty", 0) for s in stats_list)
    morning_backtrack_penalty = sum(s.get("morning_backtrack_penalty", 0) for s in stats_list)

    # Auditor-first + compact-cluster + outbound-direction objective:
    # 1) use all drivers when requested
    # 2) keep auditor waiting in reasonable buckets, not exact seconds
    # 3) strongly avoid putting far-apart facilities in the same car
    # 4) prefer natural nearby clusters, e.g. Tarmeem + Al Taweelah instead of Dubai + Al Taweelah
    # 5) then reduce exact waiting, return time, and driver driving
    objective = (
        unused_required,
        objective_bucket(max_auditor_wait),
        far_pair_penalty,
        morning_backtrack_penalty,
        max_facility_gap,
        total_facility_gap,
        objective_bucket(total_auditor_wait),
        objective_bucket(max_morning_early_wait),
        objective_bucket(max_afternoon_wait),
        start_deficit,
        max_auditor_wait,
        total_auditor_wait,
        latest_return,
        total_drive,
        total_distance,
    )

    latest_common_start_candidates = [r.latest_morning_start_seconds for r in used_routes if r.latest_morning_start_seconds is not None]
    totals: Dict[str, Optional[int]] = {
        "auditors": len(auditors),
        "drivers_used": drivers_used,
        "driver_count": len(groups),
        "total_driving_time": total_drive,
        "total_distance": total_distance,
        "latest_common_start": min(latest_common_start_candidates) if latest_common_start_candidates else None,
        "latest_return": latest_return if used_routes else None,
        "max_auditor_wait": max_auditor_wait,
        "total_auditor_wait": total_auditor_wait,
        "max_morning_early_wait": max_morning_early_wait,
        "total_morning_early_wait": total_morning_early_wait,
        "max_pickup_wait": max_afternoon_wait,
        "total_pickup_wait": total_afternoon_wait,
        "morning_start_deficit": start_deficit,
        "max_facility_gap": max_facility_gap,
        "total_facility_gap": total_facility_gap,
        "far_pair_penalty": far_pair_penalty,
        "morning_backtrack_penalty": morning_backtrack_penalty,
    }
    return objective, routes, totals


def seed_initial_groups(
    auditors: List[AuditorStop],
    driver_count: int,
    time_matrix: List[List[int]],
    require_all_drivers_if_possible: bool,
) -> List[List[AuditorStop]]:
    groups: List[List[AuditorStop]] = [[] for _ in range(driver_count)]
    if not auditors:
        return groups

    # Start by spreading the farthest / most time-constrained auditors across drivers.
    ranked = sorted(
        auditors,
        key=lambda a: (
            -(time_matrix[0][auditor_node(auditors, a)] + time_matrix[auditor_node(auditors, a)][0]),
            a.arrival_deadline_seconds,
        ),
    )

    next_driver = 0
    if require_all_drivers_if_possible:
        for auditor in ranked[: min(driver_count, len(ranked))]:
            groups[next_driver].append(auditor)
            next_driver += 1
        remaining = ranked[min(driver_count, len(ranked)):]
    else:
        remaining = ranked

    for auditor in remaining:
        # Lightweight placement: put the auditor into the group that currently has the smallest
        # depot round-trip burden. Full local search improves this immediately after.
        best_driver = min(
            range(driver_count),
            key=lambda d: sum(time_matrix[0][auditor_node(auditors, a)] + time_matrix[auditor_node(auditors, a)][0] for a in groups[d]),
        )
        groups[best_driver].append(auditor)

    return groups


def assignment_is_allowed(labels: Tuple[int, ...], driver_count: int, required_drivers: int) -> bool:
    if required_drivers <= 0:
        return True
    return len(set(labels)) >= required_drivers


def exhaustive_assignment_search(
    depot: Location,
    auditors: List[AuditorStop],
    driver_count: int,
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
    require_all_drivers_if_possible: bool,
    max_assignments: int = 250_000,
) -> Optional[Tuple[List[List[AuditorStop]], Tuple[int, ...], List[DailyDriverRoute], Dict[str, Optional[int]]]]:
    """Try every driver assignment for small jobs.

    Seven auditors and four drivers is only 4^7 = 16,384 assignments, which is
    small enough. This prevents the local search from getting stuck in a
    driver-time local optimum and makes the result match the auditor-priority
    objective much more reliably.
    """
    count = driver_count ** len(auditors)
    if count > max_assignments:
        return None

    required_drivers = min(len(auditors), driver_count) if require_all_drivers_if_possible else 0
    best = None
    for labels in itertools.product(range(driver_count), repeat=len(auditors)):
        if not assignment_is_allowed(labels, driver_count, required_drivers):
            continue
        groups: List[List[AuditorStop]] = [[] for _ in range(driver_count)]
        for auditor, driver_idx in zip(auditors, labels):
            groups[driver_idx].append(auditor)
        objective, routes, totals = evaluate_assignment(
            depot, auditors, groups, time_matrix, distance_matrix, require_all_drivers_if_possible
        )
        if best is None or objective < best[0]:
            best = (objective, groups, routes, totals)

    if best is None:
        return None
    objective, groups, routes, totals = best
    return groups, objective, routes, totals


def improve_assignment(
    depot: Location,
    auditors: List[AuditorStop],
    groups: List[List[AuditorStop]],
    time_matrix: List[List[int]],
    distance_matrix: List[List[int]],
    require_all_drivers_if_possible: bool,
) -> Tuple[List[List[AuditorStop]], Tuple[int, ...], List[DailyDriverRoute], Dict[str, Optional[int]]]:
    best_objective, best_routes, best_totals = evaluate_assignment(
        depot, auditors, groups, time_matrix, distance_matrix, require_all_drivers_if_possible
    )

    required_drivers = min(len(auditors), len(groups)) if require_all_drivers_if_possible else 0

    improved = True
    passes = 0
    while improved and passes < 20:
        improved = False
        passes += 1

        # Try moving one auditor to another driver.
        for src in range(len(groups)):
            for auditor in list(groups[src]):
                if len(groups[src]) <= 1 and sum(1 for g in groups if g) <= required_drivers:
                    continue
                for dst in range(len(groups)):
                    if src == dst:
                        continue
                    candidate = [list(g) for g in groups]
                    candidate[src].remove(auditor)
                    candidate[dst].append(auditor)
                    objective, routes, totals = evaluate_assignment(
                        depot, auditors, candidate, time_matrix, distance_matrix, require_all_drivers_if_possible
                    )
                    if objective < best_objective:
                        groups = candidate
                        best_objective, best_routes, best_totals = objective, routes, totals
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break

        if improved:
            continue

        # Try swapping auditors between drivers.
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                for auditor_a in list(groups[a]):
                    for auditor_b in list(groups[b]):
                        candidate = [list(g) for g in groups]
                        candidate[a].remove(auditor_a)
                        candidate[b].remove(auditor_b)
                        candidate[a].append(auditor_b)
                        candidate[b].append(auditor_a)
                        objective, routes, totals = evaluate_assignment(
                            depot, auditors, candidate, time_matrix, distance_matrix, require_all_drivers_if_possible
                        )
                        if objective < best_objective:
                            groups = candidate
                            best_objective, best_routes, best_totals = objective, routes, totals
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                break

    return groups, best_objective, best_routes, best_totals


def solve_daily_auditor_routes(
    depot: Location,
    auditors: List[AuditorStop],
    driver_count: int,
    require_all_drivers_if_possible: bool = True,
    solver_seconds: int = 30,
) -> Tuple[List[DailyDriverRoute], Dict[str, Optional[int]]]:
    """Build practical full-day routes for the auditor use case.

    This deliberately avoids the previous hard OR-Tools time-window model because that model
    can return "no feasible schedule" even when a usable business schedule exists. Here,
    morning arrival times are handled as "leave as late as possible" calculations, and the
    afternoon is optimized around minimizing how long auditors wait after they can leave.
    """
    del solver_seconds  # kept for backward compatibility with old calls

    if driver_count < 1:
        raise ValueError("Driver count must be at least 1.")
    if not auditors:
        raise ValueError("Add at least one auditor/facility.")

    physical_locations = [depot] + [auditor.facility for auditor in auditors]
    time_matrix, distance_matrix = compute_route_matrix(physical_locations)

    exhaustive = exhaustive_assignment_search(
        depot,
        auditors,
        driver_count,
        time_matrix,
        distance_matrix,
        require_all_drivers_if_possible,
    )
    if exhaustive is not None:
        _, _, routes, totals = exhaustive
    else:
        groups = seed_initial_groups(auditors, driver_count, time_matrix, require_all_drivers_if_possible)
        groups, _, routes, totals = improve_assignment(
            depot, auditors, groups, time_matrix, distance_matrix, require_all_drivers_if_possible
        )

    # Keep routes ordered by driver number and make sure empty drivers remain visible.
    routes = sorted(routes, key=lambda r: r.driver_number)
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
    cols = st.columns(6)
    cols[0].metric("Auditors", totals.get("auditors") or 0)
    cols[1].metric("Drivers used", f"{totals.get('drivers_used') or 0}/{totals.get('driver_count') or 0}")
    cols[2].metric("Latest depot return", format_clock(totals.get("latest_return")))
    cols[3].metric("Max early drop-off", format_duration(int(totals.get("max_morning_early_wait") or 0)))
    cols[4].metric("Total driving", format_duration(int(totals.get("total_driving_time") or 0)))
    cols[5].metric("Total distance", format_distance(int(totals.get("total_distance") or 0)))

    warnings = []
    for route in routes:
        if route.auditors and route.latest_morning_start_seconds is not None and route.latest_morning_start_seconds < 0:
            warnings.append(f"Driver {route.driver_number} would need to start before midnight to meet the morning arrival deadlines.")
        for auditor in route.pickup_order:
            pickup_time = route.pickup_times.get(auditor.label)
            if (
                pickup_time is not None
                and auditor.pickup_latest_seconds is not None
                and pickup_time > auditor.pickup_latest_seconds
            ):
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
                st.write(f"Morning start at latest: **{format_clock(route.latest_morning_start_seconds)}**")
            else:
                st.caption("No auditors assigned. This should only happen if there are more drivers than auditors, or if force-all-drivers is off.")
                continue

            if route.morning_google_url:
                render_google_maps_logo_button(route.morning_google_url)

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



def main() -> None:
    st.set_page_config(page_title="Multi-Driver Auditor Route Optimizer", page_icon="🚗", layout="wide")
    require_password_if_configured()
    reset_stale_session_state()

    st.title("Route Optimizer")

    with st.sidebar:
        st.header("Settings")
        st.write(f"Routing provider: **{ROUTING_PROVIDER}**")
        if ROUTING_PROVIDER == "osrm":
            st.caption("Using OSRM/OpenStreetMap road travel times. No live traffic and no paid Google API key.")
        else:
            st.caption("Using straight-line estimates only.")
        st.divider()
        st.caption("Morning arrival = when the auditor must reach the facility.")
        st.caption("Can leave = when the auditor can leave work, usually 16:00 or 17:00.")
        st.caption("Auditor-priority mode: use all drivers, keep auditor waiting low, then cluster nearby facilities before reducing driver driving.")

    defaults = default_stop_values()

    st.markdown("### Daily setup")
    top_cols = st.columns([1, 1, 1, 1, 2])
    with top_cols[0]:
        auditor_count = st.number_input("Number of auditors", min_value=1, max_value=39, value=7, step=1)
    with top_cols[1]:
        driver_count = st.number_input("Number of drivers", min_value=1, max_value=20, value=4, step=1)
    with top_cols[2]:
        default_arrive_by = datetime.time(8, 30)
    with top_cols[3]:
        default_can_leave = datetime.time(17, 0)
    with top_cols[4]:
        depot_input = st.text_input(
            "Office",
            value=st.session_state.get("depot_input", "24.485451, 54.381805"),
            placeholder="Office address, coordinates, or Google Maps link",
        )

    require_all_drivers = st.checkbox(
        "Use all drivers when possible",
        value=True,
        help="If auditors >= drivers, every driver is forced to handle at least one auditor.",
    )

    st.markdown("### Facilities' Locations")

    stop_rows: List[Tuple[str, int, int]] = []
    with st.form("daily_route_form"):
        header = st.columns([0.3, 4.0, 1, 1])
        header[0].markdown("**#**")
        header[1].markdown("**Facility Google Maps link / coordinates / address**")
        header[2].markdown("**Arrive by**")
        header[3].markdown("**Leave by**")

        for idx in range(1, int(auditor_count) + 1):
            row = st.columns([0.3, 4.0, 1, 1])
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
            stop_rows.append(
                (
                    raw,
                    seconds_from_time(arrival_time),
                    seconds_from_time(pickup_earliest),
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
                for idx, (raw, arrive_by, pickup_earliest) in enumerate(cleaned_rows, start=1):
                    facility = parse_location(raw, idx, label_prefix="Facility")
                    auditors.append(
                        AuditorStop(
                            label=f"Auditor {idx}",
                            facility=facility,
                            arrival_deadline_seconds=arrive_by,
                            pickup_earliest_seconds=pickup_earliest,
                            pickup_latest_seconds=None,
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
