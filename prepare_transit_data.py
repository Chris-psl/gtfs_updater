import csv
import io
import os
import urllib.request
import zipfile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# OFFICIAL API ENDPOINTS
OSY_URL = (
    "https://data.gov.gr/dataset/fb049bb1-aea6-4443-95fa-8b941dd6a057/"
    "resource/119db488-16ea-4c76-b560-41c472872390/download/osy_gtfs.zip"
)
STASY_URL = (
    "https://data.gov.gr/dataset/4e897a75-975a-4ce7-af65-f32ea01f93b9/"
    "resource/5e3858ee-d9ba-48c2-9015-744ea160976d/download/stasy_gtfs.zip"
)
HELLENIC_TRAIN_URL = "https://jbb.ghsq.de/gtfs/gr-hellenic-train.gtfs.zip"
RAIL_SHAPES_URL = (
    "https://raw.githubusercontent.com/Miqell24/athens-bus-map/main/"
    "docs/data/route.geojson"
)

def download(url):
    # Added a standard User-Agent so government servers don't block GitHub Actions
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    )
    with urllib.request.urlopen(req) as response:
        return response.read()

def extract_zip(data, target):
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(target)

def write_csv(target, filename, columns, rows):
    with open(os.path.join(target, filename), "w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file, fieldnames=columns, lineterminator="\n", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)

def csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))

