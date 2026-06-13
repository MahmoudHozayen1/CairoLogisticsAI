from flask import Flask, render_template, request, redirect, url_for
from model_logic import optimize_routes, CAIRO_LOCATIONS

app = Flask(__name__)

# --- GLOBAL VARIABLES ---
latest_routes = {} 

# --- DATABASES ---
warehouses_db = [
    {'id': 1, 'name': 'Maadi Hub', 'coords': CAIRO_LOCATIONS.get('Maadi Station'), 'couriers': 2},
]

# Updated Shipment Structure
shipments_db = [
    {
        'id': 101, 
        'sender_name': 'Jumia Store', 'sender_phone': '01012345678',
        'receiver_name': 'Ahmed', 'receiver_phone': '01234567890',
        'district': 'Grand Mall', 'landmark': 'Near Cinema',
        'coords': CAIRO_LOCATIONS.get('Grand Mall'), 
        'status': 'Pending', 'fulfilled_by': 'Unassigned'
    },
    {
        'id': 102, 
        'sender_name': 'Amazon Egy', 'sender_phone': '01123456789',
        'receiver_name': 'Sarah', 'receiver_phone': '01555555555',
        'district': 'Degla Square', 'landmark': 'Beside Metro',
        'coords': CAIRO_LOCATIONS.get('Degla Square'), 
        'status': 'Pending', 'fulfilled_by': 'Unassigned'
    },
]

@app.route('/')
def home():
    return render_template('landing.html')

@app.route('/admin')
def admin_dashboard():
    global latest_routes
    # Run Optimization
    latest_routes = optimize_routes(shipments_db, warehouses_db)
    
    # Update 'fulfilled_by' based on the result
    # (In a real app, you'd save this to DB, here we just update for display)
    for route_name, stops in latest_routes.items():
        for stop in stops:
            if stop['type'] == 'shipment':
                # Find the shipment in DB and update fulfilled_by
                s = next((x for x in shipments_db if x['id'] == stop['shipment']['id']), None)
                if s: s['fulfilled_by'] = route_name.split(' - ')[1] # e.g. "Courier 1"

    return render_template('admin_dashboard.html', 
                           routes=latest_routes, 
                           shipments=shipments_db, 
                           warehouses=warehouses_db)

# --- WAREHOUSE ROUTES (Keep as is) ---
@app.route('/add_warehouse', methods=['POST'])
def add_warehouse():
    try:
        name = request.form['name']
        couriers = int(request.form['couriers'])
        lat = float(request.form['lat'])
        lon = float(request.form['lon'])
        warehouses_db.append({'id': len(warehouses_db)+1, 'name': name, 'coords': [lat, lon], 'couriers': couriers})
    except: pass
    return redirect(url_for('admin_dashboard'))

@app.route('/edit_warehouse/<int:w_id>', methods=['POST'])
def edit_warehouse(w_id):
    w = next((x for x in warehouses_db if x['id'] == w_id), None)
    if w:
        w['name'] = request.form['name']
        w['couriers'] = int(request.form['couriers'])
        w['coords'] = [float(request.form['lat']), float(request.form['lon'])]
    return redirect(url_for('admin_dashboard'))

@app.route('/delete_warehouse/<int:w_id>')
def delete_warehouse(w_id):
    global warehouses_db
    warehouses_db = [w for w in warehouses_db if w['id'] != w_id]
    return redirect(url_for('admin_dashboard'))

# --- NEW SHIPMENT ROUTES ---

@app.route('/add_shipment', methods=['POST'])
def add_shipment():
    try:
        # Get all new details
        new_id = (shipments_db[-1]['id'] + 1) if shipments_db else 101
        
        new_shipment = {
            'id': new_id,
            'sender_name': request.form['sender_name'],
            'sender_phone': request.form['sender_phone'],
            'receiver_name': request.form['receiver_name'],
            'receiver_phone': request.form['receiver_phone'],
            'landmark': request.form['landmark'],
            'district': 'Custom',
            'coords': [float(request.form['lat']), float(request.form['lon'])],
            'status': 'Pending',
            'fulfilled_by': 'Unassigned'
        }
        shipments_db.append(new_shipment)
    except: pass
    return redirect(url_for('admin_dashboard'))

@app.route('/edit_shipment/<int:s_id>', methods=['POST'])
def edit_shipment(s_id):
    s = next((x for x in shipments_db if x['id'] == s_id), None)
    if s:
        s['sender_name'] = request.form['sender_name']
        s['sender_phone'] = request.form['sender_phone']
        s['receiver_name'] = request.form['receiver_name']
        s['receiver_phone'] = request.form['receiver_phone']
        s['landmark'] = request.form['landmark']
        s['coords'] = [float(request.form['lat']), float(request.form['lon'])]
    return redirect(url_for('admin_dashboard'))

@app.route('/track', methods=['GET', 'POST'])
def track_order():
    status_info = None
    
    if request.method == 'POST':
        try:
            order_id = int(request.form['order_id'])
            # Find the shipment in DB
            order = next((item for item in shipments_db if item["id"] == order_id), None)
            
            if order:
                eta_msg = "Processing at Warehouse"
                found_in_route = False
                
                # Check where it is in the routes to calculate ETA
                # (Ensure admin page has been visited at least once to generate routes)
                if latest_routes:
                    for route_name, stops in latest_routes.items():
                        current_time = 0
                        for stop in stops:
                            if stop['type'] == 'start': continue
                            
                            # Add time for stops (Mock: 15 mins per stop)
                            current_time += 15 
                            
                            if stop['shipment']['id'] == order_id:
                                # Create a Time Range (e.g. "25 - 40 Mins")
                                min_time = max(10, current_time - 10)
                                max_time = current_time + 10
                                eta_msg = f"{min_time} - {max_time} Mins"
                                found_in_route = True
                                
                                # Update the Courier Name dynamically
                                order['fulfilled_by'] = route_name.split(' - ')[1] # e.g. "Courier 1"
                                break
                        if found_in_route: break
                
                # PREPARE ALL DETAILS FOR DISPLAY
                status_info = {
                    'id': order['id'],
                    'status': 'Out for Delivery' if found_in_route else 'Pending',
                    'eta': eta_msg,
                    
                    # Sender Details
                    'sender_name': order['sender_name'],
                    'sender_phone': order['sender_phone'],
                    
                    # Receiver Details
                    'receiver_name': order['receiver_name'],
                    'receiver_phone': order['receiver_phone'],
                    
                    # Location Details
                    'district': order.get('district', 'Custom Location'),
                    'landmark': order.get('landmark', 'N/A'),
                    
                    # Logistics Details
                    'fulfilled_by': order.get('fulfilled_by', 'Unassigned')
                }
        except: 
            pass
            
    return render_template('track_order.html', info=status_info)
if __name__ == '__main__':
    app.run(debug=True)