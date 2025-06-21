import streamlit as st
import pandas as pd
import folium
from folium.plugins import MarkerCluster
import requests
import json

# Page configuration
st.set_page_config(page_title="Valencia City Data", layout="wide")

# API endpoints
DATASETS = {
    "parkings": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=parkings&rows=1000",
    "valenbisi": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=valenbisi-disponibilitat-valenbisi-dsiponibilidad&rows=1000",
    "traffic": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=estat-transit-temps-real-estado-trafico-tiempo-real&rows=1000",
    "buses": "https://valencia.opendatasoft.com/api/records/1.0/search/?dataset=emt&rows=5000",
}

# Function to get data from API
@st.cache_data(ttl=300, show_spinner=False)  # Cache for 5 minutes
def get_json(url):
    """Fetch data from API and return as DataFrame"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Extract records from the API response
        records = data.get('records', [])
        if not records:
            return pd.DataFrame()

        # Convert to DataFrame
        rows = []
        for record in records:
            fields = record.get('fields', {})
            geometry = record.get('geometry', {})

            # Add geometry coordinates if available
            if geometry and 'coordinates' in geometry:
                coords = geometry['coordinates']
                fields['geo_point_2d'] = f"{coords[1]}, {coords[0]}"  # lat, lon format

            rows.append(fields)

        return pd.DataFrame(rows)
    except Exception as e:
        st.error(f"Error al cargar datos de {url}: {e}")
        return pd.DataFrame()


# Load data from APIs
@st.cache_data(ttl=300, show_spinner=False)
def load_data():
    parkings = get_json(DATASETS["parkings"])
    bikes = get_json(DATASETS["valenbisi"])
    traffic = get_json(DATASETS["traffic"])
    buses = get_json(DATASETS["buses"])
    return buses, bikes, traffic, parkings


# Load data
with st.spinner("Cargando datos en tiempo real..."):
    emt_df, bici_df, traffic_df, parkings_df = load_data()

# Check if data was loaded successfully
if emt_df.empty or bici_df.empty or traffic_df.empty or parkings_df.empty:
    st.error("Error al cargar algunos datos. Por favor, recarga la página.")
    st.stop()

# Prepare data subsets
# Filter EMT stops that are out of service (suprimidas) for tab 2
if 'suprimida' in emt_df.columns:
    emt_suppressed_df = emt_df[emt_df['suprimida'] == 1].copy()
else:
    emt_suppressed_df = pd.DataFrame()

# Define helper function to get lat, lon from geo_point string
def parse_geo_point(point_str):
    """Parse a 'lat, lon' string into a (lat, lon) tuple of floats."""
    try:
        if pd.isna(point_str):
            return None
        lat_str, lon_str = str(point_str).split(',')
        return float(lat_str.strip()), float(lon_str.strip())
    except Exception:
        return None


# Create the tab layout
tab1, tab2, tab3 = st.tabs(["🌍 Mapa principal", "🚫 Paradas EMT suprimidas", "🚲 Valenbisi"])

# --- Tab 1: Main Map ---
with tab1:
    # Build the main map with folium
    # Use a central point in Valencia for initial view (e.g., Plaza del Ayuntamiento coordinates)
    valencia_center = [39.4399, -0.3763]
    main_map = folium.Map(location=valencia_center, zoom_start=13)

    # Layer: EMT stops (clustered markers) - OFF by default
    emt_layer = MarkerCluster(name="Paradas EMT", control=True, show=False)
    for _, row in emt_df.iterrows():
        coords = parse_geo_point(row.get('geo_point_2d'))
        if not coords:
            continue
        lat, lon = coords

        # Use a bus icon for bus stops
        icon = folium.Icon(color="red", icon="bus", prefix="fa")

        # Tooltip with stop name and lines
        stop_name = str(row.get('denominacion', 'Parada EMT'))
        lines = str(row.get('lineas', ''))
        tooltip = f"{stop_name} - Líneas: {lines}"

        folium.Marker(location=[lat, lon], icon=icon, tooltip=tooltip).add_to(emt_layer)
    emt_layer.add_to(main_map)

    # Layer: Valenbisi stations (colored circle markers by bikes availability)
    bici_layer = folium.FeatureGroup(name="Estaciones Valenbisi", show=False)
    for _, row in bici_df.iterrows():
        coords = parse_geo_point(row.get('geo_point_2d'))
        if not coords:
            continue
        lat, lon = coords

        bikes = 0
        try:
            bikes = int(row.get('available', 0))
        except (ValueError, TypeError):
            bikes = 0

        # Color coding for bikes available
        if bikes == 0:
            color = 'red'
        elif bikes <= 2:
            color = 'orange'
        else:
            color = 'green'

        # Circle marker with tooltip showing station info
        station_name = str(row.get('address', 'Estación Valenbisi'))
        free_slots = 0
        try:
            free_slots = int(row.get('free', 0))
        except (ValueError, TypeError):
            free_slots = 0
        tooltip = f"{station_name} - {bikes} bicis, {free_slots} libres"

        folium.CircleMarker(location=[lat, lon], radius=6, color=color, fill=True,
                            fill_color=color, fill_opacity=0.8, tooltip=tooltip).add_to(bici_layer)
    bici_layer.add_to(main_map)

    # Layer: Traffic status (lines with color by state) - ON by default
    traffic_layer = folium.FeatureGroup(name="Tráfico (estado)", show=True)
    for _, row in traffic_df.iterrows():
        geo_shape = row.get('geo_shape')
        if pd.isna(geo_shape):
            continue

        try:
            if isinstance(geo_shape, str):
                shape = json.loads(geo_shape)
            else:
                shape = geo_shape
        except:
            continue

        coords_list = shape.get('coordinates', [])
        if not coords_list:
            continue

        # Each coords_list is a LineString (list of [lon, lat])
        line_coords = [(pt[1], pt[0]) for pt in coords_list]

        state = 0
        try:
            state = int(row.get('estado', 0))
        except (ValueError, TypeError):
            state = 0

        # Determine line color based on traffic state
        if state == 0:
            line_color = "green"
        elif state == 1:
            line_color = "orange"
        elif state >= 2:
            line_color = "red"
        else:
            line_color = "gray"

        folium.PolyLine(locations=line_coords, color=line_color, weight=5, opacity=0.7).add_to(traffic_layer)
    traffic_layer.add_to(main_map)

    # Layer: Public parkings (markers with 'P' icon) - ON by default
    parking_layer = folium.FeatureGroup(name="Parkings públicos", show=True)
    for _, row in parkings_df.iterrows():
        coords = parse_geo_point(row.get('geo_point_2d'))
        if not coords:
            continue
        lat, lon = coords

        parking_name = str(row.get('nombre', 'Parking'))
        total_spots = row.get('plazastota', 'N/D')

        # Safe conversion to int
        try:
            if pd.notna(total_spots) and str(total_spots).strip() != '':
                total_spots = int(float(str(total_spots)))
            else:
                total_spots = "N/D"
        except (ValueError, TypeError):
            total_spots = "N/D"

        icon = folium.Icon(color="blue", icon="car", prefix="fa")
        popup_text = f"{parking_name} - {total_spots} plazas totales"

        folium.Marker(location=[lat, lon], icon=icon, popup=popup_text, tooltip=parking_name).add_to(parking_layer)
    parking_layer.add_to(main_map)

    # Add a layer control to toggle layers
    folium.LayerControl(collapsed=False).add_to(main_map)

    # Display the map
    st.components.v1.html(main_map._repr_html_(), height=760)

# --- Tab 2: Suppressed EMT Stops ---
with tab2:
    if emt_suppressed_df.empty:
        st.info("Actualmente no hay paradas suprimidas.")
    else:
        # Display a table of suppressed stops
        display_data = []
        for _, row in emt_suppressed_df.iterrows():
            stop_id = row.get('id_parada', 'N/D')
            stop_name = row.get('denominacion', 'N/D')
            lines = row.get('lineas', 'N/D')

            display_data.append({
                'ID Parada': stop_id,
                'Denominación': stop_name,
                'Líneas': lines
            })

        # Show table
        df_display = pd.DataFrame(display_data)
        st.dataframe(df_display, use_container_width=True)

        # Create a map showing these stops
        supp_map = folium.Map(location=valencia_center, zoom_start=12)
        for _, row in emt_suppressed_df.iterrows():
            coords = parse_geo_point(row.get('geo_point_2d'))
            if not coords:
                continue
            lat, lon = coords

            stop_name = str(row.get('denominacion', 'Parada suprimida'))

            folium.Marker(location=[lat, lon],
                          icon=folium.Icon(color="red", icon="ban", prefix="fa"),
                          tooltip=f"{stop_name} (Fuera de servicio)").add_to(supp_map)

        folium.LayerControl().add_to(supp_map)

        # Show the map
        st.components.v1.html(supp_map._repr_html_(), height=760)

# --- Tab 3: Valenbisi Stations ---
with tab3:
    # User selection for coloring criterion
    criterion = st.radio("Colorear por:", options=["Bicis disponibles", "Espacios libres"], index=0)

    # Explain color meaning for the chosen criterion
    if criterion == "Bicis disponibles":
        st.markdown("**Colores:** rojo = 0 bicis disponibles, naranja = 1-2 bicis, verde = 3 o más bicis.")
    else:
        st.markdown("**Colores:** rojo = 0 espacios libres, naranja = 1-2 espacios, verde = 3 o más espacios libres.")

    # Build map of Valenbisi stations
    valenbisi_map = folium.Map(location=valencia_center, zoom_start=13)

    for _, row in bici_df.iterrows():
        coords = parse_geo_point(row.get('geo_point_2d'))
        if not coords:
            continue
        lat, lon = coords

        bikes = 0
        free = 0
        try:
            bikes = int(row.get('available', 0))
        except (ValueError, TypeError):
            bikes = 0
        try:
            free = int(row.get('free', 0))
        except (ValueError, TypeError):
            free = 0

        # Determine value based on selected criterion
        if criterion == "Bicis disponibles":
            value = bikes
        else:
            value = free

        # Color based on value thresholds
        if value == 0:
            color = 'red'
        elif value <= 2:
            color = 'orange'
        else:
            color = 'green'

        # Tooltip with station details
        station = str(row.get('adress', 'Estación Valenbisi'))
        tooltip = f"{station} - {bikes} bicis, {free} libres"

        folium.CircleMarker(location=[lat, lon], radius=7, color=color, fill=True,
                            fill_color=color, fill_opacity=0.9, tooltip=tooltip).add_to(valenbisi_map)

    # Show the map of Valenbisi
    st.components.v1.html(valenbisi_map._repr_html_(), height=625)