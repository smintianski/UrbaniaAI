import json
import requests
import streamlit as st
from streamlit_folium import st_folium
import folium
from folium.plugins import LocateControl
import openrouteservice
import markdown
import random
import googlemaps
import polyline

# ----------------------------------------------------------------------------------------------------------------------
# GENERAL CONFIGURATION
# ----------------------------------------------------------------------------------------------------------------------
st.set_page_config(page_title="Valencia Tour & Recs", page_icon="🗺️", layout="wide")

DEBUGGING = False

# API Keys
PPL_API_KEY = st.secrets["PERPLEXITY_API_KEY"]
GMAPS_API_KEY = st.secrets["GMAPS_API_KEY"]

translations = {
    "search_placeholder": {"es": "¿Qué quieres buscar en Valencia?", "en": "What do you want to search in Valencia?"},
    "clear_button": {"es": "🗑️ Borrar Todo", "en": "🗑️ Clear All"},
    "assistant": {"es": "Pregúntame algo sobre Valencia", "en": "Ask me something about Valencia"},
}

# ----------------------------------------------------------------------------------------------------------------------
# SESSION STATE
# ----------------------------------------------------------------------------------------------------------------------
defaults = {
    "history": [{'assistant': translations["assistant"]["es"]}],  # List of dicts: {'user','assistant','sources'}
    "points": [],  # Accumulated markers [{'name','lat','lon'}]
    "routes": [],  # Accumulated routes
    "language": "es",
}
for k, v in defaults.items():
    st.session_state.setdefault(k, v)

def reset_history():
    st.session_state.history = [{
        'assistant': translations["assistant"][st.session_state.language]
    }]
    st.session_state.points = []
    st.session_state.routes = []
    st.rerun()

# ----------------------------------------------------------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------------------------------------------------------
PASSWORD = st.secrets["PASSWORD"]

# Initialize authentication state
if "authentication" not in st.session_state:
    st.session_state.authentication = False

# If not authenticated yet, show the login form
if not st.session_state.authentication:
    pwd = st.text_input("Enter password", type="password")
    if st.button("Enter"):
        if pwd == PASSWORD:
            st.session_state.authentication = True
            st.rerun()
        else:
            st.error("❌ Incorrect password")
    st.stop()  # Stop execution until correct password is entered

