import csv
import json
import math
import os
import sqlite3

GTFS_DIRECTORIES = ["gtfs_bus", "gtfs_stasy", "gtfs_proastiakos"]
EXPECTED_TRAM_LINES = {"T6", "T7"}
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_OUTPUT = os.path.join(
    PROJECT_ROOT, "app", "src", "main", "assets", "database", "transit.db"
)
RAIL_SHAPES = os.path.join(PROJECT_ROOT, "rail_shapes.geojson")


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _stop_latlon(stops_by_id, stop_id):
    row = stops_by_id.get(stop_id)
    if not row:
        return None
    try:
        return float(row["stop_lat"]), float(row["stop_lon"])
    except (TypeError, ValueError, KeyError):
        return None


def _bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _angle_diff(a, b):
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


def _pattern_bearing(pattern, stops_by_id):
    start = _stop_latlon(stops_by_id, pattern[0])
    end = _stop_latlon(stops_by_id, pattern[-1])
    if not start or not end or start == end:
        return None
    return _bearing(start[0], start[1], end[0], end[1])


def _mode(values):
    if not values:
        return None
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# Variant clustering
#
# A route can have far more than two physical patterns: a normal two-way bus
# line, express/skip variants, a school-only detour, or - like the circular
# 824 - a main loop plus a single once-a-day short-working from a different
# square (Πλατεία Καραϊσκάκη). None of these should ever be dropped or
# silently merged into another pattern just because it isn't the most common
# one. Every distinct stop sequence with >= 2 stops becomes its own
# "variant" and is kept in full; directionId is only ever a UI grouping
# label (which tab a variant's PRIMARY sibling lives under), never a filter
# that decides what data survives.
# ---------------------------------------------------------------------------

def _assign_direction(pattern, reference_pattern, reference_bearing, raw_directions, stops_by_id):
    """
    Priority:
      1. If every trip that shares this EXACT pattern agrees on a raw 0/1
         GTFS direction_id, trust it - it only needs to be self-consistent
         for this one pattern, not for the whole (possibly messy) feed.
      2. Otherwise fall back to geography: compare the bearing from this
         pattern's first stop to its last stop against the route's
         dominant (most-used) pattern's bearing. This still works when the
         two patterns share zero stops, unlike a stop-position comparison,
         and is what keeps a low-overlap return trip out of the outbound
         bucket instead of defaulting into it.
      3. A genuine loop (first stop == last stop) with nothing to compare
         bearings against is the route's single direction - 0.
    """
    if raw_directions == {0} or raw_directions == {1}:
        return next(iter(raw_directions))

    if pattern == reference_pattern:
        return 0

    if pattern[0] == pattern[-1]:
        return 0

    bearing = _pattern_bearing(pattern, stops_by_id)
    if bearing is None or reference_bearing is None:
        # No usable geometry and it's a genuinely distinct pattern from the
        # dominant one - safer to flag it as the other direction than to
        # silently fold it into the main pattern's bucket.
        return 1

    return 1 if _angle_diff(bearing, reference_bearing) > 90.0 else 0


def build_route_variants(route_id, trip_ids, trip_to_direction, trip_stops,
                          trip_to_headsign, trip_to_shape, stops_by_id):
    patterns = {}  # pattern tuple -> [trip_id, ...]
    for trip_id in trip_ids:
        stops = trip_stops.get(trip_id)
        if not stops or len(stops) < 2:
            continue
        pattern = tuple(stop_id for _, stop_id in sorted(stops))
        patterns.setdefault(pattern, []).append(trip_id)

    if not patterns:
        return []

    reference_pattern = max(patterns, key=lambda p: len(patterns[p]))
    reference_bearing = _pattern_bearing(reference_pattern, stops_by_id)

    variants = []
    for index, (pattern, trip_ids_in_pattern) in enumerate(
        sorted(patterns.items(), key=lambda kv: -len(kv[1]))
    ):
        raw_directions = {trip_to_direction.get(tid) for tid in trip_ids_in_pattern}
        raw_directions.discard(None)

        direction_id = _assign_direction(
            pattern, reference_pattern, reference_bearing, raw_directions, stops_by_id
        )

        shape_id = _mode(
            [trip_to_shape.get(tid) for tid in trip_ids_in_pattern if trip_to_shape.get(tid)]
        )
        headsign = _mode(
            [trip_to_headsign.get(tid) for tid in trip_ids_in_pattern if trip_to_headsign.get(tid)]
        ) or (stops_by_id.get(pattern[-1], {}) or {}).get("stop_name", "")

        variants.append({
            "variant_id": f"{route_id}::v{index}",
            "route_id": route_id,
            "direction_id": direction_id,
            "headsign": headsign,
            "trip_count": len(trip_ids_in_pattern),
            "stop_ids": pattern,
            "shape_id": shape_id,
        })

    # The most-used variant per direction stays "primary" - it's what
    # continues to drive the existing Outbound/Inbound tabs and the legacy
    # route_stops/route_shapes tables the metro/tram background layer reads.
    # Every other variant (rare detours, short-workings, express patterns)
    # is still stored and still selectable - it's just not the default.
    best_per_direction = {}
    for variant in variants:
        key = variant["direction_id"]
        if key not in best_per_direction or variant["trip_count"] > best_per_direction[key]["trip_count"]:
            best_per_direction[key] = variant
    for variant in variants:
        variant["is_primary"] = variant is best_per_direction[variant["direction_id"]]

    return variants


