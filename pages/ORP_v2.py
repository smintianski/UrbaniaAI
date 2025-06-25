import streamlit as st
import folium
from streamlit_folium import st_folium
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import openrouteservice
from heapq import nsmallest
import hashlib
import time

# ----------------------------------------------------------------------------------------------------------------------
# GENERAL CONFIGURATION
# ----------------------------------------------------------------------------------------------------------------------
st.set_page_config(page_title="Optimal Route Planner in Valencia", page_icon="🗺️", layout="wide", initial_sidebar_state="expanded")

ORS_API_KEY = st.secrets["ORS_API_KEY"]
ors_client = openrouteservice.Client(key=ORS_API_KEY)

geolocator = Nominatim(user_agent="route_planner_valencia")

st.markdown("""
<style>
header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------------------------------------------------------------
# CACHED HELPERS (MEJORADOS PARA STREAMLIT CLOUD)
# ----------------------------------------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)  # TTL más largo
def geocode(address: str):
    """Return (lat, lon) tuple for a street address or None if not found."""
    try:
        loc = geolocator.geocode(address, timeout=15)  # Timeout más generoso
        return (loc.latitude, loc.longitude) if loc else None
    except Exception as e:
        st.error(f"Geocoding error: {str(e)}")
        return None

@st.cache_data(ttl=1800, show_spinner=False)  # TTL más conservador
def get_json(url: str):
    """Download JSON from an open data endpoint and return list of records, empty list on error."""
    try:
        response = requests.get(url, timeout=10)  # Timeout más generoso
        response.raise_for_status()
        return response.json().get("records", [])
    except Exception as e:
        st.warning(f"Error loading data from {url}: {str(e)}")
        return []

@st.cache_data(show_spinner=False, ttl=1800)
def get_route(coords, profile="foot-walking"):
    """Call OpenRouteService and return a GeoJSON route for the given list of coordinates."""
    try:
        result = ors_client.directions(coordinates=coords, profile=profile, format="geojson")
        return result
    except Exception as e:
        st.error(f"Route calculation error: {str(e)}")
        return None

# ----------------------------------------------------------------------------------------------------------------------
# HELPER FUNCTIONS
# ----------------------------------------------------------------------------------------------------------------------
def draw_route(m, route_geojson, color, name=None):
    """Add a colored route to the folium map."""
    if route_geojson and "features" in route_geojson:
        folium.GeoJson(
            route_geojson,
            name=name or "Route",
            style_function=lambda _: {"color": color, "weight": 5, "opacity": 0.8},
        ).add_to(m)

def find_closest(records, ref_coords):
    """Return the record whose geo_point_2d is closest (in meters) to ref_coords."""
    if not records or not ref_coords:
        return None
    
    best, best_d = None, float("inf")
    for rec in records:
        fields = rec.get("fields", {})
        coords = fields.get("geo_point_2d")
        if coords:
            try:
                d = geodesic(ref_coords, coords).meters
                if d < best_d:
                    best, best_d = fields, d
            except Exception:
                continue
    return best

def traffic_penalty_seconds(route_geojson, traffic_records, radius_m=50):
    """
    Estimate extra travel time due to traffic around the route.
    For every 100 vehicles/hour average intensity near the path add 60 s.
    """
    if not route_geojson or "features" not in route_geojson:
        return 0
    
    try:
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
    except Exception:
        return 0

def k_closest(records, ref_coords, k=20):
    """Return k nearest stop field dicts to ref_coords based on straight-line distance."""
    if not records or not ref_coords:
        return []
    
    valid_records = []
    for rec in records:
        fields = rec.get("fields", {})
        if fields.get("geo_point_2d"):
            valid_records.append(fields)
    
    if not valid_records:
        return []
    
    return nsmallest(
        k,
        valid_records,
        key=lambda f: geodesic(ref_coords, f["geo_point_2d"]).meters
    )

def common_lines(stop_a: dict, stop_b: dict) -> set[str]:
    """Return set of EMT bus lines that both stops share."""
    sa = {l.strip() for l in stop_a.get("lineas", "").split(",") if l.strip()}
    sb = {l.strip() for l in stop_b.get("lineas", "").split(",") if l.strip()}
    return sa & sb

# ----------------------------------------------------------------------------------------------------------------------
# GENERAR HASH PARA INPUTS (PARA MEJOR CONTROL DE ESTADO)
# ----------------------------------------------------------------------------------------------------------------------
def generate_input_hash(*args):
    """Generate a hash for input parameters to track changes"""
    combined = str(args)
    return hashlib.md5(combined.encode()).hexdigest()

# ----------------------------------------------------------------------------------------------------------------------
# LOAD DATA (CON MANEJO DE ERRORES MEJORADO)
# ----------------------------------------------------------------------------------------------------------------------
DATASETS = {
    "parkings": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=parkings&rows=1000",
    "valenbisi": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=valenbisi-disponibilitat-valenbisi-dsiponibilidad&rows=1000",
    "traffic": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=estat-transit-temps-real-estado-trafico-tiempo-real&rows=1000",
    "buses": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=emt&rows=5000",
}

# Inicializar datos con manejo de errores
@st.cache_data(ttl=1800)
def load_all_data():
    """Load all datasets with error handling"""
    data = {}
    for name, url in DATASETS.items():
        with st.spinner(f"Loading {name} data..."):
            data[name] = get_json(url)
    return data

try:
    all_data = load_all_data()
    parkings = all_data["parkings"]
    bikes = all_data["valenbisi"]
    traffic = all_data["traffic"]
    buses = all_data["buses"]
except Exception as e:
    st.error(f"Error loading data: {str(e)}")
    parkings = bikes = traffic = buses = []

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

# Generar hash único para los inputs
input_hash = generate_input_hash(transport_mode, start_point, end_point, use_parking)

# ----------------------------------------------------------------------------------------------------------------------
# CACHE ROUTE DATA (NO FOLIUM OBJECTS)
# ----------------------------------------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=1800)
def get_route_data(transport_mode, start_point, end_point, use_parking, input_hash):
    """Calculate route data (without folium objects) that can be cached safely."""
    start_coords = geocode(start_point)
    end_coords = geocode(end_point)

    if not (start_coords and end_coords):
        return {"error": "Unable to geocode one of the addresses.", "start_coords": None, "end_coords": None}

    result = {
        "start_coords": start_coords,
        "end_coords": end_coords,
        "routes": [],
        "markers": [],
        "notice": "",
        "error": None
    }

    try:
        # CAR MODE
        if transport_mode == "Car":
            def add_penalty(drive_route, base_seconds):
                penalty = traffic_penalty_seconds(drive_route, traffic)
                return base_seconds + penalty, penalty

            if use_parking == "Parking garage":
                parking = find_closest(parkings, end_coords)
                if parking:
                    result["markers"].append({
                        "coords": parking["geo_point_2d"],
                        "tooltip": "Nearest Parking Garage",
                        "color": "blue"
                    })
                    
                    r1 = get_route([start_coords[::-1], parking["geo_point_2d"][::-1]], profile="driving-car")
                    r2 = get_route([parking["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
                    
                    if r1 and r2:
                        result["routes"].extend([
                            {"geojson": r1, "color": "blue", "name": "Drive to parking"},
                            {"geojson": r2, "color": "green", "name": "Walk to destination"}
                        ])
                        secs_drive = r1["features"][0]["properties"]["summary"]["duration"]
                        secs_walk = r2["features"][0]["properties"]["summary"]["duration"]
                        total_secs, penalty = add_penalty(r1, secs_drive + secs_walk)
                        result["notice"] = f"Est. total time: {total_secs / 60:.1f} min  (traffic +{penalty / 60:.1f})"
                else:
                    result["notice"] = "No parking garage found near destination"
            else:
                r = get_route([start_coords[::-1], end_coords[::-1]], profile="driving-car")
                if r:
                    result["routes"].append({"geojson": r, "color": "blue", "name": "Driving route"})
                    secs = r["features"][0]["properties"]["summary"]["duration"]
                    total_secs, penalty = add_penalty(r, secs)
                    result["notice"] = f"Est. driving time: {total_secs / 60:.1f} min  (traffic +{penalty / 60:.1f})"

        # VALENBISI MODE  
        elif transport_mode == "Valenbisi":
            st_start = find_closest(bikes, start_coords)
            st_end = find_closest(bikes, end_coords)
            
            if st_start and st_end:
                result["markers"].extend([
                    {"coords": st_start["geo_point_2d"], "tooltip": "Bike pickup", "color": "orange"},
                    {"coords": st_end["geo_point_2d"], "tooltip": "Bike dropoff", "color": "purple"}
                ])

                walk1 = get_route([start_coords[::-1], st_start["geo_point_2d"][::-1]], profile="foot-walking")
                bike = get_route([st_start["geo_point_2d"][::-1], st_end["geo_point_2d"][::-1]], profile="cycling-regular")
                walk2 = get_route([st_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")

                if walk1 and bike and walk2:
                    result["routes"].extend([
                        {"geojson": walk1, "color": "green", "name": "Walk to bike"},
                        {"geojson": bike, "color": "orange", "name": "Bike route"},
                        {"geojson": walk2, "color": "green", "name": "Walk from bike"}
                    ])
                    dur = sum(r["features"][0]["properties"]["summary"]["duration"] for r in (walk1, bike, walk2))
                    result["notice"] = f"Estimated total time: {dur/60:.1f} min"
            else:
                result["notice"] = "No Valenbisi stations found near start/end points"

        # WALKING MODE
        elif transport_mode == "Walking":
            r = get_route([start_coords[::-1], end_coords[::-1]], profile="foot-walking")
            if r:
                result["routes"].append({"geojson": r, "color": "green", "name": "Walking route"})
                result["notice"] = f"Estimated walking time: {r['features'][0]['properties']['summary']['duration']/60:.1f} min"

        # BUS MODE
        elif transport_mode == "Bus":
            start_cand = k_closest(buses, start_coords)
            end_cand = k_closest(buses, end_coords)

            if not start_cand or not end_cand:
                result["notice"] = "No bus stops found near start/end points"
            else:
                best_pair, best_line, best_score = None, None, float("inf")

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
                    result["notice"] = "No bus line connects stops near origin and destination"
                else:
                    stop_start, stop_end = best_pair
                    
                    result["markers"].extend([
                        {"coords": stop_start["geo_point_2d"], "tooltip": f"Start stop · LINE {best_line}", "color": "lightblue"},
                        {"coords": stop_end["geo_point_2d"], "tooltip": f"End stop · LINE {best_line}", "color": "lightblue"}
                    ])

                    walk1 = get_route([start_coords[::-1], stop_start["geo_point_2d"][::-1]], profile="foot-walking")
                    walk2 = get_route([stop_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
                    bus_r = get_route([stop_start["geo_point_2d"][::-1], stop_end["geo_point_2d"][::-1]], profile="driving-car")

                    if walk1 and bus_r and walk2:
                        result["routes"].extend([
                            {"geojson": walk1, "color": "green", "name": "Walk to bus"},
                            {"geojson": walk2, "color": "green", "name": "Walk from bus"},
                            {"geojson": bus_r, "color": "red", "name": f"Línea {best_line}", "weight": 6}
                        ])
                        
                        dur = (sum(r["features"][0]["properties"]["summary"]["duration"] for r in (walk1, bus_r, walk2)) + 7 * 60)
                        result["notice"] = f"Estimated time with **line {best_line}**: {dur / 60:.1f} min"

    except Exception as e:
        result["error"] = f"Error calculating route: {str(e)}"
        result["notice"] = result["error"]
    
    return result

def build_map_from_data(route_data):
    """Build folium map from cached route data."""
    if route_data.get("error") and not route_data.get("start_coords"):
        return None, route_data["notice"]
    
    start_coords = route_data["start_coords"]
    end_coords = route_data["end_coords"]
    
    center = [(start_coords[0] + end_coords[0]) / 2, (start_coords[1] + end_coords[1]) / 2]
    m = folium.Map(location=center, zoom_start=14)
    
    # Add start/end markers
    folium.Marker(start_coords, tooltip="Start", icon=folium.Icon(color="green")).add_to(m)
    folium.Marker(end_coords, tooltip="End", icon=folium.Icon(color="red")).add_to(m)
    
    # Add additional markers
    for marker in route_data.get("markers", []):
        folium.Marker(
            marker["coords"], 
            tooltip=marker["tooltip"],
            icon=folium.Icon(color=marker["color"])
        ).add_to(m)
    
    # Add routes
    for route in route_data.get("routes", []):
        weight = route.get("weight", 5)
        if route["name"].startswith("Línea"):
            folium.GeoJson(
                route["geojson"],
                name=route["name"],
                style_function=lambda _: {"color": route["color"], "weight": weight, "opacity": 0.8},
                tooltip=route["name"]
            ).add_to(m)
        else:
            draw_route(m, route["geojson"], route["color"], route["name"])
    
    return m, route_data["notice"]

# ----------------------------------------------------------------------------------------------------------------------
# SESSION STATE MEJORADO
# ----------------------------------------------------------------------------------------------------------------------
# Inicializar session state si no existe
if "current_input_hash" not in st.session_state:
    st.session_state.current_input_hash = ""
    st.session_state.current_map = None
    st.session_state.current_notice = ""

# Solo reconstruir si los inputs cambiaron
if st.session_state.current_input_hash != input_hash:
    with st.spinner("Calculating route..."):
        # Get cached route data (serializable)
        route_data = get_route_data(transport_mode, start_point, end_point, use_parking, input_hash)
        
        # Build map from cached data (not cached itself)
        map_result, notice_result = build_map_from_data(route_data)
        
        st.session_state.current_input_hash = input_hash
        st.session_state.current_map = map_result
        st.session_state.current_notice = notice_result

# ----------------------------------------------------------------------------------------------------------------------
# DISPLAY MEJORADO
# ----------------------------------------------------------------------------------------------------------------------
with col_ui:
    if st.session_state.current_notice:
        if "Error" in st.session_state.current_notice or "Unable" in st.session_state.current_notice:
            st.error(st.session_state.current_notice)
        else:
            st.success(st.session_state.current_notice)

with col_map:
    if st.session_state.current_map:
        # Key único para evitar conflictos
        map_key = f"route_map_{input_hash[:8]}"
        st_folium(st.session_state.current_map, use_container_width=True, height=800, key=map_key)
    else:
        # Mapa por defecto si hay error
        default_map = folium.Map(location=[39.4699, -0.3763], zoom_start=12)
        st_folium(default_map, use_container_width=True, height=800, key="default_map")
