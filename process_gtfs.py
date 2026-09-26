import csv
import json
import os
import sqlite3

GTFS_DIRECTORIES = ["gtfs_bus", "gtfs_stasy", "gtfs_proastiakos"]
EXPECTED_TRAM_LINES = {"T6", "T7"}
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_OUTPUT = os.path.join(
    PROJECT_ROOT, "app", "src", "main", "assets", "database", "transit.db"
)
RAIL_SHAPES = os.path.join(PROJECT_ROOT, "rail_shapes.geojson")


def _pattern_direction(pattern, base_pattern):
    """
    Determines whether `pattern` runs the same way as `base_pattern` (0) or in
    reverse (1), by comparing the relative order of the stops the two patterns
    have in common.

    This is deliberately NOT an exact-endpoint comparison (e.g.
    `pattern[-1] == base_pattern[0]`). Real GTFS feeds frequently give the
    return trip a different terminus stop_id than the outbound trip's origin
    (a different bay/platform, an extra depot or layover stop tacked onto one
    end, a time-of-day variant with a slightly different final stop, etc.).
    An exact-match test silently falls back to "same direction" whenever that
    happens, which re-merges genuine return trips into direction 0 - the same
    bug this whole function exists to avoid.

    Instead, we look only at the stops shared by both patterns and check
    whether they appear in increasing or decreasing order relative to
    base_pattern. True reverse-direction trips will have their shared stops
    in decreasing order even if their exact endpoints differ; same-direction
    variants (e.g. an express pattern skipping some stops) will have them in
    increasing order.
    """
    base_index = {stop_id: i for i, stop_id in enumerate(base_pattern)}
    common_positions = [base_index[s] for s in pattern if s in base_index]

    if len(common_positions) < 2:
        # Not enough shared stops to judge order from - fall back to a loose
        # endpoint check rather than defaulting blindly to direction 0.
        if pattern[-1] == base_pattern[0] or pattern[0] == base_pattern[-1]:
            return 1
        return 0

    increasing = sum(
        1 for a, b in zip(common_positions, common_positions[1:]) if b > a
    )
    decreasing = sum(
        1 for a, b in zip(common_positions, common_positions[1:]) if b < a
    )
    return 1 if decreasing > increasing else 0


def resolve_route_directions(trip_ids_by_route, trip_to_direction, trip_stops):
    """
    Maps trip_id -> direction_id (0 or 1), robust to GTFS feeds (e.g. OSY's bus
    feed) where direction_id is blank or identical for every trip on a route.

    When the raw GTFS field can't tell two real directions apart, the
    "keep the longest trip per (route_id, direction_id)" logic below silently
    drops whichever direction happens to have shorter trips — this is what
    caused routes like line 20 to only ever show one way. Here, if a route's
    raw direction_id never varies across its trips, direction is re-derived
    from each trip's actual stop sequence instead.
    """
    resolved = {}
    for route_id, trip_ids in trip_ids_by_route.items():
        raw = {trip_to_direction.get(tid, 0) for tid in trip_ids}
        if len(raw) > 1:
            # The feed already distinguishes directions for this route - trust it.
            for tid in trip_ids:
                resolved[tid] = trip_to_direction.get(tid, 0)
            continue

        # Cluster trips by their exact stop-sequence pattern.
        patterns = {}
        for tid in trip_ids:
            stops = trip_stops.get(tid)
            if not stops:
                continue
            pattern = tuple(stop_id for _, stop_id in sorted(stops))
            patterns.setdefault(pattern, []).append(tid)

        if len(patterns) < 2:
            # Genuinely only one pattern exists - one real direction.
            for tid in trip_ids:
                resolved[tid] = 0
            continue

        ranked = sorted(patterns.items(), key=lambda kv: -len(kv[1]))
        base_pattern, base_trips = ranked[0]
        for tid in base_trips:
            resolved[tid] = 0
        for pattern, trips in ranked[1:]:
            # Judge direction by whether shared stops run in the same order as
            # the base pattern or in reverse - robust to trips whose terminus
            # stop doesn't exactly match the base pattern's origin stop.
            direction = _pattern_direction(pattern, base_pattern)
            for tid in trips:
                resolved[tid] = direction
        for tid in trip_ids:
            resolved.setdefault(tid, 0)
    return resolved