def prepare_proastiakos(source, target):
    routes = {
        row["route_id"]: row
        for row in csv_rows(os.path.join(source, "routes.txt"))
        if row.get("agency_id") == "hellenic-train" and row.get("route_type") == "109"
    }
    stops = {row["stop_id"]: row for row in csv_rows(os.path.join(source, "stops.txt"))}
    trips = {
        row["trip_id"]: row for row in csv_rows(os.path.join(source, "trips.txt"))
        if row.get("route_id") in routes
    }
    stop_times = {}
    for row in csv_rows(os.path.join(source, "stop_times.txt")):
        if row.get("trip_id") in trips:
            stop_times.setdefault(row["trip_id"], []).append(row)

    lines = {
        "A1": ("Πειραιάς – Αθήνα – Αεροδρόμιο", "3C5494"),
        "A2": ("Άνω Λιόσια – Αεροδρόμιο", "6E6F73"),
        "A3": ("Αθήνα – Χαλκίδα", "3BB464"),
        "A4": ("Πειραιάς – Αθήνα – Κιάτο", "F09635"),
    }

    def classify(names):
        first, last = names[0], names[-1]
        if "Κιάτο" in names:
            line = "A4"
        elif any(name in names for name in ("Χαλκίδα", "Αφίδνες", "Οινόη")):
            line = "A3"
        elif "Αεροδρόμιο" in names and (first == "Άνω Λιόσια" or last == "Άνω Λιόσια"):
            line = "A2"
        elif "Αεροδρόμιο" in names:
            line = "A1"
        else:
            return None
        inbound = {
            "A1": last in ("Πειραιάς", "Ταύρος"),
            "A2": last == "Άνω Λιόσια",
            "A3": last in ("Αθήνα", "Οινόη"),
            "A4": last in ("Πειραιάς", "Ταύρος"),
        }[line]
        return line, "1" if inbound else "0"

    output_routes = []
    output_trips = []
    output_times = []
    output_stops = {}
    output_shapes = []
    used_shapes = set()
    shape_by_pattern = {}

    for trip_id, trip in trips.items():
        sequence = sorted(stop_times.get(trip_id, []), key=lambda row: int(row["stop_sequence"]))
        if len(sequence) < 2:
            continue
        names = [stops[row["stop_id"]]["stop_name"] for row in sequence]
        classification = classify(names)
        if not classification:
            continue
        line, direction = classification
        pattern = ">".join(row["stop_id"] for row in sequence)
        shape_id = trip.get("shape_id", "")
        shape_by_pattern.setdefault(pattern, shape_id)
        shape_id = shape_by_pattern[pattern]
        output_trips.append({
            "route_id": "P:" + line,
            "service_id": "P:" + trip["service_id"],
            "trip_id": "P:" + trip_id,
            "trip_headsign": names[-1],
            "direction_id": direction,
            "shape_id": "P:" + shape_id if shape_id else "",
        })
        used_shapes.add(shape_id)
        for row in sequence:
            output_times.append({
                "trip_id": "P:" + trip_id,
                "arrival_time": row["arrival_time"],
                "departure_time": row["departure_time"],
                "stop_id": "P:" + row["stop_id"],
                "stop_sequence": row["stop_sequence"],
            })
            output_stops["P:" + row["stop_id"]] = {
                "stop_id": "P:" + row["stop_id"],
                "stop_name": row_name(stops[row["stop_id"]]["stop_name"]),
                "stop_lat": stops[row["stop_id"]]["stop_lat"],
                "stop_lon": stops[row["stop_id"]]["stop_lon"],
            }

    for line, (name, color) in lines.items():
        if any(trip["route_id"] == "P:" + line for trip in output_trips):
            output_routes.append({
                "route_id": "P:" + line,
                "agency_id": "P:HT",
                "route_short_name": line,
                "route_long_name": name,
                "route_type": "2",
                "route_color": color,
            })

    source_shapes = csv_rows(os.path.join(source, "shapes.txt"))
    output_shapes = [
        {**row, "shape_id": "P:" + row["shape_id"]}
        for row in source_shapes
        if row["shape_id"] in used_shapes
    ]
    os.makedirs(target, exist_ok=True)
    write_csv(target, "routes.txt", list(output_routes[0]), output_routes)
    write_csv(target, "trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign", "direction_id", "shape_id"], output_trips)
    write_csv(target, "stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], output_times)
    write_csv(target, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"], output_stops.values())
    write_csv(target, "shapes.txt", ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], output_shapes)

def row_name(name):
    return "ΣΚΑ (Σιδηροδρομικό Κέντρο Αχαρνών)" if name == "Σιδηροδρομικός Κέντρο Αχαρνών (ΣΚΑ)" else name

def prepare_stasy(target_dir):
    trips_path = os.path.join(target_dir, "trips.txt")
    if not os.path.exists(trips_path):
        return

    trips = csv_rows(trips_path)
    modified = False

    for trip in trips:
        route_id = trip.get("route_id", "")
        if route_id in ["T6", "T7"] or "ΤΡΑΜ" in route_id.upper():
            if not trip.get("shape_id"):
                direction = trip.get("direction_id", "1")
                trip["shape_id"] = f"{route_id}_dir{direction}"
                modified = True

    if modified and trips:
        write_csv(target_dir, "trips.txt", list(trips[0].keys()), trips)

def main():
    # Now downloading the Bus GTFS alongside Stasy and Hellenic Train!
    print("Downloading OSY (Buses) GTFS...")
    osy_dir = os.path.join(PROJECT_ROOT, "gtfs_bus")
    extract_zip(download(OSY_URL), osy_dir)

    print("Downloading STASY (Metro/Tram) GTFS...")
    stasy_dir = os.path.join(PROJECT_ROOT, "gtfs_stasy")
    extract_zip(download(STASY_URL), stasy_dir)
    prepare_stasy(stasy_dir)  

    print("Downloading Hellenic Train (Suburban) GTFS...")
    source = os.path.join(PROJECT_ROOT, ".hellenic_train")
    extract_zip(download(HELLENIC_TRAIN_URL), source)
    prepare_proastiakos(source, os.path.join(PROJECT_ROOT, "gtfs_proastiakos"))
    
    print("Downloading Rail Shapes GeoJSON...")
    with open(os.path.join(PROJECT_ROOT, "rail_shapes.geojson"), "wb") as file:
        file.write(download(RAIL_SHAPES_URL))
        
    print("Transit feeds and offline rail geometry updated.")

if __name__ == "__main__":
    main()