import streamlit as st
import folium
from streamlit_folium import st_folium
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import openrouteservice
from heapq import nsmallest

# ----------------------------------------------------------------------------------------------------------------------
# GENERAL CONFIGURATION
# ----------------------------------------------------------------------------------------------------------------------
st.set_page_config(page_title="Optimal Route Planner in Valencia", page_icon="🗺️", layout="wide")

ORS_API_KEY = st.secrets["ORS_API_KEY"]
ors_client = openrouteservice.Client(key=ORS_API_KEY)

geolocator = Nominatim(user_agent="route_planner_valencia")

st.markdown("""
<style>
header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)
# ----------------------------------------------------------------------------------------------------------------------
# CACHED HELPERS
# ----------------------------------------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def geocode(address: str):
    """Return (lat, lon) tuple for a street address or None if not found."""
    loc = geolocator.geocode(address, timeout=10)
    return (loc.latitude, loc.longitude) if loc else None

@st.cache_data(ttl=600, show_spinner=False)
def get_json(url: str):
    """Download JSON from an open data endpoint and return list of records, empty list on error."""
    try:
        return requests.get(url, timeout=5).json().get("records", [])
    except Exception:
        return []

@st.cache_data(show_spinner=False)
def get_route(coords, profile="foot-walking"):
    """Call OpenRouteService and return a GeoJSON route for the given list of coordinates."""
    try:
        return ors_client.directions(coordinates=coords, profile=profile, format="geojson")
    except Exception:
        return None

# ----------------------------------------------------------------------------------------------------------------------
# HELPER FUNCTIONS
# ----------------------------------------------------------------------------------------------------------------------
def draw_route(m, route_geojson, color):
    """Add a colored route to the folium map."""
    folium.GeoJson(
        route_geojson,
        name="Route",
        style_function=lambda _: {"color": color, "weight": 5},
    ).add_to(m)

def find_closest(records, ref_coords):
    """Return the record whose geo_point_2d is closest (in meters) to ref_coords."""
    best, best_d = None, float("inf")
    for rec in records:
        fields = rec.get("fields", {})
        coords = fields.get("geo_point_2d")
        if coords:
            d = geodesic(ref_coords, coords).meters
            if d < best_d:
                best, best_d = fields, d
    return best

def traffic_penalty_seconds(route_geojson, traffic_records, radius_m=50):
    """
    Estimate extra travel time due to traffic around the route.
    For every 100 vehicles/hour average intensity near the path add 60 s.
    """
    coords = route_geojson["features"][0]["geometry"]["coordinates"]
    intensities = []
    for rec in traffic_records:
        fields = rec.get("fields", {})
        tcoord = fields.get("geo_point_2d")
        inten = fields.get("intensidad")
        if tcoord and inten is not None:
            try:
                inten = float(inten)
            except ValueError:
                continue
            # subsample coords for speed
            for lon, lat in coords[::5]:
                if geodesic((lat, lon), tcoord).meters < radius_m:
                    intensities.append(inten)
                    break
    if not intensities:
        return 0
    avg_inten = sum(intensities) / len(intensities)
    return (avg_inten / 100) * 60  # seconds

def k_closest(records, ref_coords, k=20):
    """Return k nearest stop field dicts to ref_coords based on straight-line distance."""
    return nsmallest(
        k,
        (rec["fields"] for rec in records if rec.get("fields", {}).get("geo_point_2d")),
        key=lambda f: geodesic(ref_coords, f["geo_point_2d"]).meters
    )

def common_lines(stop_a: dict, stop_b: dict) -> set[str]:
    """Return set of EMT bus lines that both stops share."""
    sa = {l.strip() for l in stop_a.get("lineas", "").split(",")}
    sb = {l.strip() for l in stop_b.get("lineas", "").split(",")}
    return sa & sb

# ----------------------------------------------------------------------------------------------------------------------
# LOAD DATA
# ----------------------------------------------------------------------------------------------------------------------
DATASETS = {
    "parkings": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=parkings&rows=1000",
    "valenbisi": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=valenbisi-disponibilitat-valenbisi-dsiponibilidad&rows=1000",
    "traffic": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=estat-transit-temps-real-estado-trafico-tiempo-real&rows=1000",
    "buses": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=emt&rows=5000",
}

parkings = get_json(DATASETS["parkings"])
bikes = get_json(DATASETS["valenbisi"])
traffic = get_json(DATASETS["traffic"])  # retained for possible future use
buses = get_json(DATASETS["buses"])

# ----------------------------------------------------------------------------------------------------------------------
# LAYOUT
# ----------------------------------------------------------------------------------------------------------------------
col_map, col_ui = st.columns([3, 2])

with col_ui:
    transport_mode = st.selectbox("Select transport mode", ["Walking", "Car", "Valenbisi", "Bus"])
    start_point = st.text_input("Start address", "Plaza del Ayuntamiento, Valencia")
    end_point = st.text_input("End address", "Ciudad de las Artes y las Ciencias, Valencia")

    use_parking = None
    if transport_mode == "Car":
        use_parking = st.radio(
            "Parking preference",
            ["Parking garage", "Street parking"],
            horizontal=True,
        )

input_signature = (transport_mode, start_point, end_point, use_parking)

# ----------------------------------------------------------------------------------------------------------------------
# ROUTE & MAP
# ----------------------------------------------------------------------------------------------------------------------
def build_map():
    """Compute route based on UI state and return folium map plus a notice string."""
    start_coords = geocode(start_point)
    end_coords = geocode(end_point)

    if not (start_coords and end_coords):
        st.error("Unable to geocode one of the addresses.")
        return folium.Map(location=[39.4699, -0.3763], zoom_start=12)  # default Valencia

    center = [(start_coords[0] + end_coords[0]) / 2, (start_coords[1] + end_coords[1]) / 2]
    m = folium.Map(location=center, zoom_start=14)
    folium.Marker(start_coords, tooltip="Start", icon=folium.Icon(color="green")).add_to(m)
    folium.Marker(end_coords, tooltip="End", icon=folium.Icon(color="red")).add_to(m)

    notice = ""

    # CAR MODE
    if transport_mode == "Car":
        def add_penalty(drive_route, base_seconds):
            """Apply traffic penalty to car segment and return (total_seconds, penalty_seconds)."""
            penalty = traffic_penalty_seconds(drive_route, traffic)
            return base_seconds + penalty, penalty

        if use_parking == "Parking garage":
            parking = find_closest(parkings, end_coords)
            if parking:
                folium.Marker(parking["geo_point_2d"], tooltip="Nearest Parking Garage",
                              icon=folium.Icon(color="blue")).add_to(m)
                # split journey: drive to garage then walk to final point
                r1 = get_route([start_coords[::-1], parking["geo_point_2d"][::-1]], profile="driving-car")
                r2 = get_route([parking["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
                if r1 and r2:
                    draw_route(m, r1, "blue");
                    draw_route(m, r2, "green")
                    secs_drive = r1["features"][0]["properties"]["summary"]["duration"]
                    secs_walk = r2["features"][0]["properties"]["summary"]["duration"]
                    total_secs, penalty = add_penalty(r1, secs_drive + secs_walk)
                    notice = f"Est. total time: {total_secs / 60:.1f} min  (traffic +{penalty / 60:.1f})"
        else:
            # street parking assumed at destination
            r = get_route([start_coords[::-1], end_coords[::-1]], profile="driving-car")
            if r:
                draw_route(m, r, "blue")
                secs = r["features"][0]["properties"]["summary"]["duration"]
                total_secs, penalty = add_penalty(r, secs)
                notice = f"Est. driving time: {total_secs / 60:.1f} min  (traffic +{penalty / 60:.1f})"

    # VALENBISI MODE
    elif transport_mode == "Valenbisi":
        st_start = find_closest(bikes, start_coords)
        st_end = find_closest(bikes, end_coords)
        if st_start and st_end:
            folium.Marker(st_start["geo_point_2d"], tooltip="Bike pickup",
                          icon=folium.Icon(color="orange")).add_to(m)
            folium.Marker(st_end["geo_point_2d"], tooltip="Bike dropoff",
                          icon=folium.Icon(color="purple")).add_to(m)

            # walk → bike → walk
            walk1 = get_route([start_coords[::-1], st_start["geo_point_2d"][::-1]], profile="foot-walking")
            bike = get_route([st_start["geo_point_2d"][::-1], st_end["geo_point_2d"][::-1]], profile="cycling-regular")
            walk2 = get_route([st_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")

            if walk1 and bike and walk2:
                for r, c in zip([walk1, bike, walk2], ["green", "orange", "green"]):
                    draw_route(m, r, c)
                dur = sum(r["features"][0]["properties"]["summary"]["duration"] for r in (walk1, bike, walk2))
                notice = f"Estimated total time: {dur/60:.1f} min"

    # WALKING MODE
    elif transport_mode == "Walking":
        r = get_route([start_coords[::-1], end_coords[::-1]], profile="foot-walking")
        if r:
            draw_route(m, r, "green")
            notice = f"Estimated walking time: {r['features'][0]['properties']['summary']['duration']/60:.1f} min"

    # BUS MODE
    elif transport_mode == "Bus":
        start_cand = k_closest(buses, start_coords)
        end_cand = k_closest(buses, end_coords)

        best_pair, best_line, best_score = None, None, float("inf")

        # search for pair of stops sharing a bus line that minimises walking distance
        for s in start_cand:
            for e in end_cand:
                lines = common_lines(s, e)
                if not lines:
                    continue
                walk_score = (geodesic(start_coords, s["geo_point_2d"]).meters +
                              geodesic(end_coords, e["geo_point_2d"]).meters)
                if walk_score < best_score:
                    best_pair, best_line, best_score = (s, e), next(iter(lines)), walk_score

        if not best_pair:
            return m, "There is no bus line connecting stops near the origin and destination."

        stop_start, stop_end = best_pair

        # mark chosen stops on map
        for stop, tip in ((stop_start, "start"), (stop_end, "end")):
            folium.Marker(stop["geo_point_2d"],  tooltip=f"{tip.capitalize()} stop · LINE {best_line}",
                          icon=folium.Icon(color="lightblue")).add_to(m)

        # walk legs and bus leg
        walk1 = get_route([start_coords[::-1], stop_start["geo_point_2d"][::-1]], profile="foot-walking")
        walk2 = get_route([stop_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
        bus_r = get_route([stop_start["geo_point_2d"][::-1], stop_end["geo_point_2d"][::-1]], profile="driving-car")

        if walk1 and bus_r and walk2:
            draw_route(m, walk1, "green")
            draw_route(m, walk2, "green")

            folium.GeoJson(bus_r, name=f"Línea {best_line}", style_function=lambda _: {"color": "red", "weight": 6},
                tooltip=f"Línea {best_line}").add_to(m)

            # add average waiting time at stop (7 min)
            dur = (sum(r["features"][0]["properties"]["summary"]["duration"] for r in (walk1, bus_r, walk2)) + 7 * 60)  # espera media en la parada
            notice = f"Estimated time with **line {best_line}**: {dur / 60:.1f} min"

    return m, notice

# ----------------------------------------------------------------------------------------------------------------------
# SESSION STATE
# ----------------------------------------------------------------------------------------------------------------------
# rebuild map only when inputs change
if "last_inputs" not in st.session_state or st.session_state["last_inputs"] != input_signature:
    map, notice = build_map()
    st.session_state.update(
        map=map,
        notice=notice,
        last_inputs=input_signature
    )

# ----------------------------------------------------------------------------------------------------------------------
# LAYOUT 2
# ----------------------------------------------------------------------------------------------------------------------
with col_ui:
    if st.session_state.get("notice"):
        st.success(st.session_state["notice"])

with col_map:
    st_folium(st.session_state["map"], use_container_width=True, height=800, key="route_map")