# ----------------------------------------------------------------------------------------------------------------------
# CSS STYLES FOR CHAT
# ----------------------------------------------------------------------------------------------------------------------
st.markdown("""
<style>
header { visibility: hidden; }

.user-message {
    background-color: #ff4b4b;
    color: white;
    padding: 12px 18px;
    border-radius: 20px 20px 5px 20px;
    margin: 10px 0 10px 30%;
    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
    word-wrap: break-word;
    text-align: left;
}

.assistant-message {
    background-color: #0068c9;
    color: white;
    padding: 12px 18px;
    border-radius: 20px 20px 20px 5px;
    margin: 10px 20% 5px 0;
    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
    word-wrap: break-word;
}

.sources-text {
    background-color: #e8f4f8;
    padding: 8px 12px;
    border-radius: 10px;
    margin: 5px 30% 15px 0;
    font-size: 0.8em;
    color: #0068c9;
}

.sources-text a {
    color: #0068c9;
    text-decoration: none;
}

.sources-text a:hover {
    text-decoration: underline;
}

.chat-container {
    max-height: 745px;
    overflow-y: auto;
    padding: 10px;
    margin-bottom: 15px;
    background-color: #F0F2F6;
    border-radius: 10px;
}

</style>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------------------------------------------------------------
# PROCESS QUERY WITH PERPLEXITY AND ORS
# ----------------------------------------------------------------------------------------------------------------------
def process_query(user_query):
    system_prompt = (
        "You are an assistant that searches for and responds only with locations in **Valencia, Spain.** "
        "Respond exclusively in **JSON** with the following keys: 'Points': an array of objects containing 'name' and 'address', "
        "'Comment': a Markdown-formatted string and 'Route' a boolean. "
        "Include Points only if the user requests recommendations for places, routes, etc. "
        "Provide accurate addresses for each place and verify them twice. "
        "Do not include Points if the user asks for additional information about locations already mentioned. "
        "When the user requests recommendations for places or routes, populate 'Comment' by listing each place with a brief description and a rating. "
        "Set Route to 'true' only if the user asks you to create a route."
    )
    # History in messages
    messages = [{"role": "system", "content": system_prompt}]
    for turn in st.session_state.history:
        if turn.get("user"):
            messages.append({"role": "user", "content": turn["user"]})
        if turn.get("assistant") not in [translations["assistant"]["es"], translations["assistant"]["en"]]:
            messages.append({"role": "assistant", "content": turn.get("assistant")})
    messages.append({"role": "user", "content": user_query + '. In Valencia, Spain'})
    if DEBUGGING:
        print("Messages: ", messages)
        print()

    # JSON schema
    schema = {
        "type": "object",
        "properties": {
            "Points": {"type": "array", "items": {"type": "object",
                                                  "properties": {"name": {"type": "string"},
                                                                 "address": {"type": "string"}},
                                                  "required": ["name", "address"]
                                                  }},
            "Comment": {"type": "string"},
            "Route": {"type": "boolean"}
        },
        "required": ["Points", "Comment", "Route"]
    }

    payload = {
        "model": "sonar-pro",
        "messages": messages,
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        "web_search_options": {
            "user_location": {
                "latitude": 39.466667,
                "longitude": -0.375000,
                "country": "ES"
            }
        }
    }
    headers = {
        "Authorization": f"Bearer {PPL_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers=headers,
        json=payload,
        timeout=60
    )

    resp.raise_for_status()
    raw = resp.json()
    if DEBUGGING:
        print("RAW response: ", raw)
        print("-"*30)
    content = raw["choices"][0]["message"]["content"]
    data = json.loads(content)
    sources = raw.get("search_results", [])

    # Add to history
    st.session_state.history.append({
        "user": user_query,
        "assistant": data.get("Comment", ""),
        "sources": sources
    })

    # Geocode addresses and save points
    gmaps = googlemaps.Client(key=GMAPS_API_KEY)
    new_points = []
    for p in data.get("Points", []):
        try:
            address_query = f"{p['address']}"
            results = gmaps.geocode(address_query)
            if results:
                loc = results[0]["geometry"]["location"]
                lat, lon = loc["lat"], loc["lng"]

                # Avoid overlapping markers
                existing_coords = [(pt["lat"], pt["lon"]) for pt in st.session_state.points]
                if (lat, lon) in existing_coords:
                    lat += random.uniform(-0.0001, 0.0001)
                    lon += random.uniform(-0.0001, 0.0001)
                    if DEBUGGING:
                        print(f"Duplicate coords, offset applied for {p['name']}")

                point = {"name": p["name"], "lat": lat, "lon": lon, "address": p["address"]}
                st.session_state.points.append(point)
                new_points.append(point)
                if DEBUGGING:
                    print(f"Geocoded: {p['name']} -> {lat}, {lon}")
            else:
                if DEBUGGING:
                    print(f"No geocode result for: {p['address']}")
        except Exception as e:
            if DEBUGGING:
                print(f"Error geocoding {p['address']}: {e}")
            continue

    # Calculate route if requested and enough points
    if data.get("Route") and len(new_points) > 1:
        try:
            # Prepare origin, destination, and waypoints
            origin = f"{new_points[0]['lat']},{new_points[0]['lon']}"
            destination = f"{new_points[-1]['lat']},{new_points[-1]['lon']}"
            waypoints = [
                            f"{pt['lat']},{pt['lon']}" for pt in new_points[1:-1]
                        ] or None

            directions = gmaps.directions(
                origin=origin,
                destination=destination,
                mode="walking",
                waypoints=waypoints,
                optimize_waypoints=True
            )

            # Decode the overview polyline to latitude/longitude pairs
            encoded = directions[0]["overview_polyline"]["points"]
            route_coords = polyline.decode(encoded)  # list of (lat, lng)

            # Append to session routes
            st.session_state.routes.append(route_coords)
            if DEBUGGING:
                print(f"Route calculated with {len(route_coords)} points")
        except Exception as e:
            if DEBUGGING:
                print(f"Error calculating route: {e}")

# -----------------------------------------------------------------------------
# RENDER MAP AND CHAT
# -----------------------------------------------------------------------------
col_map, col_chat = st.columns([3, 2])

with col_map:
    center = [39.4699, -0.3763]
    m = folium.Map(location=center, zoom_start=13)
    #LocateControl(auto_start=True).add_to(m)
    for pt in st.session_state.points:
        folium.Marker([pt["lat"], pt["lon"]], popup=pt["name"]).add_to(m)
    for r in st.session_state.routes:
        folium.PolyLine(r, color="#0068c9", weight=5, dash_array="10, 10").add_to(m)
    st_folium(m, height=800, width="100%")

with col_chat:
    # Fixed input at the top of the chat column
    query = st.chat_input(placeholder=translations["search_placeholder"][st.session_state.language], max_chars=200)
    if query:
        process_query(query)
        st.rerun()

    # Chat container with fixed height and scroll
    with st.container():
        # Build all chat HTML as a single string
        chat_html = '<div class="chat-container">'

        # Display messages in reverse order (newest first)
        for turn in reversed(st.session_state.history):
            # Assistant's response
            chat_html += f'<div class="assistant-message">{markdown.markdown(turn["assistant"])}</div>' if turn.get("assistant") else ''

            # Sources if present
            if turn.get("sources"):
                sources_links = []
                for item in turn['sources']:
                    title = item.get('title', item['url'])
                    url = item['url']
                    sources_links.append(f'<a href="{url}" target="_blank">{title}</a>')
                sources_text = "📚 Sources: " + ", ".join(sources_links)
                chat_html += f'<div class="sources-text">{sources_text}</div>'

            # User's message
            chat_html += f'<div class="user-message">{turn.get("user")}</div>' if turn.get("user") else ''

        chat_html += '</div>'

        # Render the entire chat at once
        st.markdown(chat_html, unsafe_allow_html=True)

        if st.button(translations["clear_button"][st.session_state.language]):
            reset_history()

with st.sidebar:
    st.markdown(
        "Al cambiar el idioma, se reseteará todo el historial y los marcadores. \n\nChanging the language will reset the entire history and markers."
    )
    st.selectbox(
        label="Idioma / Language",
        options=["es", "en"],
        format_func=lambda x: "Español" if x == "es" else "English",
        key="language",
        on_change=reset_history
    )