def build_database():
    os.makedirs(os.path.dirname(DB_OUTPUT), exist_ok=True)

    if os.path.exists(DB_OUTPUT):
        os.remove(DB_OUTPUT)

    conn = sqlite3.connect(DB_OUTPUT)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE routes (
            routeId TEXT PRIMARY KEY NOT NULL,
            routeShortName TEXT NOT NULL,
            routeLongName TEXT NOT NULL,
            routeType TEXT NOT NULL,
            routeColor TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE stops (
            stopId TEXT PRIMARY KEY NOT NULL,
            stopName TEXT NOT NULL,
            stopLat REAL NOT NULL,
            stopLon REAL NOT NULL,
            transportType TEXT NOT NULL
        )
    """)

    # ΠΡΟΣΘΗΚΗ directionId
    cursor.execute("""
        CREATE TABLE route_stops (
            routeId TEXT NOT NULL,
            directionId INTEGER NOT NULL,
            stopId TEXT NOT NULL,
            stopSequence INTEGER NOT NULL,
            PRIMARY KEY (routeId, directionId, stopId, stopSequence)
        )
    """)

    # ΠΡΟΣΘΗΚΗ directionId
    cursor.execute("""
        CREATE TABLE route_shapes (
            routeId TEXT NOT NULL,
            directionId INTEGER NOT NULL,
            shapeLat REAL NOT NULL,
            shapeLon REAL NOT NULL,
            shapeSequence INTEGER NOT NULL,
            PRIMARY KEY (routeId, directionId, shapeSequence)
        )
    """)

    cursor.execute("""
        CREATE TABLE favorites (
            id TEXT PRIMARY KEY NOT NULL,
            targetId TEXT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT
        )
    """)

    # ΠΡΟΣΘΗΚΗ directionId
    cursor.execute("""
        CREATE TABLE scheduled_arrivals (
            routeId TEXT NOT NULL,
            directionId INTEGER NOT NULL,
            stopId TEXT NOT NULL,
            arrivalSeconds INTEGER NOT NULL,
            PRIMARY KEY (routeId, directionId, stopId, arrivalSeconds)
        )
    """)

    for gtfs_dir in GTFS_DIRECTORIES:
        if not os.path.exists(gtfs_dir):
            continue

        print(f"Processing {gtfs_dir}...")

        # 1. Routes
        routes_path = os.path.join(gtfs_dir, "routes.txt")
        if os.path.exists(routes_path):
            with open(routes_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    route_id = row["route_id"].strip()
                    short_name = row.get("route_short_name", "").strip()
                    long_name = row.get("route_long_name", "").strip()

                    raw_type = row.get("route_type", "3")
                    if raw_type == "1" or "subway" in gtfs_dir.lower():
                        transport_type = "METRO"
                    elif raw_type == "0" or "stasy" in gtfs_dir.lower():
                        transport_type = "TRAM"
                    elif "proastiakos" in gtfs_dir.lower():
                        transport_type = "SUBURBAN"
                    elif (
                        raw_type in ["0", "2", "4"]
                        or "TRAM" in short_name.upper()
                        or "ΤΡΑΜ" in long_name.upper()
                    ):
                        transport_type = "TRAM"
                    else:
                        transport_type = "BUS"

                    route_color = row.get("route_color", "").strip()
                    if not route_color or len(route_color) != 6:
                        if transport_type == "METRO":
                            if "1" in short_name or "ISAP" in long_name.upper():
                                route_color = "00A859"
                            elif "2" in short_name:
                                route_color = "DA291C"
                            else:
                                route_color = "00539F"
                        elif transport_type == "TRAM":
                            route_color = "E91E63"
                        else:
                            route_color = "0A58CA"

                    if transport_type == "TRAM":
                        route_color = "E91E63"

                    route_color = "#" + route_color

                    cursor.execute(
                        "INSERT OR IGNORE INTO routes VALUES (?, ?, ?, ?, ?)",
                        (route_id, short_name, long_name, transport_type, route_color),
                    )

        # 2. Stops
        stops_path = os.path.join(gtfs_dir, "stops.txt")
        if os.path.exists(stops_path):
            with open(stops_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    stop_id = row["stop_id"].strip()
                    stop_name = row["stop_name"].strip()
                    try:
                        lat = float(row["stop_lat"])
                        lon = float(row["stop_lon"])
                    except (ValueError, TypeError):
                        continue

                    if "proastiakos" in gtfs_dir.lower():
                        default_transport = "SUBURBAN"
                    elif "stasy" in gtfs_dir.lower() or "subway" in gtfs_dir.lower():
                        default_transport = "METRO"
                    else:
                        default_transport = "BUS"
                    cursor.execute(
                        "INSERT OR IGNORE INTO stops VALUES (?, ?, ?, ?, ?)",
                        (stop_id, stop_name, lat, lon, default_transport),
                    )

        # 3. Trips, Stop Times & Shapes
        trips_path = os.path.join(gtfs_dir, "trips.txt")
        stop_times_path = os.path.join(gtfs_dir, "stop_times.txt")
        shapes_path = os.path.join(gtfs_dir, "shapes.txt")

        trip_to_route = {}
        trip_to_shape = {}
        trip_to_direction = {}
        if os.path.exists(trips_path):
            with open(trips_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trip_id = row["trip_id"].strip()
                    route_id = row["route_id"].strip()
                    shape_id = row.get("shape_id", "").strip()
                    dir_str = row.get("direction_id", "").strip()
                    direction_id = int(dir_str) if dir_str.isdigit() else 0
                    
                    trip_to_route[trip_id] = route_id
                    trip_to_direction[trip_id] = direction_id
                    if shape_id:
                        trip_to_shape[trip_id] = shape_id

        # Stop Times for route_stops (UI stop list)
        if os.path.exists(stop_times_path):
            trip_stops = {}
            with open(stop_times_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trip_id = row["trip_id"].strip()
                    if trip_id in trip_to_route:
                        try:
                            seq = int(row["stop_sequence"])
                        except ValueError:
                            seq = 0
                        stop_id = row["stop_id"].strip()
                        if trip_id not in trip_stops:
                            trip_stops[trip_id] = []
                        trip_stops[trip_id].append((seq, stop_id))

            # Re-derive direction_id per route whenever the raw GTFS field is
            # unreliable (blank/constant for every trip on that route). This is
            # the fix for lines like 20 that were losing their return direction
            # because both directions collapsed onto the same (route, 0) key.
            trip_ids_by_route = {}
            for trip_id in trip_stops:
                route_id = trip_to_route.get(trip_id)
                if route_id:
                    trip_ids_by_route.setdefault(route_id, []).append(trip_id)
            trip_to_direction = resolve_route_directions(
                trip_ids_by_route, trip_to_direction, trip_stops
            )

            # Κρατάμε το καλύτερο δρομολόγιο ΑΝΑ ΓΡΑΜΜΗ & ΚΑΤΕΥΘΥΝΣΗ (Πλέον δεν σβήνεται η επιστροφή)
            route_dir_best_trip = {}
            for trip_id, stops_list in trip_stops.items():
                route_id = trip_to_route.get(trip_id)
                direction_id = trip_to_direction.get(trip_id, 0)
                if route_id:
                    key = (route_id, direction_id)
                    if (
                        key not in route_dir_best_trip
                        or len(stops_list) > route_dir_best_trip[key][1]
                    ):
                        route_dir_best_trip[key] = (trip_id, len(stops_list))

            batch_stops = set()
            for (route_id, direction_id), (best_trip_id, _) in route_dir_best_trip.items():
                sorted_stops = sorted(trip_stops[best_trip_id], key=lambda x: x[0])
                for idx, (seq, stop_id) in enumerate(sorted_stops):
                    batch_stops.add((route_id, direction_id, stop_id, idx))

            cursor.executemany(
                "INSERT OR IGNORE INTO route_stops VALUES (?, ?, ?, ?)", list(batch_stops)
            )

        # Shapes for route_shapes (High-resolution map rendering)
        if os.path.exists(shapes_path) and trip_to_shape:
            shape_points = {}
            with open(shapes_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    shape_id = row["shape_id"].strip()
                    try:
                        lat = float(row["shape_pt_lat"])
                        lon = float(row["shape_pt_lon"])
                        seq = int(row["shape_pt_sequence"])
                    except (ValueError, TypeError):
                        continue
                    if shape_id not in shape_points:
                        shape_points[shape_id] = []
                    shape_points[shape_id].append((seq, lat, lon))

            route_dir_best_shape = {}
            for (route_id, direction_id), (best_trip_id, _) in route_dir_best_trip.items():
                shape_id = trip_to_shape.get(best_trip_id)
                if shape_id in shape_points:
                    route_dir_best_shape[(route_id, direction_id)] = (shape_id, len(shape_points[shape_id]))

            batch_shapes = []
            for (route_id, direction_id), (best_shape_id, _) in route_dir_best_shape.items():
                sorted_pts = sorted(shape_points[best_shape_id], key=lambda x: x[0])
                for idx, (seq, lat, lon) in enumerate(sorted_pts):
                    batch_shapes.append((route_id, direction_id, lat, lon, idx))

            # Αν δεν υπάρχει επίσημο shape, σχεδιάζουμε γραμμή ενώνοντας τις στάσεις
            for (route_id, direction_id), (best_trip_id, _) in route_dir_best_trip.items():
                if (route_id, direction_id) in route_dir_best_shape:
                    continue
                sorted_stops = sorted(trip_stops[best_trip_id], key=lambda x: x[0])
                for idx, (_, stop_id) in enumerate(sorted_stops):
                    stop = cursor.execute(
                        "SELECT stopLat, stopLon FROM stops WHERE stopId = ?", (stop_id,)
                    ).fetchone()
                    if stop:
                        batch_shapes.append((route_id, direction_id, stop[0], stop[1], idx))

            cursor.executemany(
                "INSERT OR IGNORE INTO route_shapes VALUES (?, ?, ?, ?, ?)", batch_shapes
            )

        # Προγραμματισμένες αφίξεις
        if os.path.exists(stop_times_path):
            scheduled_arrivals = []
            with open(stop_times_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trip_id = row.get("trip_id", "").strip()
                    route_id = trip_to_route.get(trip_id)
                    direction_id = trip_to_direction.get(trip_id, 0)
                    stop_id = row.get("stop_id", "").strip()
                    raw_time = row.get("arrival_time", "").strip()
                    if not route_id or not stop_id or not raw_time:
                        continue
                    try:
                        hours, minutes, seconds = (int(part) for part in raw_time.split(":"))
                        arrival_seconds = hours * 3600 + minutes * 60 + seconds
                    except (ValueError, TypeError):
                        continue
                    scheduled_arrivals.append((route_id, direction_id, stop_id, arrival_seconds))
            cursor.executemany(
                "INSERT OR IGNORE INTO scheduled_arrivals VALUES (?, ?, ?, ?)",
                scheduled_arrivals,
            )

    # GEOJSON Override
    if os.path.exists(RAIL_SHAPES):
        with open(RAIL_SHAPES, mode="r", encoding="utf-8") as f:
            rail_shapes = json.load(f).get("features", [])

        routes_by_name = {
            row[0]: row[1]
            for row in cursor.execute("SELECT routeShortName, routeId FROM routes")
        }
        
        lines_geojson = {}
        for feature in rail_shapes:
            properties = feature.get("properties", {})
            line_name = properties.get("line")
            geom_type = feature.get("geometry", {}).get("type")
            coords = feature.get("geometry", {}).get("coordinates", [])
            
            if properties.get("mode") != "tram" or not line_name or not coords:
                continue
                
            if geom_type == "LineString":
                lines_geojson.setdefault(line_name, []).append(coords)
            elif geom_type == "MultiLineString":
                for line_coords in coords:
                    lines_geojson.setdefault(line_name, []).append(line_coords)

        # Αν το GeoJSON έχει δύο (ή παραπάνω) lines για μία διαδρομή, τις αναθέτουμε σε direction 0 και 1
        for line_name, shapes_list in lines_geojson.items():
            route_id = routes_by_name.get(line_name)
            if not route_id:
                continue
            
            shapes_list.sort(key=len, reverse=True)
            cursor.execute("DELETE FROM route_shapes WHERE routeId = ?", (route_id,))
            
            for direction_id, coordinates in enumerate(shapes_list[:2]):
                cursor.executemany(
                    "INSERT OR IGNORE INTO route_shapes VALUES (?, ?, ?, ?, ?)",
                    [(route_id, direction_id, lat, lon, index) for index, (lon, lat) in enumerate(coordinates)],
                )

    tram_lines = {
        row[0] for row in cursor.execute(
            "SELECT routeShortName FROM routes WHERE routeType = 'TRAM'"
        )
    }
    missing_tram_lines = EXPECTED_TRAM_LINES - tram_lines
    if missing_tram_lines:
        raise RuntimeError(
            "Missing expected tram lines: " + ", ".join(sorted(missing_tram_lines))
        )

    conn.commit()
    conn.close()
    print("Database successfully built with routes, stops, and shapes (both directions)!")


if __name__ == "__main__":
    build_database()
