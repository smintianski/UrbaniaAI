import streamlit as st
import folium
from streamlit_folium import st_folium
import requests
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import openrouteservice
from heapq import nsmallest
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
# CACHED HELPERS
# ----------------------------------------------------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def geocode(address: str):
    """Return (lat, lon) tuple for a street address or None if not found."""
    try:
        loc = geolocator.geocode(address, timeout=10)
        return (loc.latitude, loc.longitude) if loc else None
    except Exception:
        return None

@st.cache_data(ttl=300, show_spinner=False)
def get_json(url: str):
    """Download JSON from an open data endpoint and return list of records, empty list on error."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json().get("records", [])
    except Exception:
        return []

@st.cache_data(show_spinner=False)
def get_route(coords, profile="foot-walking"):
    """Call OpenRouteService and return a GeoJSON route for the given list of coordinates."""
    try:
        # Add small delay to avoid rate limiting
        time.sleep(0.1)
        return ors_client.directions(coordinates=coords, profile=profile, format="geojson")
    except Exception as e:
        st.error(f"Error getting route: {str(e)}")
        return None

# ----------------------------------------------------------------------------------------------------------------------
# HELPER FUNCTIONS
# ----------------------------------------------------------------------------------------------------------------------
def draw_route(m, route_geojson, color, name="Route", weight=5):
    """Add a colored route to the folium map with proper styling."""
    if route_geojson and "features" in route_geojson and route_geojson["features"]:
        folium.GeoJson(
            route_geojson,
            name=name,
            style_function=lambda feature: {
                "color": color, 
                "weight": weight,
                "opacity": 0.8,
                "fillOpacity": 0.3
            },
            tooltip=name
        ).add_to(m)

def find_closest(records, ref_coords):
    """Return the record whose geo_point_2d is closest (in meters) to ref_coords."""
    if not records or not ref_coords:
        return None
    
    best, best_d = None, float("inf")
    for rec in records:
        fields = rec.get("fields", {})
        coords = fields.get("geo_point_2d")
        if coords and len(coords) == 2:
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
    if not route_geojson or not traffic_records:
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
# LOAD DATA
# ----------------------------------------------------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner="Loading Valencia data...")
def load_all_data():
    """Load all datasets at once to improve performance."""
    DATASETS = {
        "parkings": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=parkings&rows=1000",
        "valenbisi": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=valenbisi-disponibilitat-valenbisi-dsiponibilidad&rows=1000",
        "traffic": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=estat-transit-temps-real-estado-trafico-tiempo-real&rows=1000",
        "buses": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=emt&rows=5000",
    }
    
    return {
        "parkings": get_json(DATASETS["parkings"]),
        "bikes": get_json(DATASETS["valenbisi"]),
        "traffic": get_json(DATASETS["traffic"]),
        "buses": get_json(DATASETS["buses"]),
    }

# Load data
data = load_all_data()
parkings = data["parkings"]
bikes = data["bikes"]
traffic = data["traffic"]
buses = data["buses"]

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

# Create a unique key for the current inputs
input_key = f"{transport_mode}_{start_point}_{end_point}_{use_parking}"

# ----------------------------------------------------------------------------------------------------------------------
# ROUTE & MAP
# ----------------------------------------------------------------------------------------------------------------------
def build_map():
    """Compute route based on UI state and return folium map plus a notice string."""
    with st.spinner("Calculating route..."):
        start_coords = geocode(start_point)
        end_coords = geocode(end_point)

        if not start_coords:
            st.error(f"Could not find location: {start_point}")
            return folium.Map(location=[39.4699, -0.3763], zoom_start=12), "Error: Could not geocode start address"
        
        if not end_coords:
            st.error(f"Could not find location: {end_point}")
            return folium.Map(location=[39.4699, -0.3763], zoom_start=12), "Error: Could not geocode end address"

        # Calculate center point
        center = [(start_coords[0] + end_coords[0]) / 2, (start_coords[1] + end_coords[1]) / 2]
        
        # Create map with good zoom level
        distance_km = geodesic(start_coords, end_coords).kilometers
        if distance_km < 2:
            zoom = 15
        elif distance_km < 5:
            zoom = 14
        elif distance_km < 10:
            zoom = 13
        else:
            zoom = 12
        
        m = folium.Map(location=center, zoom_start=zoom)
        
        # Add start and end markers
        folium.Marker(
            start_coords, 
            tooltip="Start Point", 
            popup=start_point,
            icon=folium.Icon(color="green", icon="play")
        ).add_to(m)
        
        folium.Marker(
            end_coords, 
            tooltip="End Point", 
            popup=end_point,
            icon=folium.Icon(color="red", icon="stop")
        ).add_to(m)

        notice = ""

        # CAR MODE
        if transport_mode == "Car":
            def add_penalty(drive_route, base_seconds):
                """Apply traffic penalty to car segment and return (total_seconds, penalty_seconds)."""
                penalty = traffic_penalty_seconds(drive_route, traffic)
                return base_seconds + penalty, penalty

            if use_parking == "Parking garage":
                parking = find_closest(parkings, end_coords)
                if parking and parking.get("geo_point_2d"):
                    folium.Marker(
                        parking["geo_point_2d"], 
                        tooltip="Nearest Parking Garage",
                        popup=parking.get("nombre", "Parking"),
                        icon=folium.Icon(color="blue", icon="car")
                    ).add_to(m)
                    
                    # Drive to parking + walk to destination
                    r1 = get_route([start_coords[::-1], parking["geo_point_2d"][::-1]], profile="driving-car")
                    r2 = get_route([parking["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
                    
                    if r1 and r2:
                        draw_route(m, r1, "blue", "Driving", 6)
                        draw_route(m, r2, "green", "Walking", 4)
                        
                        secs_drive = r1["features"][0]["properties"]["summary"]["duration"]
                        secs_walk = r2["features"][0]["properties"]["summary"]["duration"]
                        total_secs, penalty = add_penalty(r1, secs_drive + secs_walk)
                        notice = f"🚗 Total time: {total_secs / 60:.1f} min (traffic penalty: +{penalty / 60:.1f} min)"
                    else:
                        notice = "⚠️ Could not calculate route to parking garage"
                else:
                    notice = "⚠️ No parking garages found near destination"
            else:
                # Direct driving
                r = get_route([start_coords[::-1], end_coords[::-1]], profile="driving-car")
                if r:
                    draw_route(m, r, "blue", "Driving", 6)
                    secs = r["features"][0]["properties"]["summary"]["duration"]
                    total_secs, penalty = add_penalty(r, secs)
                    notice = f"🚗 Driving time: {total_secs / 60:.1f} min (traffic penalty: +{penalty / 60:.1f} min)"
                else:
                    notice = "⚠️ Could not calculate driving route"

        # VALENBISI MODE
        elif transport_mode == "Valenbisi":
            st_start = find_closest(bikes, start_coords)
            st_end = find_closest(bikes, end_coords)
            
            if st_start and st_end and st_start.get("geo_point_2d") and st_end.get("geo_point_2d"):
                folium.Marker(
                    st_start["geo_point_2d"], 
                    tooltip="Bike Pickup Station",
                    popup=f"Available bikes: {st_start.get('available', 'N/A')}",
                    icon=folium.Icon(color="orange", icon="bicycle")
                ).add_to(m)
                
                folium.Marker(
                    st_end["geo_point_2d"], 
                    tooltip="Bike Return Station",
                    popup=f"Available slots: {st_end.get('available_slots', 'N/A')}",
                    icon=folium.Icon(color="purple", icon="bicycle")
                ).add_to(m)

                # Walk to bike + cycle + walk from bike
                walk1 = get_route([start_coords[::-1], st_start["geo_point_2d"][::-1]], profile="foot-walking")
                bike = get_route([st_start["geo_point_2d"][::-1], st_end["geo_point_2d"][::-1]], profile="cycling-regular")
                walk2 = get_route([st_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")

                if walk1 and bike and walk2:
                    draw_route(m, walk1, "green", "Walk to bike", 4)
                    draw_route(m, bike, "orange", "Cycling", 6)
                    draw_route(m, walk2, "green", "Walk from bike", 4)
                    
                    dur = sum(r["features"][0]["properties"]["summary"]["duration"] for r in (walk1, bike, walk2))
                    notice = f"🚲 Total time: {dur/60:.1f} min"
                else:
                    notice = "⚠️ Could not calculate Valenbisi route"
            else:
                notice = "⚠️ No Valenbisi stations found near start/end points"

        # WALKING MODE
        elif transport_mode == "Walking":
            r = get_route([start_coords[::-1], end_coords[::-1]], profile="foot-walking")
            if r:
                draw_route(m, r, "green", "Walking", 5)
                duration = r["features"][0]["properties"]["summary"]["duration"]
                distance = r["features"][0]["properties"]["summary"]["distance"]
                notice = f"🚶 Walking time: {duration/60:.1f} min ({distance/1000:.1f} km)"
            else:
                notice = "⚠️ Could not calculate walking route"

        # BUS MODE
        elif transport_mode == "Bus":
            start_cand = k_closest(buses, start_coords)
            end_cand = k_closest(buses, end_coords)

            if not start_cand or not end_cand:
                notice = "⚠️ No bus stops found near start/end points"
            else:
                best_pair, best_line, best_score = None, None, float("inf")

                # Find best bus connection
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
                    notice = "⚠️ No direct bus line found between nearby stops"
                else:
                    stop_start, stop_end = best_pair

                    # Add bus stop markers
                    folium.Marker(
                        stop_start["geo_point_2d"], 
                        tooltip=f"Start Bus Stop - Line {best_line}",
                        popup=stop_start.get("denominacion", "Bus Stop"),
                        icon=folium.Icon(color="lightblue", icon="bus")
                    ).add_to(m)
                    
                    folium.Marker(
                        stop_end["geo_point_2d"], 
                        tooltip=f"End Bus Stop - Line {best_line}",
                        popup=stop_end.get("denominacion", "Bus Stop"),
                        icon=folium.Icon(color="lightblue", icon="bus")
                    ).add_to(m)

                    # Calculate routes
                    walk1 = get_route([start_coords[::-1], stop_start["geo_point_2d"][::-1]], profile="foot-walking")
                    walk2 = get_route([stop_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
                    bus_r = get_route([stop_start["geo_point_2d"][::-1], stop_end["geo_point_2d"][::-1]], profile="driving-car")

                    if walk1 and bus_r and walk2:
                        draw_route(m, walk1, "green", "Walk to bus", 4)
                        draw_route(m, walk2, "green", "Walk from bus", 4)
                        draw_route(m, bus_r, "red", f"Bus Line {best_line}", 8)

                        # Add average waiting time (7 minutes)
                        walk_time = (walk1["features"][0]["properties"]["summary"]["duration"] + 
                                   walk2["features"][0]["properties"]["summary"]["duration"])
                        bus_time = bus_r["features"][0]["properties"]["summary"]["duration"]
                        total_time = walk_time + bus_time + 420  # +7 min wait
                        
                        notice = f"🚌 Total time with Line {best_line}: {total_time / 60:.1f} min (includes 7 min wait)"
                    else:
                        notice = f"⚠️ Could not calculate complete bus route for Line {best_line}"

        return m, notice

# ----------------------------------------------------------------------------------------------------------------------
# SESSION STATE & MAP DISPLAY
# ----------------------------------------------------------------------------------------------------------------------
# Only rebuild map when inputs change or calculate button is pressed
if ("last_input_key" not in st.session_state or 
    st.session_state["last_input_key"] != input_key or 
    calculate_route):
    
    map_obj, notice = build_map()
    st.session_state.update({
        "map": map_obj,
        "notice": notice,
        "last_input_key": input_key
    })

# Display results
with col_ui:
    if st.session_state.get("notice"):
        if "⚠️" in st.session_state["notice"] or "Error:" in st.session_state["notice"]:
            st.warning(st.session_state["notice"])
        else:
            st.success(st.session_state["notice"])

with col_map:
    if st.session_state.get("map"):
        st_folium(
            st.session_state["map"], 
            use_container_width=True, 
            height=800, 
            key="route_map",
            returned_objects=["last_object_clicked"]
        )
