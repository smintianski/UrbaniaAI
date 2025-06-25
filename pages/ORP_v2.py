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
st.set_page_config(page_title="Optimal Route Planner in Valencia",
                   page_icon="🗺️", layout="wide",
                   initial_sidebar_state="expanded")

ORS_API_KEY = st.secrets["ORS_API_KEY"]
ors_client = openrouteservice.Client(key=ORS_API_KEY)

geolocator = Nominatim(user_agent="route_planner_valencia")

st.markdown(
    """
    <style>
    header { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True
)

# ----------------------------------------------------------------------------------------------------------------------
# CACHED HELPERS (solo datos relativamente estáticos)
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


# ----------------------------------------------------------------------------------------------------------------------
# SESSION-BASED HELPERS (rutas y marcadores – nunca cache)
# ----------------------------------------------------------------------------------------------------------------------
def _route_key(coords, profile):
    """Genera una clave hashable y breve para una consulta de ruta."""
    return f"{profile}_{tuple(round(c, 5) for pair in coords for c in pair)}"


def get_route(coords, profile="foot-walking"):
    """
    Devuelve la ruta GeoJSON desde/para coords y profile.

    * Se almacena en st.session_state["routes"] usando una clave única.
    * Si la clave ya existe se reusa, evitamos otra llamada a la API.
    """
    if "routes" not in st.session_state:
        st.session_state["routes"] = {}

    key = _route_key(coords, profile)
    if key in st.session_state["routes"]:
        return st.session_state["routes"][key]

    try:
        route = ors_client.directions(coordinates=coords,
                                      profile=profile,
                                      format="geojson")
    except Exception:
        route = None

    st.session_state["routes"][key] = route
    return route


def store_marker(latlon, tooltip, color):
    """
    Guarda la info necesaria de cada marcador en `st.session_state["markers"]`
    y devuelve el objeto folium.Marker para añadirlo al mapa.
    """
    if "markers" not in st.session_state:
        st.session_state["markers"] = []

    st.session_state["markers"].append(
        {"coords": latlon, "tooltip": tooltip, "color": color}
    )
    return folium.Marker(latlon, tooltip=tooltip,
                         icon=folium.Icon(color=color))


# ----------------------------------------------------------------------------------------------------------------------
# LOAD DATA (vía cache, porque son open-data que apenas cambian)
# ----------------------------------------------------------------------------------------------------------------------
DATASETS = {
    "parkings": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=parkings&rows=1000",
    "valenbisi": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=valenbisi-disponibilitat-valenbisi-dsiponibilidad&rows=1000",
    "traffic": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=estat-transit-temps-real-estado-trafico-tiempo-real&rows=1000",
    "buses": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=emt&rows=5000",
}

parkings = get_json(DATASETS["parkings"])
bikes = get_json(DATASETS["valenbisi"])
traffic = get_json(DATASETS["traffic"])
buses = get_json(DATASETS["buses"])

# ----------------------------------------------------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------------------------------------------------
col_map, col_ui = st.columns([3, 2])

with col_ui:
    transport_mode = st.selectbox(
        "Select transport mode", ["Walking", "Car", "Valenbisi", "Bus"]
    )
    start_point = st.text_input(
        "Start address", "Plaza del Ayuntamiento, Valencia")
    end_point = st.text_input(
        "End address", "Ciudad de las Artes y las Ciencias, Valencia")

    use_parking = None
    if transport_mode == "Car":
        use_parking = st.radio(
            "Parking preference",
            ["Parking garage", "Street parking"],
            horizontal=True,
        )

input_signature = (transport_mode, start_point, end_point, use_parking)

# ----------------------------------------------------------------------------------------------------------------------
# MAP BUILDER
# ----------------------------------------------------------------------------------------------------------------------
def draw_route(m, route_geojson, color):
    """Add a colored route to the folium map."""
    folium.GeoJson(
        route_geojson,
        name="Route",
        style_function=lambda _: {"color": color, "weight": 5},
    ).add_to(m)


def build_map():
    # Inicializamos las listas para la ejecución actual
    local_markers = []
    local_routes = []

    start_coords = geocode(start_point)
    end_coords = geocode(end_point)

    if not (start_coords and end_coords):
        st.error("Unable to geocode one of the addresses.")
        return folium.Map(location=[39.4699, -0.3763], zoom_start=12), ""

    center = [(start_coords[0] + end_coords[0]) / 2,
              (start_coords[1] + end_coords[1]) / 2]
    m = folium.Map(location=center, zoom_start=14)

    # --- Marcadores iniciales ---
    m_start = store_marker(start_coords, "Start", "green")
    m_end = store_marker(end_coords, "End", "red")
    m_start.add_to(m)
    m_end.add_to(m)

    notice = ""

    # --------------------------- CAR ---------------------------
    if transport_mode == "Car":
        def add_penalty(drive_route, base_seconds):
            """Apply traffic penalty to car segment and return (total_seconds, penalty_seconds)."""
            penalty = traffic_penalty_seconds(drive_route, traffic)
            return base_seconds + penalty, penalty

        if use_parking == "Parking garage":
            parking = find_closest(parkings, end_coords)
            if parking:
                store_marker(parking["geo_point_2d"],
                             "Nearest Parking Garage", "blue").add_to(m)

                r1 = get_route(
                    [start_coords[::-1], parking["geo_point_2d"][::-1]], profile="driving-car")
                r2 = get_route(
                    [parking["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
                if r1 and r2:
                    for r in (r1, r2):
                        local_routes.append(r)
                    draw_route(m, r1, "blue")
                    draw_route(m, r2, "green")

                    secs_drive = r1["features"][0]["properties"]["summary"]["duration"]
                    secs_walk = r2["features"][0]["properties"]["summary"]["duration"]
                    total_secs, penalty = add_penalty(
                        r1, secs_drive + secs_walk)
                    notice = f"Est. total time: {total_secs / 60:.1f} min  (traffic +{penalty / 60:.1f})"
        else:
            r = get_route([start_coords[::-1], end_coords[::-1]],
                          profile="driving-car")
            if r:
                local_routes.append(r)
                draw_route(m, r, "blue")
                secs = r["features"][0]["properties"]["summary"]["duration"]
                total_secs, penalty = add_penalty(r, secs)
                notice = f"Est. driving time: {total_secs / 60:.1f} min  (traffic +{penalty / 60:.1f})"

    # ------------------------ VALENBISI ------------------------
    elif transport_mode == "Valenbisi":
        st_start = find_closest(bikes, start_coords)
        st_end = find_closest(bikes, end_coords)
        if st_start and st_end:
            store_marker(st_start["geo_point_2d"],
                         "Bike pickup", "orange").add_to(m)
            store_marker(st_end["geo_point_2d"],
                         "Bike drop-off", "purple").add_to(m)

            walk1 = get_route(
                [start_coords[::-1], st_start["geo_point_2d"][::-1]], profile="foot-walking")
            bike = get_route(
                [st_start["geo_point_2d"][::-1], st_end["geo_point_2d"][::-1]], profile="cycling-regular")
            walk2 = get_route(
                [st_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")

            if walk1 and bike and walk2:
                local_routes.extend([walk1, bike, walk2])
                for r, c in zip([walk1, bike, walk2], [
                                "green", "orange", "green"]):
                    draw_route(m, r, c)

                dur = sum(r["features"][0]["properties"]
                          ["summary"]["duration"] for r in (walk1, bike, walk2))
                notice = f"Estimated total time: {dur/60:.1f} min"

    # ------------------------- WALKING -------------------------
    elif transport_mode == "Walking":
        r = get_route([start_coords[::-1], end_coords[::-1]],
                      profile="foot-walking")
        if r:
            local_routes.append(r)
            draw_route(m, r, "green")
            notice = f"Estimated walking time: {r['features'][0]['properties']['summary']['duration']/60:.1f} min"

    # --------------------------- BUS ----------------------------
    elif transport_mode == "Bus":
        start_cand = k_closest(buses, start_coords)
        end_cand = k_closest(buses, end_coords)

        best_pair, best_line, best_score = None, None, float("inf")
        for s in start_cand:
            for e in end_cand:
                lines = common_lines(s, e)
                if not lines:
                    continue
                walk_score = (geodesic(start_coords, s["geo_point_2d"]).meters +
                              geodesic(end_coords, e["geo_point_2d"]).meters)
                if walk_score < best_score:
                    best_pair, best_line, best_score = (
                        s, e), next(iter(lines)), walk_score

        if not best_pair:
            return m, "There is no bus line connecting stops near the origin and destination."

        stop_start, stop_end = best_pair

        # Stops
        store_marker(stop_start["geo_point_2d"],
                     f"Start stop · L{best_line}", "lightblue").add_to(m)
        store_marker(stop_end["geo_point_2d"],
                     f"End stop · L{best_line}", "lightblue").add_to(m)

        walk1 = get_route(
            [start_coords[::-1], stop_start["geo_point_2d"][::-1]], profile="foot-walking")
        walk2 = get_route(
            [stop_end["geo_point_2d"][::-1], end_coords[::-1]], profile="foot-walking")
        bus_r = get_route(
            [stop_start["geo_point_2d"][::-1],
             stop_end["geo_point_2d"][::-1]], profile="driving-car")

        if walk1 and bus_r and walk2:
            local_routes.extend([walk1, bus_r, walk2])

            draw_route(m, walk1, "green")
            draw_route(m, walk2, "green")
            folium.GeoJson(bus_r, name=f"Línea {best_line}",
                           style_function=lambda _: {"color": "red", "weight": 6},
                           tooltip=f"Línea {best_line}").add_to(m)

            dur = (sum(r["features"][0]["properties"]["summary"]["duration"]
                       for r in (walk1, bus_r, walk2)) + 7 * 60)
            notice = f"Estimated time with **line {best_line}**: {dur / 60:.1f} min"

    # --- Guardamos los resultados de esta ejecución ---
    st.session_state["markers_info"] = st.session_state.get(
        "markers", [])          # solo info → serializable
    st.session_state["routes_info"] = local_routes              # geojson raw

    return m, notice

# ----------------------------------------------------------------------------------------------------------------------
# SESSION STATE – actualizamos si cambian los inputs
# ----------------------------------------------------------------------------------------------------------------------
if ("last_inputs" not in st.session_state or
        st.session_state["last_inputs"] != input_signature):

    # Limpiamos rutas y marcadores previos
    st.session_state.pop("routes", None)
    st.session_state.pop("markers", None)
    st.session_state.pop("markers_info", None)
    st.session_state.pop("routes_info", None)

    # Generamos nuevo mapa
    map_obj, notice_msg = build_map()

    st.session_state.update(
        map=map_obj,
        notice=notice_msg,
        last_inputs=input_signature
    )

# ----------------------------------------------------------------------------------------------------------------------
# LAYOUT (notificaciones + mapa)
# ----------------------------------------------------------------------------------------------------------------------
with col_ui:
    if st.session_state.get("notice"):
        st.success(st.session_state["notice"])

with col_map:
    st_folium(st.session_state["map"], use_container_width=True,
              height=800, key="route_map")
