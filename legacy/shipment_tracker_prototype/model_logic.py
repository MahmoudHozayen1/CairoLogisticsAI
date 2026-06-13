import numpy as np
from sklearn.cluster import KMeans
import osmnx as ox
import networkx as nx

# --- 1. CONFIGURATION ---
# We define a central point in Maadi to download the map around.
# This avoids the "Polygon not found" error you saw earlier.
CENTER_LAT = 29.9602
CENTER_LON = 31.2569
SEARCH_RADIUS = 3000  # Download streets within 3km of Maadi Station

print(f"Loading Street Map for Cairo (Radius: {SEARCH_RADIUS}m)... Please wait.")
try:
    # Download the street network (Drive mode)
    G = ox.graph_from_point((CENTER_LAT, CENTER_LON), dist=SEARCH_RADIUS, network_type='drive')
    # Add speed/travel time data to the graph for accurate timing
    G = ox.add_edge_speeds(G)
    G = ox.add_edge_travel_times(G)
    print("GIS Data Loaded Successfully!")
except Exception as e:
    print(f"Error loading map: {e}")
    G = None

# --- 2. LOCATIONS DATABASE ---
CAIRO_LOCATIONS = {
    'Maadi Station': [29.9602, 31.2569],
    'Grand Mall': [29.9575, 31.2650],
    'Degla Square': [29.9650, 31.2600],
    'Corniche Maadi': [29.9500, 31.2400],
    'Victory College': [29.9700, 31.2700]
}

# --- 3. HELPER FUNCTIONS ---
def get_dist(p1, p2):
    # Simple straight line distance for clustering logic
    return np.linalg.norm(np.array(p1) - np.array(p2))

def get_real_path(start_coords, end_coords):
    """
    Calculates the detailed street path between two points.
    Returns a list of [lat, lon] points that follow the road.
    """
    if G is None:
        return [start_coords, end_coords] # Fallback to straight line if map failed

    try:
        # 1. Find the nearest street intersection (Node) to our points
        orig_node = ox.distance.nearest_nodes(G, X=start_coords[1], Y=start_coords[0])
        dest_node = ox.distance.nearest_nodes(G, X=end_coords[1], Y=end_coords[0])

        # 2. Calculate shortest driving path (Dijkstra's Algorithm)
        route_nodes = nx.shortest_path(G, orig_node, dest_node, weight='travel_time')

        # 3. Convert Nodes back to Coordinates (Lat, Lon)
        path_coords = []
        for node in route_nodes:
            point = G.nodes[node]
            path_coords.append([point['y'], point['x']])
            
        return path_coords
    except Exception:
        # If route fails (e.g. points too far apart), return straight line
        return [start_coords, end_coords]

# --- 4. MAIN OPTIMIZATION FUNCTION ---
def optimize_routes(shipments, warehouses):
    if not shipments or not warehouses:
        return {}

    # A. Assign Shipments to Nearest Warehouse
    warehouse_groups = {w['id']: [] for w in warehouses}
    for s in shipments:
        dists = [get_dist(s['coords'], w['coords']) for w in warehouses]
        nearest_w = warehouses[np.argmin(dists)]
        warehouse_groups[nearest_w['id']].append(s)

    final_routes = {}

    # B. Process Each Warehouse
    for w in warehouses:
        w_shipments = warehouse_groups[w['id']]
        n_couriers = w['couriers']
        
        if not w_shipments: continue
            
        active_couriers = min(len(w_shipments), n_couriers)
        
        # Clustering
        coords = [s['coords'] for s in w_shipments]
        if active_couriers > 0:
            kmeans = KMeans(n_clusters=active_couriers, random_state=42, n_init=10)
            labels = kmeans.fit_predict(coords)
        else:
            labels = []

        # C. Route Each Courier
        for c_idx in range(active_couriers):
            indices = np.where(labels == c_idx)[0]
            courier_load = [w_shipments[i] for i in indices]
            
            if not courier_load: continue

            sorted_route = []
            
            # 1. From Depot -> First Stop
            current_coords = w['coords'] 
            # Find nearest first stop
            dists = [get_dist(current_coords, s['coords']) for s in courier_load]
            first_stop = courier_load.pop(np.argmin(dists))
            
            # CALCULATE REAL STREET PATH
            street_path = get_real_path(current_coords, first_stop['coords'])
            
            sorted_route.append({
                'type': 'start',
                'shipment': {'id': 'DEPOT', 'customer': w['name']},
                'path_to_next': street_path 
            })
            
            # 2. From Stop -> Next Stop
            current_stop = first_stop
            while courier_load:
                dists = [get_dist(current_stop['coords'], s['coords']) for s in courier_load]
                next_stop = courier_load.pop(np.argmin(dists))
                
                street_path = get_real_path(current_stop['coords'], next_stop['coords'])
                
                sorted_route.append({
                    'type': 'shipment',
                    'shipment': current_stop,
                    'path_to_next': street_path
                })
                current_stop = next_stop
            
            # Final Stop
            sorted_route.append({
                'type': 'shipment',
                'shipment': current_stop,
                'path_to_next': [] 
            })

            route_key = f"{w['name']} - Courier {c_idx + 1}"
            final_routes[route_key] = sorted_route
            
    return final_routes