def _shape_or_stop_points(variant, shape_points, stops_by_id):
    """Returns a list of (lat, lon) points to draw for a variant: the shape
    if one exists with >= 2 points, otherwise the variant's own stops joined
    in order - never another variant's or another trip's stops."""
    shape_id = variant["shape_id"]
    points = shape_points.get(shape_id) if shape_id else None
    if points and len(points) >= 2:
        sorted_pts = sorted(points, key=lambda x: x[0])
        return [(lat, lon) for _, lat, lon in sorted_pts]

    result = []
    for stop_id in variant["stop_ids"]:
        latlon = _stop_latlon(stops_by_id, stop_id)
        if latlon:
            result.append(latlon)
    return result


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

    # Legacy "one path per (route, direction)" tables. Still populated -
    # from each direction's PRIMARY variant - so the existing metro/tram
    # background layer (MapLibreManager.loadAndRenderTransitLines) and the
    # default Outbound/Inbound view keep working unchanged.
    cursor.execute("""
        CREATE TABLE route_stops (
            routeId TEXT NOT NULL,
            directionId INTEGER NOT NULL,
            stopId TEXT NOT NULL,
            stopSequence INTEGER NOT NULL,
            PRIMARY KEY (routeId, directionId, stopId, stopSequence)
        )
    """)

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

    # New: every distinct physical pattern a route runs, kept in full.
    cursor.execute("""
        CREATE TABLE route_variants (
            variantId TEXT PRIMARY KEY NOT NULL,
            routeId TEXT NOT NULL,
            directionId INTEGER NOT NULL,
            headsign TEXT,
            tripCount INTEGER NOT NULL,
            isPrimary INTEGER NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE route_variant_stops (
            variantId TEXT NOT NULL,
            routeId TEXT NOT NULL,
            stopId TEXT NOT NULL,
            stopSequence INTEGER NOT NULL,
            PRIMARY KEY (variantId, stopSequence)
        )
    """)

    cursor.execute("""
        CREATE TABLE route_variant_shapes (
            variantId TEXT NOT NULL,
            routeId TEXT NOT NULL,
            shapeLat REAL NOT NULL,
            shapeLon REAL NOT NULL,
            shapeSequence INTEGER NOT NULL,
            PRIMARY KEY (variantId, shapeSequence)
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
        stops_by_id = {}
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
                    stops_by_id[stop_id] = {"stop_name": stop_name, "stop_lat": lat, "stop_lon": lon}

        # 3. Trips
        trips_path = os.path.join(gtfs_dir, "trips.txt")
        stop_times_path = os.path.join(gtfs_dir, "stop_times.txt")
        shapes_path = os.path.join(gtfs_dir, "shapes.txt")

        trip_to_route = {}
        trip_to_shape = {}
        trip_to_direction = {}
        trip_to_headsign = {}
        if os.path.exists(trips_path):
            with open(trips_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trip_id = row["trip_id"].strip()
                    route_id = row["route_id"].strip()
                    shape_id = row.get("shape_id", "").strip()
                    dir_str = row.get("direction_id", "").strip()

                    trip_to_route[trip_id] = route_id
                    trip_to_direction[trip_id] = int(dir_str) if dir_str in ("0", "1") else None
                    trip_to_headsign[trip_id] = row.get("trip_headsign", "").strip()
                    if shape_id:
                        trip_to_shape[trip_id] = shape_id

        # 4. Stop times -> per-trip stop sequences
        trip_stops = {}
        if os.path.exists(stop_times_path):
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
                        trip_stops.setdefault(trip_id, []).append((seq, stop_id))

        # 5. Shapes
        shape_points = {}
        if os.path.exists(shapes_path):
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
                    shape_points.setdefault(shape_id, []).append((seq, lat, lon))

        if not trip_stops:
            continue

        # 6. Cluster every route's trips into variants
        trip_ids_by_route = {}
        for trip_id in trip_stops:
            route_id = trip_to_route.get(trip_id)
            if route_id:
                trip_ids_by_route.setdefault(route_id, []).append(trip_id)

        variants_by_route = {}
        for route_id, trip_ids in trip_ids_by_route.items():
            variants_by_route[route_id] = build_route_variants(
                route_id, trip_ids, trip_to_direction, trip_stops,
                trip_to_headsign, trip_to_shape, stops_by_id
            )

        # 7. Insert variants + their stops/shapes, and mirror primaries into
        #    the legacy route_stops/route_shapes tables.
        batch_variants = []
        batch_variant_stops = []
        batch_variant_shapes = []
        batch_legacy_stops = set()
        batch_legacy_shapes = []
        pattern_to_variant = {}

        for route_id, variants in variants_by_route.items():
            for variant in variants:
                pattern_to_variant[(route_id, variant["stop_ids"])] = variant

                batch_variants.append((
                    variant["variant_id"], route_id, variant["direction_id"],
                    variant["headsign"], variant["trip_count"],
                    1 if variant["is_primary"] else 0,
                ))

                for idx, stop_id in enumerate(variant["stop_ids"]):
                    batch_variant_stops.append((variant["variant_id"], route_id, stop_id, idx))

                points = _shape_or_stop_points(variant, shape_points, stops_by_id)
                for idx, (lat, lon) in enumerate(points):
                    batch_variant_shapes.append((variant["variant_id"], route_id, lat, lon, idx))

                if variant["is_primary"]:
                    for idx, stop_id in enumerate(variant["stop_ids"]):
                        batch_legacy_stops.add((route_id, variant["direction_id"], stop_id, idx))
                    for idx, (lat, lon) in enumerate(points):
                        batch_legacy_shapes.append((route_id, variant["direction_id"], lat, lon, idx))

        cursor.executemany("INSERT OR IGNORE INTO route_variants VALUES (?, ?, ?, ?, ?, ?)", batch_variants)
        cursor.executemany("INSERT OR IGNORE INTO route_variant_stops VALUES (?, ?, ?, ?)", batch_variant_stops)
        cursor.executemany("INSERT OR IGNORE INTO route_variant_shapes VALUES (?, ?, ?, ?, ?)", batch_variant_shapes)
        cursor.executemany("INSERT OR IGNORE INTO route_stops VALUES (?, ?, ?, ?)", list(batch_legacy_stops))
        cursor.executemany("INSERT OR IGNORE INTO route_shapes VALUES (?, ?, ?, ?, ?)", batch_legacy_shapes)

        # 8. Scheduled arrivals - attributed via each trip's OWN exact
        #    pattern -> variant -> directionId, so every arrival lands under
        #    the direction its trip actually belongs to (fixes the old
        #    resolve_route_directions mis-attribution) instead of a single
        #    per-route heuristic.
        scheduled_arrivals = []
        with open(stop_times_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trip_id = row.get("trip_id", "").strip()
                route_id = trip_to_route.get(trip_id)
                stop_id = row.get("stop_id", "").strip()
                raw_time = row.get("arrival_time", "").strip()
                if not route_id or not stop_id or not raw_time:
                    continue
                stops_for_trip = trip_stops.get(trip_id)
                if not stops_for_trip:
                    continue
                pattern = tuple(s for _, s in sorted(stops_for_trip))
                variant = pattern_to_variant.get((route_id, pattern))
                direction_id = variant["direction_id"] if variant else 0
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

    # GEOJSON Override (unchanged - still targets the legacy route_shapes table)
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
    print("Database successfully built with routes, stops, shapes and full variant coverage!")


if __name__ == "__main__":
    build_database()
