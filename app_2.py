import os
import json
import re
from html import escape
from uuid import uuid4
from datetime import date, time
from pathlib import Path
import requests
import pyodbc
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

# Load environment variables from .env file - explicitly specify the path
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

API_KEY = os.getenv('API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

print(f"[app_2.py] API_KEY loaded: {API_KEY is not None}")
print(f"[app_2.py] DATABASE_URL loaded: {DATABASE_URL is not None}")

def _parse_database_name(url):
    if not url or '/' not in url:
        return 'Fuel_Station'
    tail = url.rsplit('/', 1)[1]
    return tail.split('?', 1)[0] or 'Fuel_Station'


def get_db_connection():
    """Create a database connection using a set of local SQL Server candidates."""
    database_name = _parse_database_name(DATABASE_URL or os.getenv('DATABASE_URL'))
    conn_str = (
        'DRIVER={ODBC Driver 17 for SQL Server};'
        'SERVER=DESKTOP-7K48LCE\\SQLEXPRESS;'
        f'DATABASE={database_name};'
        'Trusted_Connection=yes;'
        'Encrypt=no;'
        'TrustServerCertificate=yes;'
    )

    try:
        return pyodbc.connect(conn_str, timeout=5)
    except Exception as first_error:
        fallback_str = (
            'DRIVER={ODBC Driver 17 for SQL Server};'
            'SERVER=localhost\\SQLEXPRESS;'
            f'DATABASE={database_name};'
            'Trusted_Connection=yes;'
            'Encrypt=no;'
            'TrustServerCertificate=yes;'
        )
        try:
            return pyodbc.connect(fallback_str, timeout=5)
        except Exception as second_error:
            print(f'Database connection error: {first_error} | fallback: {second_error}')
            return None


def _get_truck_details_columns(cursor):
    """Resolve table/schema plus truck name and fuel-average columns."""
    cursor.execute(
        """
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME IN ('Truck_Detail', 'Truck_Details')
            ORDER BY
                CASE WHEN TABLE_SCHEMA = 'dbo' THEN 0 ELSE 1 END,
                CASE WHEN TABLE_NAME = 'Truck_Detail' THEN 0 ELSE 1 END,
                ORDINAL_POSITION
        """
    )
    rows = cursor.fetchall()
    if not rows:
        return None, None, None, None

    table_columns = {}
    for row in rows:
        key = (row[0], row[1])
        table_columns.setdefault(key, []).append((row[2], (row[3] or '').lower()))

    def pick_columns(columns_with_types):
        columns = [c[0] for c in columns_with_types]
        normalized = {col.lower(): col for col in columns}

        truck_name_candidates = [
            'truck_name',
            'truckname',
            'name',
            'truck',
            'truck_no',
            'truck_number',
            'vehicle_name',
            'vehicle'
        ]
        fuel_avg_candidates = [
            'fuel_average_l',
            'fuelaverage_l',
            'fuel_avg_l',
            'average_fuel_l',
            'avg_fuel_l',
            'fuel_average',
            'average_fuel',
            'fuel_avg'
        ]

        truck_name_col = next((normalized[c] for c in truck_name_candidates if c in normalized), None)
        fuel_avg_col = next((normalized[c] for c in fuel_avg_candidates if c in normalized), None)

        if not truck_name_col:
            truck_name_col = next(
                (col for col in columns if 'truck' in col.lower() and 'id' not in col.lower()),
                None
            )

        if not truck_name_col:
            truck_name_col = next(
                (col for col in columns if 'vehicle' in col.lower() and 'id' not in col.lower()),
                None
            )

        if not truck_name_col:
            truck_name_col = next(
                (
                    col for col in columns
                    if 'fuel' not in col.lower()
                    and 'avg' not in col.lower()
                    and 'average' not in col.lower()
                    and 'liter' not in col.lower()
                    and col.lower() not in {'id'}
                ),
                None
            )

        if not truck_name_col and columns:
            truck_name_col = columns[0]

        if not fuel_avg_col:
            fuel_avg_col = next(
                (
                    col for col in columns
                    if 'fuel' in col.lower() and ('avg' in col.lower() or 'average' in col.lower())
                ),
                None
            )

        if not fuel_avg_col:
            fuel_avg_col = next((col for col in columns if 'fuel' in col.lower()), None)

        if not fuel_avg_col:
            numeric_types = {'decimal', 'numeric', 'float', 'real', 'int', 'bigint', 'smallint'}
            fuel_avg_col = next(
                (col for col, data_type in columns_with_types if data_type in numeric_types),
                None
            )

        return truck_name_col, fuel_avg_col

    best = None
    best_score = -1
    for (table_schema, table_name), columns_with_types in table_columns.items():
        truck_name_col, fuel_avg_col = pick_columns(columns_with_types)
        score = 0
        if truck_name_col:
            score += 2
        if fuel_avg_col:
            score += 2
        if table_schema == 'dbo':
            score += 1
        if table_name == 'Truck_Detail':
            score += 1

        if score > best_score:
            best_score = score
            best = (table_schema, table_name, truck_name_col, fuel_avg_col)

    return best if best else (None, None, None, None)


def _split_location_label(location_name):
    """Split a display label like 'City, Address' into searchable parts."""
    text = str(location_name or '').strip()
    if not text:
        return '', ''

    if ', ' in text:
        city, address = text.split(', ', 1)
        return city.strip(), address.strip()

    if ',' in text:
        city, address = text.split(',', 1)
        return city.strip(), address.strip()

    return text, ''


def get_available_trucks():
    """Return all truck names from Truck_Detail(s) table."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {'success': False, 'error': 'Database connection failed'}

        cursor = conn.cursor()
        table_schema, table_name, truck_name_col, fuel_avg_col = _get_truck_details_columns(cursor)
        if not truck_name_col:
            return {
                'success': False,
                'error': 'Truck name column not found in Truck_Detail/Truck_Details'
            }

        table_ref = f"[{table_schema}].[{table_name}]" if table_schema and table_name else "dbo.Truck_Details"

        query = f"""
            SELECT DISTINCT [{truck_name_col}]
            FROM {table_ref}
            WHERE [{truck_name_col}] IS NOT NULL
            ORDER BY [{truck_name_col}]
        """
        cursor.execute(query)

        trucks = [str(row[0]).strip() for row in cursor.fetchall() if str(row[0]).strip()]
        return {'success': True, 'trucks': trucks}
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()


def get_truck_fuel_average(truck_name):
    """Get Fuel_Average_L (or equivalent) for a selected truck name."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {'success': False, 'error': 'Database connection failed'}

        cursor = conn.cursor()
        table_schema, table_name, truck_name_col, fuel_avg_col = _get_truck_details_columns(cursor)
        if not truck_name_col or not fuel_avg_col:
            return {
                'success': False,
                'error': 'Required columns not found in Truck_Detail/Truck_Details'
            }

        table_ref = f"[{table_schema}].[{table_name}]" if table_schema and table_name else "dbo.Truck_Details"

        query = f"""
            SELECT TOP 1 [{truck_name_col}], [{fuel_avg_col}]
            FROM {table_ref}
            WHERE LOWER(LTRIM(RTRIM(CAST([{truck_name_col}] AS NVARCHAR(255))))) = LOWER(?)
        """
        cursor.execute(query, (truck_name.strip(),))
        row = cursor.fetchone()

        if not row:
            return {'success': False, 'error': f'Truck "{truck_name}" not found'}

        try:
            fuel_avg = float(row[1])
        except (TypeError, ValueError):
            return {
                'success': False,
                'error': f'Invalid fuel average for truck "{truck_name}"'
            }

        if fuel_avg <= 0:
            return {
                'success': False,
                'error': f'Fuel average must be greater than 0 for truck "{truck_name}"'
            }

        return {
            'success': True,
            'truck_name': str(row[0]).strip(),
            'fuel_average_l': fuel_avg
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()

def get_coordinates_from_database(location_name, city=None, address=None):
    """
    Search for a location in the LovesLocations table and return its coordinates
    If city or address are provided, use exact match first.
    Otherwise search by LIKE pattern on location_name
    Returns: dict with latitude and longitude, or None if not found
    """
    try:
        conn = get_db_connection()
        if not conn:
            return {'found': False, 'error': 'Database connection failed'}
        
        cursor = conn.cursor()

        display_city, display_address = _split_location_label(location_name)
        city = (city or display_city or '').strip()
        address = (address or display_address or '').strip()

        if city and address:
            query = """
                SELECT TOP 1 Latitude, Longitude, City, Address
                FROM dbo.LovesLocations
                WHERE LTRIM(RTRIM(City)) = ?
                  AND LTRIM(RTRIM(Address)) = ?
                ORDER BY City, Address
            """
            cursor.execute(query, (city, address))
            row = cursor.fetchone()

            if row:
                return {
                    'latitude': float(row[0]),
                    'longitude': float(row[1]),
                    'city': row[2],
                    'address': row[3],
                    'found': True
                }
        
        # If city is provided, try exact match first
        if city:
            query = """
                SELECT TOP 1 Latitude, Longitude, City, Address
                FROM dbo.LovesLocations
                WHERE LTRIM(RTRIM(City)) = ?
                ORDER BY City, Address
            """
            cursor.execute(query, (city,))
            row = cursor.fetchone()
            
            if row:
                return {
                    'latitude': float(row[0]),
                    'longitude': float(row[1]),
                    'city': row[2],
                    'address': row[3],
                    'found': True
                }
        
        # Fall back to LIKE search on location_name
        query = """
            SELECT TOP 1 Latitude, Longitude, City, Address
            FROM dbo.LovesLocations
            WHERE LTRIM(RTRIM(City)) = ?
               OR LTRIM(RTRIM(Address)) = ?
               OR City LIKE ?
               OR Address LIKE ?
               OR (City + ', ' + ISNULL(Address, '')) LIKE ?
            ORDER BY City, Address
        """

        term = location_name.strip()
        cursor.execute(query, (term, term, f"%{term}%", f"%{term}%", f"%{term}%"))
        row = cursor.fetchone()
        
        if row:
            return {
                'latitude': float(row[0]),
                'longitude': float(row[1]),
                'city': row[2],
                'address': row[3],
                'found': True
            }
        else:
            return {'found': False, 'message': f'Location "{location_name}" not found in database'}
    
    except Exception as e:
        return {'found': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()


def _resolve_location_via_ors(location_text):
    """Resolve a user-provided location name to coordinates with OpenRouteService geocoding."""
    query = str(location_text or '').strip()
    if not query:
        return {'success': False, 'error': 'Location text is required'}

    if not API_KEY:
        return {'success': False, 'error': 'API_KEY is not configured'}

    try:
        response = requests.get(
            'https://api.openrouteservice.org/geocode/search',
            headers={'Authorization': API_KEY, 'Accept': 'application/json'},
            params={'text': query, 'size': 1},
            timeout=10,
        )

        if response.status_code != 200:
            return {
                'success': False,
                'error': f'Geocoding error: {response.status_code} - {response.text}',
            }

        payload = response.json()
        features = payload.get('features') or []
        if not features:
            return {'success': False, 'error': f'Location "{query}" could not be resolved by OpenRouteService'}

        feature = features[0]
        geometry = feature.get('geometry') or {}
        coordinates = geometry.get('coordinates') or []
        if len(coordinates) < 2:
            return {'success': False, 'error': f'Invalid coordinates returned for "{query}"'}

        properties = feature.get('properties') or {}
        resolved_label = properties.get('label') or properties.get('name') or query

        return {
            'success': True,
            'found': True,
            'latitude': float(coordinates[1]),
            'longitude': float(coordinates[0]),
            'label': resolved_label,
            'source': 'openrouteservice',
            'raw': feature,
        }
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Geocoding request timed out'}
    except requests.exceptions.RequestException as error:
        return {'success': False, 'error': f'Geocoding request error: {str(error)}'}
    except Exception as error:
        return {'success': False, 'error': f'Geocoding error: {str(error)}'}


def resolve_location_to_coordinates(location_name, city=None, address=None):
    """Resolve a location using OpenRouteService first, then fall back to the database lookup."""
    search_parts = [location_name, city, address]
    search_text = ', '.join(part.strip() for part in search_parts if part and str(part).strip())

    if not search_text:
        return {'success': False, 'error': 'Location text is required'}

    geocode_result = _resolve_location_via_ors(search_text)
    if geocode_result.get('success'):
        geocode_result.setdefault('label', search_text)
        return geocode_result

    fallback = get_coordinates_from_database(location_name, city=city, address=address)
    if fallback.get('found'):
        return {
            'success': True,
            'found': True,
            'latitude': fallback['latitude'],
            'longitude': fallback['longitude'],
            'city': fallback.get('city'),
            'address': fallback.get('address'),
            'label': fallback.get('address') or fallback.get('city') or location_name,
            'source': 'database',
        }

    if fallback.get('error'):
        return {'success': False, 'error': fallback['error']}

    return {'success': False, 'error': geocode_result.get('error') or fallback.get('message') or f'Location "{location_name}" not found'}


def _haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate great-circle distance between two points on Earth in kilometers."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371
    lat1_rad = radians(lat1)
    lon1_rad = radians(lon1)
    lat2_rad = radians(lat2)
    lon2_rad = radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


def _distance_to_route_segment(point_lat, point_lon, seg_lat1, seg_lon1, seg_lat2, seg_lon2):
    """Calculate minimum distance from a point to a line segment."""
    lat1, lon1 = seg_lat1, seg_lon1
    lat2, lon2 = seg_lat2, seg_lon2
    lat_p, lon_p = point_lat, point_lon
    
    dy = lat2 - lat1
    dx = lon2 - lon1
    if dx == 0 and dy == 0:
        return _haversine_distance(lat_p, lon_p, lat1, lon1)
    
    t = max(0, min(1, ((lat_p - lat1) * dy + (lon_p - lon1) * dx) / (dy * dy + dx * dx)))
    proj_lat = lat1 + t * dy
    proj_lon = lon1 + t * dx
    return _haversine_distance(lat_p, lon_p, proj_lat, proj_lon)


def _find_nearest_point_on_route(point_lat, point_lon, route_coords):
    """Find the nearest point on the route and return distance from route start."""
    if not route_coords:
        return None
    
    min_distance_to_route = float('inf')
    distance_along_route = 0
    nearest_index = 0
    
    for i in range(len(route_coords) - 1):
        seg_lat1, seg_lon1 = route_coords[i]
        seg_lat2, seg_lon2 = route_coords[i + 1]
        
        distance_to_segment = _distance_to_route_segment(
            point_lat, point_lon,
            seg_lat1, seg_lon1,
            seg_lat2, seg_lon2
        )
        
        if distance_to_segment < min_distance_to_route:
            min_distance_to_route = distance_to_segment
            nearest_index = i
    
    for i in range(nearest_index + 1):
        if i == 0:
            continue
        seg_lat1, seg_lon1 = route_coords[i - 1]
        seg_lat2, seg_lon2 = route_coords[i]
        distance_along_route += _haversine_distance(seg_lat1, seg_lon1, seg_lat2, seg_lon2)
    
    return {
        'distance_to_route_km': round(min_distance_to_route, 2),
        'distance_along_route_km': round(distance_along_route, 2),
    }


def _decode_ors_polyline(encoded_geometry):
    """Decode an ORS encoded polyline into a list of (lat, lon) tuples."""
    if not isinstance(encoded_geometry, str) or not encoded_geometry:
        return []

    coordinates = []
    index = 0
    latitude = 0
    longitude = 0
    length = len(encoded_geometry)

    while index < length:
        shift = 0
        result = 0
        while True:
            if index >= length:
                return coordinates
            value = ord(encoded_geometry[index]) - 63
            index += 1
            result |= (value & 0x1F) << shift
            shift += 5
            if value < 0x20:
                break
        delta_lat = ~(result >> 1) if result & 1 else (result >> 1)
        latitude += delta_lat

        shift = 0
        result = 0
        while True:
            if index >= length:
                return coordinates
            value = ord(encoded_geometry[index]) - 63
            index += 1
            result |= (value & 0x1F) << shift
            shift += 5
            if value < 0x20:
                break
        delta_lon = ~(result >> 1) if result & 1 else (result >> 1)
        longitude += delta_lon

        coordinates.append((latitude / 1e5, longitude / 1e5))

    return coordinates


def _extract_route_coordinates(route_geometry):
    """Extract route coordinates from ORS geometry in whichever format was returned."""
    if isinstance(route_geometry, dict) and route_geometry.get('type') == 'LineString':
        return [(coord[1], coord[0]) for coord in route_geometry.get('coordinates', [])]

    if isinstance(route_geometry, str):
        return _decode_ors_polyline(route_geometry)

    return []


def _append_exact_endpoint_matches(addresses_along_route, all_addresses, start_lat, start_lon, end_lat, end_lon):
    """Guarantee exact start/end database addresses appear in the response."""
    endpoint_tolerance_km = 0.25
    existing_keys = {
        (entry['city'], entry['address'], round(entry['latitude'], 6), round(entry['longitude'], 6))
        for entry in addresses_along_route
    }

    for addr in all_addresses:
        is_start = _haversine_distance(start_lat, start_lon, addr['latitude'], addr['longitude']) <= endpoint_tolerance_km
        is_end = _haversine_distance(end_lat, end_lon, addr['latitude'], addr['longitude']) <= endpoint_tolerance_km

        if not (is_start or is_end):
            continue

        key = (addr['city'], addr['address'], round(addr['latitude'], 6), round(addr['longitude'], 6))
        if key in existing_keys:
            continue

        distance_from_start = _haversine_distance(start_lat, start_lon, addr['latitude'], addr['longitude'])
        addresses_along_route.append({
            'store_number': addr.get('store_number'),
            'city': addr['city'],
            'address': addr['address'],
            'latitude': addr['latitude'],
            'longitude': addr['longitude'],
            'distance_from_start_km': round(distance_from_start, 2),
            'distance_from_route_km': 0.0,
            'distance_along_route_km': round(distance_from_start, 2),
            'is_exact_endpoint_match': True,
        })

    return addresses_along_route


def _parse_savings_value(value):
    """Convert a Savings value from the discount table into a comparable number."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    is_negative = text.startswith('(') and text.endswith(')')
    cleaned = (
        text.replace('$', '')
        .replace('%', '')
        .replace(',', '')
        .replace('(', '')
        .replace(')', '')
        .strip()
    )

    try:
        parsed = float(cleaned)
    except (TypeError, ValueError):
        numeric_match = re.search(r'[-+]?\d+(?:\.\d+)?', cleaned)
        if not numeric_match:
            return None
        try:
            parsed = float(numeric_match.group(0))
        except (TypeError, ValueError):
            return None

    return -parsed if is_negative else parsed


def _normalize_store_number_key(value):
    """Normalize store numbers for lookup without changing database values."""
    key = str(value or '').strip()
    return key[:-2] if key.endswith('.0') else key


def _resolve_table_reference(cursor, table_name):
    """Return a bracketed table reference, preferring dbo when available."""
    cursor.execute(
        """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = ?
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY CASE WHEN TABLE_SCHEMA = 'dbo' THEN 0 ELSE 1 END, TABLE_SCHEMA
        """,
        (table_name,),
    )
    row = cursor.fetchone()
    if not row:
        return f"[dbo].[{table_name}]"
    return f"[{row[0]}].[{row[1]}]"


def _load_station_savings_by_store_number(store_numbers):
    """Load Savings values by joining LovesLocations to Discounted_Fuel_Price."""
    normalized_store_numbers = []
    seen = set()
    for store_number in store_numbers:
        key = _normalize_store_number_key(store_number)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized_store_numbers.append(key)

    if not normalized_store_numbers:
        return {}

    conn = None
    savings_by_store = {}
    try:
        conn = get_db_connection()
        if not conn:
            return {}

        cursor = conn.cursor()
        loves_ref = _resolve_table_reference(cursor, 'LovesLocations')
        discounted_ref = _resolve_table_reference(cursor, 'Discounted_Fuel_Price')

        for start in range(0, len(normalized_store_numbers), 900):
            chunk = normalized_store_numbers[start:start + 900]
            placeholders = ','.join('?' for _ in chunk)
            query = f"""
                SELECT
                    LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) AS StoreNumber,
                    ll.[Address],
                    dfp.[Savings]
                FROM {loves_ref} ll
                INNER JOIN {discounted_ref} dfp
                    ON LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) =
                       LTRIM(RTRIM(CAST(dfp.[Name] AS NVARCHAR(255))))
                WHERE LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) IN ({placeholders})
            """
            cursor.execute(query, chunk)

            for row in cursor.fetchall():
                store_key = _normalize_store_number_key(row[0])
                parsed_savings = _parse_savings_value(row[2])
                if parsed_savings is None:
                    continue

                existing = savings_by_store.get(store_key)
                if not existing or parsed_savings > existing['savings']:
                    savings_by_store[store_key] = {
                        'address': row[1],
                        'savings': parsed_savings,
                        'savings_display': str(row[2]).strip(),
                    }

            missing_chunk = [
                store_number for store_number in chunk
                if store_number not in savings_by_store
            ]
            if not missing_chunk:
                continue

            placeholders = ','.join('?' for _ in missing_chunk)
            fallback_query = f"""
                SELECT
                    LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) AS StoreNumber,
                    ll.[Address],
                    dfp.[Savings]
                FROM {loves_ref} ll
                INNER JOIN {discounted_ref} dfp
                    ON REPLACE(LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))), '.0', '') =
                       REPLACE(LTRIM(RTRIM(CAST(dfp.[Name] AS NVARCHAR(255)))), '.0', '')
                WHERE REPLACE(LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))), '.0', '') IN ({placeholders})
            """
            cursor.execute(fallback_query, missing_chunk)

            for row in cursor.fetchall():
                store_key = _normalize_store_number_key(row[0])
                parsed_savings = _parse_savings_value(row[2])
                if parsed_savings is None:
                    continue

                existing = savings_by_store.get(store_key)
                if not existing or parsed_savings > existing['savings']:
                    savings_by_store[store_key] = {
                        'address': row[1],
                        'savings': parsed_savings,
                        'savings_display': str(row[2]).strip(),
                    }

        return savings_by_store
    except Exception as e:
        print(f'Error loading discounted fuel savings: {e}')
        return {}
    finally:
        if conn:
            conn.close()


def _load_station_savings_by_address(addresses_along_route):
    """Recover savings for route stations by address/city, still using StoreNumber = Name."""
    conn = None
    savings_by_address = {}
    try:
        conn = get_db_connection()
        if not conn:
            return {}

        cursor = conn.cursor()
        loves_ref = _resolve_table_reference(cursor, 'LovesLocations')
        discounted_ref = _resolve_table_reference(cursor, 'Discounted_Fuel_Price')

        query = f"""
            SELECT TOP 1
                LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) AS StoreNumber,
                ll.[Address],
                ll.[City],
                dfp.[Savings]
            FROM {loves_ref} ll
            INNER JOIN {discounted_ref} dfp
                ON LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) =
                   LTRIM(RTRIM(CAST(dfp.[Name] AS NVARCHAR(255))))
            WHERE LOWER(LTRIM(RTRIM(CAST(ll.[Address] AS NVARCHAR(255))))) = LOWER(?)
              AND LOWER(LTRIM(RTRIM(CAST(ll.[City] AS NVARCHAR(255))))) = LOWER(?)
        """
        fallback_query = f"""
            SELECT TOP 1
                LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))) AS StoreNumber,
                ll.[Address],
                ll.[City],
                dfp.[Savings]
            FROM {loves_ref} ll
            INNER JOIN {discounted_ref} dfp
                ON REPLACE(LTRIM(RTRIM(CAST(ll.[StoreNumber] AS NVARCHAR(255)))), '.0', '') =
                   REPLACE(LTRIM(RTRIM(CAST(dfp.[Name] AS NVARCHAR(255)))), '.0', '')
            WHERE LOWER(LTRIM(RTRIM(CAST(ll.[Address] AS NVARCHAR(255))))) = LOWER(?)
              AND LOWER(LTRIM(RTRIM(CAST(ll.[City] AS NVARCHAR(255))))) = LOWER(?)
        """

        for point in addresses_along_route:
            address = str(point.get('address') or '').strip()
            city = str(point.get('city') or '').strip()
            if not address or not city:
                continue

            address_key = (address.lower(), city.lower())
            if address_key in savings_by_address:
                continue

            row = None
            for station_query in (query, fallback_query):
                cursor.execute(station_query, (address, city))
                row = cursor.fetchone()
                if row:
                    break

            if not row:
                continue

            parsed_savings = _parse_savings_value(row[3])
            if parsed_savings is None:
                continue

            savings_by_address[address_key] = {
                'store_number': row[0],
                'address': row[1],
                'city': row[2],
                'savings': parsed_savings,
                'savings_display': str(row[3]).strip(),
            }

        return savings_by_address
    except Exception as e:
        print(f'Error loading discounted fuel savings by address: {e}')
        return {}
    finally:
        if conn:
            conn.close()


def _attach_savings_to_route_stations(addresses_along_route):
    """Attach Discounted_Fuel_Price.Savings to the existing route station list."""
    savings_by_store = _load_station_savings_by_store_number(
        point.get('store_number') for point in addresses_along_route
    )

    for point in addresses_along_route:
        store_key = _normalize_store_number_key(point.get('store_number'))
        discount = savings_by_store.get(store_key)
        if not discount:
            continue

        point['savings'] = discount['savings']
        point['savings_display'] = discount['savings_display']
        point['discount_address'] = discount.get('address') or point.get('address')

    missing_savings_points = [
        point for point in addresses_along_route
        if point.get('savings') is None
    ]
    if missing_savings_points:
        savings_by_address = _load_station_savings_by_address(missing_savings_points)
        for point in missing_savings_points:
            address_key = (
                str(point.get('address') or '').strip().lower(),
                str(point.get('city') or '').strip().lower(),
            )
            discount = savings_by_address.get(address_key)
            if not discount:
                continue

            point['store_number'] = point.get('store_number') or discount.get('store_number')
            point['savings'] = discount['savings']
            point['savings_display'] = discount['savings_display']
            point['discount_address'] = discount.get('address') or point.get('address')

    return addresses_along_route


def _build_fuel_management_summary_legacy(
    addresses_along_route,
    route_distance_km,
    current_fuel_l,
    fuel_average_l,
):
    """Compute safe fuel range and mark the best fueling stop on the route."""
    reserve_buffer_km = 20.0
    current_fuel_l = max(float(current_fuel_l or 0), 0.0)
    fuel_average_l = float(fuel_average_l or 0)

    available_travel_range_km = round((current_fuel_l / fuel_average_l) * 100, 2) if fuel_average_l > 0 else 0.0
    safe_operating_range_km = round(max(available_travel_range_km - reserve_buffer_km, 0.0), 2)
    fuel_is_sufficient = bool(route_distance_km and route_distance_km <= safe_operating_range_km)

    _attach_savings_to_route_stations(addresses_along_route)
    for point in addresses_along_route:
        point['recommended_fueling_point'] = False
        point['best_savings_recommendation'] = False
        point['is_reachable_with_current_fuel'] = (
            float(point.get('distance_from_start_km', 0)) <= available_travel_range_km
        )

    route_points = [
        point
        for point in addresses_along_route
        if 0 < float(point.get('distance_from_start_km', 0)) < float(route_distance_km or 0)
    ]

    recommended_stop = None
    recommended_reason = None
    reachable_points = []
    best_savings_stop = None

    if route_points:
        reachable_points = [
            point for point in route_points
            if float(point.get('distance_from_start_km', 0)) <= available_travel_range_km
        ]
        points_with_savings = [
            point for point in reachable_points
            if point.get('savings') is not None
        ]

        if points_with_savings:
            best_savings_stop = sorted(
                points_with_savings,
                key=lambda point: (
                    -float(point.get('savings', 0)),
                    float(point.get('distance_from_start_km', 0)),
                ),
            )[0]
            recommended_stop = best_savings_stop
            recommended_reason = 'best_savings'
        elif reachable_points:
            recommended_stop = max(reachable_points, key=lambda point: float(point.get('distance_from_start_km', 0)))
            recommended_reason = 'optional' if fuel_is_sufficient else 'planned'
        else:
            recommended_stop = min(route_points, key=lambda point: float(point.get('distance_from_start_km', 0)))
            recommended_reason = 'emergency'

        recommended_stop['recommended_fueling_point'] = True
        recommended_stop['best_savings_recommendation'] = recommended_reason == 'best_savings'
        recommended_stop['fuel_stop_label'] = (
            'BEST FUEL SAVINGS STOP'
            if recommended_reason == 'best_savings'
            else 'OPTIONAL FUEL STOP' if fuel_is_sufficient else 'RECOMMENDED FUEL STOP'
        )
        recommended_stop['fuel_stop_reason'] = recommended_reason
        recommended_stop['fuel_stop_marker_label'] = (
            'BEST FUEL SAVINGS STOP'
            if recommended_reason == 'best_savings'
            else 'OPTIONAL FUEL STOP' if fuel_is_sufficient else 'RECOMMENDED FUEL STOP'
        )
        recommended_stop['available_travel_range_km'] = available_travel_range_km
        recommended_stop['safe_operating_range_km'] = safe_operating_range_km
        recommended_stop['reserve_buffer_km'] = reserve_buffer_km
        distance_to_stop_km = float(recommended_stop.get('distance_from_start_km', 0))
        remaining_fuel_at_stop_l = max(current_fuel_l - (distance_to_stop_km * fuel_average_l / 100.0), 0.0)
        recommended_stop['fuel_remaining_at_stop_l'] = round(remaining_fuel_at_stop_l, 2)
        recommended_stop['remaining_range_after_stop_km'] = round(max(available_travel_range_km - distance_to_stop_km, 0.0), 2)
        recommended_stop['priority'] = 'LOW' if fuel_is_sufficient else 'HIGH'
        recommended_stop['warning'] = 'OPTIONAL RECOMMENDATION' if fuel_is_sufficient else 'REFUEL REQUIRED'
        recommended_stop['emergency_fuel_stop'] = not fuel_is_sufficient

    savings_verification_rows = []
    for point in route_points:
        savings_value = point.get('savings')
        savings_verification_rows.append({
            'Station address': f"{point.get('address', '')}, {point.get('city', '')}".strip(', '),
            'Distance from start': float(point.get('distance_from_start_km', 0)),
            'Savings value': savings_value,
            'Reachable': bool(point.get('is_reachable_with_current_fuel')),
            'Selected as best': bool(point.get('best_savings_recommendation')),
        })

    return {
        'current_fuel_l': round(current_fuel_l, 2),
        'fuel_average_l_per_100km': round(fuel_average_l, 2),
        'available_travel_range_km': available_travel_range_km,
        'safe_operating_range_km': safe_operating_range_km,
        'reserve_buffer_km': reserve_buffer_km,
        'fuel_status': 'Sufficient' if fuel_is_sufficient else 'Insufficient',
        'fuel_status_message': (
            'The current fuel is sufficient to safely reach the destination while maintaining the required emergency reserve.'
            if fuel_is_sufficient
            else 'The current fuel level is not sufficient to safely complete the trip while maintaining the required reserve fuel.'
        ),
        'fuel_status_detail': (
            'Fuel Status: Sufficient'
            if fuel_is_sufficient
            else 'Fuel Status: Insufficient'
        ),
        'emergency_fuel_alert': not fuel_is_sufficient,
        'emergency_fuel_alert_label': '⚠️ EMERGENCY FUEL ALERT' if not fuel_is_sufficient else None,
        'mandatory_refuel_instruction': (
            'You must refuel the truck at the recommended fueling point. Failure to refuel may result in fuel deficiency before reaching the destination.'
            if not fuel_is_sufficient
            else None
        ),
        'recommended_stop_required': bool(route_distance_km and route_distance_km > safe_operating_range_km),
        'recommended_fueling_point': recommended_stop,
        'recommended_fueling_reason': recommended_reason,
        'reachable_fueling_points_count': len(reachable_points),
        'best_savings_fueling_point': best_savings_stop,
        'savings_verification_rows': savings_verification_rows,
    }


def _fuel_required_l_for_distance(distance_km, fuel_average_l_per_100km):
    """Truck_Detail.Fuel_Average_L is treated consistently as litres per 100km."""
    fuel_average_l_per_100km = float(fuel_average_l_per_100km or 0)
    if fuel_average_l_per_100km <= 0:
        return 0.0
    return float(distance_km or 0) * fuel_average_l_per_100km / 100.0


def _range_km_for_fuel(fuel_l, fuel_average_l_per_100km):
    """Return travel range in km for a fuel amount using L/100km consumption."""
    fuel_average_l_per_100km = float(fuel_average_l_per_100km or 0)
    if fuel_average_l_per_100km <= 0:
        return 0.0
    return float(fuel_l or 0) / fuel_average_l_per_100km * 100.0


def _route_position_km(point):
    """Use route-progress distance for planning, falling back to straight-line distance."""
    value = point.get('distance_along_route_km')
    if value is None:
        value = point.get('distance_from_start_km', 0)
    return float(value or 0)


def _station_identity(point):
    store_key = _normalize_store_number_key(point.get('store_number'))
    if store_key:
        return ('store', store_key)
    return (
        'address',
        str(point.get('address') or '').strip().lower(),
        str(point.get('city') or '').strip().lower(),
        round(float(point.get('latitude') or 0), 5),
        round(float(point.get('longitude') or 0), 5),
    )


def _dedupe_route_stations(route_points):
    deduped = {}
    for point in route_points:
        key = _station_identity(point)
        existing = deduped.get(key)
        if not existing or _route_position_km(point) < _route_position_km(existing):
            deduped[key] = point
    return sorted(deduped.values(), key=_route_position_km)


def _select_best_station(candidates, prefer_savings=True):
    if not candidates:
        return None
    if prefer_savings:
        with_savings = [point for point in candidates if point.get('savings') is not None]
        if with_savings:
            return sorted(
                with_savings,
                key=lambda point: (
                    -float(point.get('savings') or 0),
                    -_route_position_km(point),
                    float(point.get('distance_from_route_km') or 0),
                ),
            )[0]
    return sorted(
        candidates,
        key=lambda point: (
            -_route_position_km(point),
            float(point.get('distance_from_route_km') or 0),
        ),
    )[0]


def _build_fuel_management_summary(
    addresses_along_route,
    route_distance_km,
    current_fuel_l,
    fuel_average_l,
):
    """Plan one or more required refueling stops along the selected route."""
    tank_capacity_l = 200.0
    half_tank_l = 100.0
    reserve_buffer_km = 20.0
    current_fuel_l = min(max(float(current_fuel_l or 0), 0.0), tank_capacity_l)
    fuel_average_l = float(fuel_average_l or 0)
    route_distance_km = float(route_distance_km or 0)

    available_travel_range_km = round(_range_km_for_fuel(current_fuel_l, fuel_average_l), 2)
    safe_operating_range_km = round(max(available_travel_range_km - reserve_buffer_km, 0.0), 2)

    _attach_savings_to_route_stations(addresses_along_route)
    for point in addresses_along_route:
        point['recommended_fueling_point'] = False
        point['best_savings_recommendation'] = False
        point.pop('fuel_stop_marker_label', None)
        point.pop('stop_sequence', None)
        point['is_reachable_with_current_fuel'] = _route_position_km(point) <= safe_operating_range_km

    route_points = _dedupe_route_stations([
        point for point in addresses_along_route
        if 0 < _route_position_km(point) < route_distance_km
    ])

    recommended_fueling_points = []
    selected_station_ids = set()
    verification_by_id = {}
    safety_failure = None
    current_position_km = 0.0
    fuel_available_l = current_fuel_l
    first_leg = True
    safe_range_km = safe_operating_range_km

    # Multi-stop planner: after each refuel the tank is full, and later stops
    # are preferred only after at least half the tank has been used.
    for _ in range(len(route_points) + 2):
        remaining_distance_km = max(route_distance_km - current_position_km, 0.0)
        safe_range_km = max(_range_km_for_fuel(fuel_available_l, fuel_average_l) - reserve_buffer_km, 0.0)
        if route_distance_km and remaining_distance_km <= safe_range_km:
            break

        safe_limit_km = current_position_km + safe_range_km
        half_tank_range_km = _range_km_for_fuel(half_tank_l, fuel_average_l)
        half_tank_threshold_km = current_position_km + half_tank_range_km
        reachable_candidates = [
            point for point in route_points
            if _station_identity(point) not in selected_station_ids
            and current_position_km < _route_position_km(point) <= safe_limit_km
        ]

        if not reachable_candidates:
            safety_failure = {
                'message': 'No safe refuelling station was found before the truck exceeds its safe operating fuel range.',
                'current_position_km': round(current_position_km, 2),
                'maximum_reachable_km': round(safe_limit_km, 2),
                'remaining_destination_distance_km': round(remaining_distance_km, 2),
            }
            break

        if first_leg:
            candidates = reachable_candidates
            selected_after_half_tank_usage = False
            half_tank_usage_required = False
            fallback_warning = None
        else:
            after_half_candidates = [
                point for point in reachable_candidates
                if _route_position_km(point) > half_tank_threshold_km
            ]
            candidates = after_half_candidates or reachable_candidates
            selected_after_half_tank_usage = bool(after_half_candidates)
            half_tank_usage_required = True
            fallback_warning = None if after_half_candidates else 'Selected before half-tank usage because no safe later station exists.'

        selected_stop = _select_best_station(candidates, prefer_savings=True)
        if not selected_stop:
            safety_failure = {
                'message': 'No safe refuelling station was found before the truck exceeds its safe operating fuel range.',
                'current_position_km': round(current_position_km, 2),
                'maximum_reachable_km': round(safe_limit_km, 2),
                'remaining_destination_distance_km': round(remaining_distance_km, 2),
            }
            break

        stop_position_km = _route_position_km(selected_stop)
        distance_from_previous_stop_km = max(stop_position_km - current_position_km, 0.0)
        fuel_used_since_previous_stop_l = _fuel_required_l_for_distance(distance_from_previous_stop_km, fuel_average_l)
        fuel_remaining_at_stop_l = max(fuel_available_l - fuel_used_since_previous_stop_l, 0.0)
        refuel_amount_l = max(tank_capacity_l - fuel_remaining_at_stop_l, 0.0)
        stop_sequence = len(recommended_fueling_points) + 1
        reason = 'highest savings reachable station' if selected_stop.get('savings') is not None else 'farthest safe reachable station'
        if fallback_warning:
            reason = 'emergency fallback before half-tank threshold'

        selected_stop.update({
            'stop_sequence': stop_sequence,
            'distance_along_route_km': round(stop_position_km, 2),
            'distance_from_previous_stop_km': round(distance_from_previous_stop_km, 2),
            'remaining_distance_after_stop_km': round(max(route_distance_km - stop_position_km, 0.0), 2),
            'fuel_used_since_previous_stop_l': round(fuel_used_since_previous_stop_l, 2),
            'fuel_remaining_at_stop_l': round(fuel_remaining_at_stop_l, 2),
            'refuel_amount_l': round(refuel_amount_l, 2),
            'fuel_after_refuel_l': tank_capacity_l,
            'tank_capacity_l': tank_capacity_l,
            'half_tank_l': half_tank_l,
            'half_tank_usage_required': half_tank_usage_required,
            'selected_after_half_tank_usage': selected_after_half_tank_usage,
            'priority': 'HIGH',
            'warning': fallback_warning or 'REFUEL REQUIRED',
            'fuel_stop_reason': reason,
            'fuel_stop_marker_label': f'FUEL STOP #{stop_sequence}',
            'recommended_fueling_point': True,
            'best_savings_recommendation': selected_stop.get('savings') is not None,
            'available_travel_range_km': round(_range_km_for_fuel(fuel_available_l, fuel_average_l), 2),
            'safe_operating_range_km': round(safe_range_km, 2),
            'reserve_buffer_km': reserve_buffer_km,
        })

        recommended_fueling_points.append(selected_stop)
        selected_station_ids.add(_station_identity(selected_stop))
        verification_by_id[_station_identity(selected_stop)] = {
            'selected_stop_number': stop_sequence,
            'reason': reason,
        }
        current_position_km = stop_position_km
        fuel_available_l = tank_capacity_l
        first_leg = False
    else:
        safety_failure = {
            'message': 'Fuel stop planning stopped before a safe destination plan was found.',
            'current_position_km': round(current_position_km, 2),
            'maximum_reachable_km': round(current_position_km + safe_range_km, 2),
            'remaining_destination_distance_km': round(max(route_distance_km - current_position_km, 0.0), 2),
        }

    recommended_stop = recommended_fueling_points[0] if recommended_fueling_points else None
    fuel_is_sufficient = bool(route_distance_km and not recommended_fueling_points and safety_failure is None)
    can_reach_destination_after_last_stop = safety_failure is None
    best_savings_stop = next((point for point in recommended_fueling_points if point.get('savings') is not None), None)
    reachable_points = [point for point in route_points if _route_position_km(point) <= safe_operating_range_km]

    savings_verification_rows = []
    for point in route_points:
        selected_info = verification_by_id.get(_station_identity(point), {})
        selected_number = selected_info.get('selected_stop_number')
        previous_selected_position = 0.0
        if selected_number and selected_number > 1:
            previous_selected_position = _route_position_km(recommended_fueling_points[selected_number - 2])
        point_position = _route_position_km(point)
        distance_from_previous = max(point_position - previous_selected_position, 0.0)
        savings_verification_rows.append({
            'Station address': f"{point.get('address', '')}, {point.get('city', '')}".strip(', '),
            'Distance along route': round(point_position, 2),
            'Distance from previous selected stop': round(distance_from_previous, 2),
            'Reachable with current fuel': bool(point_position <= safe_operating_range_km),
            'Reachable with full tank': bool(distance_from_previous <= max(_range_km_for_fuel(tank_capacity_l, fuel_average_l) - reserve_buffer_km, 0.0)),
            'After half tank usage': bool(distance_from_previous > _range_km_for_fuel(half_tank_l, fuel_average_l)),
            'Savings': point.get('savings_display') or point.get('savings'),
            'Selected stop number': selected_number,
            'Reason': selected_info.get('reason') or (
                'outside current safe range'
                if point_position > safe_operating_range_km
                else 'candidate not selected'
            ),
        })

    mandatory_stop_required = bool(route_distance_km and route_distance_km > safe_operating_range_km)
    if safety_failure:
        fuel_status = 'Insufficient'
        fuel_status_message = (
            f"{safety_failure['message']} Current position: {safety_failure['current_position_km']} km; "
            f"maximum reachable: {safety_failure['maximum_reachable_km']} km; "
            f"remaining destination distance: {safety_failure['remaining_destination_distance_km']} km."
        )
    elif recommended_fueling_points:
        fuel_status = 'Insufficient'
        fuel_status_message = (
            f"{len(recommended_fueling_points)} refuelling stop(s) are required to safely reach the destination "
            f"while maintaining the {reserve_buffer_km:.0f} km emergency reserve."
        )
    else:
        fuel_status = 'Sufficient'
        fuel_status_message = 'The current fuel is sufficient to safely reach the destination while maintaining the required emergency reserve.'

    return {
        'current_fuel_l': round(current_fuel_l, 2),
        'fuel_average_l_per_100km': round(fuel_average_l, 2),
        'available_travel_range_km': available_travel_range_km,
        'safe_operating_range_km': safe_operating_range_km,
        'tank_capacity_l': tank_capacity_l,
        'half_tank_l': half_tank_l,
        'reserve_buffer_km': reserve_buffer_km,
        'fuel_status': fuel_status,
        'fuel_status_message': fuel_status_message,
        'fuel_status_detail': 'Fuel Status: Sufficient' if fuel_is_sufficient else 'Fuel Status: Insufficient',
        'emergency_fuel_alert': not fuel_is_sufficient,
        'emergency_fuel_alert_label': 'EMERGENCY FUEL ALERT' if not fuel_is_sufficient else None,
        'mandatory_refuel_instruction': (
            'You must refuel at the recommended fueling point(s). Failure to refuel may result in fuel deficiency before reaching the destination.'
            if mandatory_stop_required
            else None
        ),
        'recommended_stop_required': mandatory_stop_required,
        'recommended_fueling_point': recommended_stop,
        'recommended_fueling_points': recommended_fueling_points,
        'total_recommended_fuel_stops': len(recommended_fueling_points),
        'multi_stop_required': len(recommended_fueling_points) > 1,
        'can_reach_destination_after_last_stop': can_reach_destination_after_last_stop,
        'safety_failure': safety_failure,
        'recommended_fueling_reason': recommended_stop.get('fuel_stop_reason') if recommended_stop else None,
        'reachable_fueling_points_count': len(reachable_points),
        'best_savings_fueling_point': best_savings_stop,
        'savings_verification_rows': savings_verification_rows,
    }


def _route_bounding_box(route_coords, margin_km=15):
    """Build a route corridor bounding box with a small margin in kilometers."""
    if not route_coords:
        return None

    latitudes = [coord[0] for coord in route_coords]
    longitudes = [coord[1] for coord in route_coords]
    center_lat = sum(latitudes) / len(latitudes)

    lat_margin = margin_km / 111.0
    lon_margin = margin_km / max(111.0 * max(abs(__import__('math').cos(__import__('math').radians(center_lat))), 0.01), 0.01)

    return {
        'min_lat': min(latitudes) - lat_margin,
        'max_lat': max(latitudes) + lat_margin,
        'min_lon': min(longitudes) - lon_margin,
        'max_lon': max(longitudes) + lon_margin,
    }


def get_all_addresses_from_database(bounding_box=None):
    """Get addresses from LovesLocations with coordinates, optionally constrained to a bounding box."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return []
        
        cursor = conn.cursor()
        query = [
            "SELECT StoreNumber, City, Address, Latitude, Longitude",
            "FROM dbo.LovesLocations",
            "WHERE Address IS NOT NULL AND Address != ''",
        ]
        params = []

        if bounding_box:
            query.append("AND Latitude BETWEEN ? AND ?")
            query.append("AND Longitude BETWEEN ? AND ?")
            params.extend([
                bounding_box['min_lat'],
                bounding_box['max_lat'],
                bounding_box['min_lon'],
                bounding_box['max_lon'],
            ])

        query.append("ORDER BY City, Address")
        cursor.execute("\n            ".join(query), params)
        addresses = []
        for row in cursor.fetchall():
            addresses.append({
                'store_number': row[0],
                'city': row[1],
                'address': row[2],
                'latitude': float(row[3]),
                'longitude': float(row[4]),
            })
        return addresses
    except Exception as e:
        print(f'Error loading addresses: {e}')
        return []
    finally:
        if conn:
            conn.close()


def calculate_distance_with_api(lat1, lon1, lat2, lon2):
    """
    Calculate distance using OpenRouteService API
    Returns: dict with distance in km and meters, plus addresses along the route
    """
    try:
        url = "https://api.openrouteservice.org/v2/directions/driving-car"
        
        headers = {
            "Authorization": API_KEY,
            "Content-Type": "application/json"
        }
        
        payload = {
            "coordinates": [
                [lon1, lat1],  # OpenRouteService expects [lon, lat]
                [lon2, lat2]
            ],
            "preference": "fastest",
            "alternative_routes": {
                "target_count": 3,
                "share_factor": 0.6,
                "weight_factor": 1.4,
            },
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=10)

        if response.status_code != 200:
            fallback_payload = {
                "coordinates": [
                    [lon1, lat1],
                    [lon2, lat2]
                ],
                "preference": "fastest",
            }
            fallback_response = requests.post(url, json=fallback_payload, headers=headers, timeout=10)
            if fallback_response.status_code != 200:
                return {
                    'success': False,
                    'error': f'API Error: {response.status_code} - {response.text}',
                }
            response = fallback_response

        data = response.json()
        routes = data.get('routes') or []
        if not routes:
            return {'success': False, 'error': 'OpenRouteService returned no route options'}

        best_route_index = 0
        best_route = routes[0]
        best_duration = best_route.get('summary', {}).get('duration', float('inf'))
        for index, route in enumerate(routes[1:], start=1):
            duration = route.get('summary', {}).get('duration', float('inf'))
            if duration < best_duration:
                best_route_index = index
                best_route = route
                best_duration = duration

        distance_meters = best_route['summary']['distance']
        distance_km = distance_meters / 1000
        distance_miles = distance_km * 0.621371

        route_geometry = best_route.get('geometry')
        route_coords = _extract_route_coordinates(route_geometry)
        addresses_along_route = []
        route_bounds = _route_bounding_box(route_coords, margin_km=15)
        all_addresses = get_all_addresses_from_database(route_bounds)

        if route_coords:
            threshold_km = 10

            for addr in all_addresses:
                proximity = _find_nearest_point_on_route(
                    addr['latitude'],
                    addr['longitude'],
                    route_coords
                )

                if proximity and proximity['distance_to_route_km'] <= threshold_km:
                    distance_from_start = _haversine_distance(
                        lat1, lon1,
                        addr['latitude'], addr['longitude']
                    )

                    addresses_along_route.append({
                        'store_number': addr.get('store_number'),
                        'city': addr['city'],
                        'address': addr['address'],
                        'latitude': addr['latitude'],
                        'longitude': addr['longitude'],
                        'distance_from_start_km': round(distance_from_start, 2),
                        'distance_from_route_km': proximity['distance_to_route_km'],
                        'distance_along_route_km': proximity['distance_along_route_km'],
                    })

        addresses_along_route = _append_exact_endpoint_matches(
            addresses_along_route,
            all_addresses,
            lat1,
            lon1,
            lat2,
            lon2,
        )

        addresses_along_route.sort(key=_route_position_km)



        return {
            'success': True,
            'distance_meters': round(distance_meters, 2),
            'distance_km': round(distance_km, 2),
            'distance_miles': round(distance_miles, 2),
            'duration_seconds': best_route['summary'].get('duration', 0),
            'route_coordinates': [
                {'lat': round(coord[0], 6), 'lon': round(coord[1], 6)} for coord in route_coords
            ],
            'route_count': len(routes),
            'selected_route_index': best_route_index,
            'route_preference': 'fastest',
            'route_label': best_route.get('summary', {}).get('summary') or 'fastest available route',
            'addresses_along_route': addresses_along_route,
        }
    
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'API request timed out'}
    except requests.exceptions.RequestException as e:
        return {'success': False, 'error': f'Request error: {str(e)}'}


def _format_duration(duration_seconds):
    """Convert seconds into a compact readable duration label."""
    total_seconds = max(int(round(float(duration_seconds or 0))), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _build_route_map_file_uri(result):
    """Generate a local HTML map file and return its file:// URI for reliable new-tab opening."""
    route_coords = result.get('distance', {}).get('route_coordinates', [])
    addresses = result.get('addresses_along_route', [])
    if not route_coords:
        return None

    route_json = json.dumps(route_coords)
    addresses_json = json.dumps(addresses)

    distance_km = result.get('distance', {}).get('distance_km', 0)
    duration_label = _format_duration(result.get('distance', {}).get('duration_seconds', 0))
    fuel_management = result.get('fuel_management', {})
    fuel_available_label = f"{fuel_management.get('current_fuel_l', 0):.2f} L"
    fuel_safe_range_label = f"{fuel_management.get('safe_operating_range_km', 0):.2f} km"
    fuel_status_label = fuel_management.get('fuel_status_detail') or 'Fuel Status: Unknown'
    fuel_alert_label = fuel_management.get('emergency_fuel_alert_label') or ''

    start_label = result.get('start_location', result.get('starting_point', 'Starting Point'))
    end_label = result.get('destination_location', result.get('destination_point', 'Destination'))
    truck_label = result.get('truck', {}).get('truck_name', '')

    start_label_html = escape(str(start_label))
    end_label_html = escape(str(end_label))
    truck_label_html = escape(str(truck_label))

    start_label_js = json.dumps(str(start_label))
    end_label_js = json.dumps(str(end_label))

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Route Map</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" crossorigin=\"\" />
    <style>
        :root {{
            --bg: #f5f7fa;
            --panel: #ffffff;
            --ink: #0f172a;
            --muted: #475569;
            --accent: #0a7ea4;
            --accent-2: #1f9d55;
        }}
        html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--ink); font-family: Segoe UI, Tahoma, sans-serif; }}
        .top {{
            display: grid;
            grid-template-columns: 2fr 1fr 1fr;
            gap: 10px;
            padding: 12px;
            background: linear-gradient(120deg, #eaf5fb, #f8fff9);
            border-bottom: 1px solid #dbe5ee;
        }}
        .card {{ background: var(--panel); border: 1px solid #e3e8ef; border-radius: 10px; padding: 10px 12px; }}
        .label {{ font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
        .value {{ font-size: 16px; font-weight: 600; }}
        #map {{ width: 100vw; height: calc(100vh - 104px); }}
        .hint {{ font-size: 12px; color: var(--muted); margin-top: 6px; }}
    </style>
</head>
<body>
    <section class=\"top\">
        <div class=\"card\">
            <div class=\"label\">Trip</div>
            <div class=\"value\">{start_label_html} → {end_label_html}</div>
            <div class=\"hint\">Truck: {truck_label_html}</div>
        </div>
        <div class=\"card\"><div class=\"label\">Total Distance</div><div class=\"value\">{distance_km} km</div></div>
        <div class=\"card\"><div class=\"label\">Expected Time</div><div class=\"value\">{duration_label}</div></div>
        <div class=\"card\"><div class=\"label\">Fuel / Safe Range</div><div class=\"value\">{fuel_available_label}</div><div class=\"hint\">Reserve-safe range: {fuel_safe_range_label}</div></div>
        <div class=\"card\"><div class=\"label\">Fuel Status</div><div class=\"value\">{fuel_status_label}</div><div class=\"hint\">{fuel_alert_label}</div></div>
    </section>
    <div id=\"map\"></div>

    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\" crossorigin=\"\"></script>
    <script>
        const routeCoords = {route_json};
        const addresses = {addresses_json};

        const routeLatLng = routeCoords.map(p => [p.lat, p.lon]);
        const start = routeLatLng[0];
        const end = routeLatLng[routeLatLng.length - 1];

        const map = L.map('map', {{ zoomControl: true }});
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{ maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }}).addTo(map);

        const routeLine = L.polyline(routeLatLng, {{ color: '#0a7ea4', weight: 6, opacity: 0.9 }}).addTo(map);

        const startIcon = L.divIcon({{ html: '<div style="background:#1f9d55;color:white;border-radius:20px;padding:4px 8px;font-size:11px;font-weight:600;">START</div>', className: '' }});
        const endIcon = L.divIcon({{ html: '<div style="background:#d9480f;color:white;border-radius:20px;padding:4px 8px;font-size:11px;font-weight:600;">END</div>', className: '' }});
        const makeFuelStopIcon = (label) => L.divIcon({{ html: `<div style="background:#b91c1c;color:white;border-radius:20px;padding:6px 10px;font-size:11px;font-weight:700;box-shadow:0 2px 8px rgba(185,28,28,0.35);">${{label}}</div>`, className: '' }});

        L.marker(start, {{ icon: startIcon }}).addTo(map).bindPopup('<b>Starting Point</b><br>' + {start_label_js});
        L.marker(end, {{ icon: endIcon }}).addTo(map).bindPopup('<b>Destination</b><br>' + {end_label_js});

        addresses.forEach((a, idx) => {{
            const title = `${{idx + 1}}. ${{a.address || ''}}, ${{a.city || ''}}`;
            const stopLabel = a.fuel_stop_marker_label || 'RECOMMENDED FUEL STOP';
            const popup = [
                `<b>${{title}}</b>`,
                `Distance from start: ${{a.distance_from_start_km}} km`,
                `Distance from route: ${{a.distance_from_route_km}} km`,
                `Distance along route: ${{a.distance_along_route_km}} km`,
                a.savings_display ? `Savings: ${{a.savings_display}}` : '',
            ].join('<br>');

            if (a.recommended_fueling_point) {{
                const fuelStopIcon = makeFuelStopIcon(stopLabel);
                const stopPopup = popup + [
                    `<br><b>Status:</b> ${{stopLabel}}`,
                    `<b>Distance from previous stop:</b> ${{a.distance_from_previous_stop_km || 0}} km`,
                    `<b>Fuel remaining at stop:</b> ${{a.fuel_remaining_at_stop_l || 0}} L`,
                    `<b>Refuel amount:</b> ${{a.refuel_amount_l || 0}} L`,
                    `<b>Remaining distance after stop:</b> ${{a.remaining_distance_after_stop_km || 0}} km`,
                    `<b>Savings:</b> ${{a.savings_display || a.savings || 'N/A'}}`,
                    `<b>Reason:</b> ${{a.fuel_stop_reason || 'planned'}}`,
                    `<b>Warning:</b> ${{a.warning || 'REFUEL REQUIRED'}}`,
                ].join('<br>');
                const recommendedMarker = L.marker([a.latitude, a.longitude], {{ icon: fuelStopIcon }}).addTo(map);
                recommendedMarker.bindPopup(stopPopup);
                recommendedMarker.bindTooltip(stopLabel, {{ direction: 'top' }});
                return;
            }}

            const m = L.circleMarker([a.latitude, a.longitude], {{
                radius: 6,
                color: '#f59e0b',
                fillColor: '#f59e0b',
                fillOpacity: 0.8,
                weight: 2,
            }}).addTo(map);

            m.bindPopup(popup);
            m.bindTooltip(title, {{ direction: 'top' }});
        }});

        map.fitBounds(routeLine.getBounds(), {{ padding: [20, 20] }});
    </script>
</body>
</html>"""

    maps_dir = Path(__file__).parent / "generated_maps"
    maps_dir.mkdir(parents=True, exist_ok=True)

    record_ref = result.get('trip_record_label') or result.get('trip_record_id') or 'trip'
    safe_ref = ''.join(ch if str(ch).isalnum() else '_' for ch in str(record_ref))
    file_path = maps_dir / f"route_{safe_ref}_{uuid4().hex[:8]}.html"
    file_path.write_text(html, encoding='utf-8')

    return file_path.resolve().as_uri()


def _get_trip_records_table(cursor):
    cursor.execute(
        """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'Trip_Records'
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY CASE WHEN TABLE_SCHEMA = 'dbo' THEN 0 ELSE 1 END, TABLE_SCHEMA
        """
    )
    row = cursor.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def _get_trip_record_id_column(cursor, table_schema, table_name):
    """Find the best identifier column for Trip_Records, if one exists."""
    cursor.execute(
        """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ?
              AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """,
        (table_schema, table_name),
    )
    columns = [str(row[0]) for row in cursor.fetchall()]
    normalized = {column.lower(): column for column in columns}

    for candidate in ('id', 'record_id', 'trip_id', 'trip_record_id'):
        if candidate in normalized:
            return normalized[candidate]

    for column in columns:
        lower_column = column.lower()
        if 'id' in lower_column and 'truck' not in lower_column:
            return column

    return None


def _duration_seconds_to_time_value(duration_seconds):
    total_seconds = max(int(round(float(duration_seconds or 0))), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    hours = hours % 24
    return time(hour=hours, minute=minutes, second=seconds)


def store_trip_record(
    starting_point,
    destination_point,
    total_distance_km,
    truck_name,
    truck_average_l,
    required_time_seconds,
):
    """Store a completed distance calculation in Trip_Records."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {'success': False, 'error': 'Database connection failed'}

        cursor = conn.cursor()
        table_schema, table_name = _get_trip_records_table(cursor)
        if not table_name:
            return {'success': False, 'error': 'Trip_Records table not found'}

        id_column = _get_trip_record_id_column(cursor, table_schema, table_name)
        required_time_value = _duration_seconds_to_time_value(required_time_seconds)
        record_date = date.today()

        output_clause = f"OUTPUT INSERTED.[{id_column}]" if id_column else ""
        insert_query = f"""
            INSERT INTO [{table_schema}].[{table_name}]
                ([Starting_point], [Destination_point], [Total_Distance], [Truck_Name], [Truck_Average_L], [Required_Time], [Date])
            {output_clause}
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
        """
        cursor.execute(
            insert_query,
            (
                starting_point,
                destination_point,
                total_distance_km,
                truck_name,
                truck_average_l,
                required_time_value,
                record_date,
            ),
        )
        row = cursor.fetchone() if id_column else None
        conn.commit()

        record_id = row[0] if row and row[0] is not None else None
        if record_id is not None:
            record_id_text = str(record_id).strip()
            record_label = record_id_text if record_id_text.upper().startswith('T') else f'T{record_id_text}'
        else:
            record_label = f"{truck_name} trip on {record_date.strftime('%m/%d/%Y')}"
        return {
            'success': True,
            'record_id': record_id,
            'record_label': record_label,
            'record_date': record_date.strftime('%m/%d/%Y'),
        }
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            conn.close()


def calculate_and_store_trip_record(
    starting_point,
    destination_point,
    truck_name,
    current_fuel_l=None,
    starting_city=None,
    starting_address=None,
    destination_city=None,
    destination_address=None,
):
    """Calculate the trip metrics and persist the result."""
    truck_result = get_truck_fuel_average(truck_name)
    if not truck_result.get('success'):
        return truck_result

    start_coords = resolve_location_to_coordinates(
        starting_point,
        city=starting_city,
        address=starting_address,
    )
    if not start_coords.get('success'):
        return {'success': False, 'error': start_coords.get('error', 'Starting point could not be resolved')}

    dest_coords = resolve_location_to_coordinates(
        destination_point,
        city=destination_city,
        address=destination_address,
    )
    if not dest_coords.get('success'):
        return {'success': False, 'error': dest_coords.get('error', 'Destination could not be resolved')}

    distance_result = calculate_distance_with_api(
        start_coords['latitude'],
        start_coords['longitude'],
        dest_coords['latitude'],
        dest_coords['longitude'],
    )
    if not distance_result.get('success'):
        return {'success': False, 'error': distance_result.get('error', 'Failed to calculate distance')}

    fuel_management = _build_fuel_management_summary(
        distance_result.get('addresses_along_route', []),
        distance_result.get('distance_km', 0),
        current_fuel_l,
        truck_result['fuel_average_l'],
    )

    fuel_required = round(_fuel_required_l_for_distance(distance_result['distance_km'], truck_result['fuel_average_l']), 2)
    duration_hours = round(distance_result['duration_seconds'] / 3600, 2)

    save_result = store_trip_record(
        starting_point=starting_point,
        destination_point=destination_point,
        total_distance_km=distance_result['distance_km'],
        truck_name=truck_result['truck_name'],
        truck_average_l=truck_result['fuel_average_l'],
        required_time_seconds=distance_result['duration_seconds'],
    )
    if not save_result.get('success'):
        return save_result

    return {
        'success': True,
        'starting_point': starting_point,
        'destination_point': destination_point,
        'truck': truck_result,
        'start_coordinates': start_coords,
        'destination_coordinates': dest_coords,
        'distance': distance_result,
        'fuel_required_l': fuel_required,
        'estimated_duration_hours': duration_hours,
        'current_fuel_l': fuel_management['current_fuel_l'],
        'fuel_management': fuel_management,
        'trip_record_id': save_result.get('record_id'),
        'trip_record_label': save_result.get('record_label'),
        'calculation_date': save_result.get('record_date'),
        'start_location': start_coords.get('label', starting_point),
        'destination_location': dest_coords.get('label', destination_point),
        'addresses_along_route': distance_result.get('addresses_along_route', []),
        'recommended_fueling_point': fuel_management.get('recommended_fueling_point'),
        'recommended_fueling_points': fuel_management.get('recommended_fueling_points', []),
        'fuel_status': fuel_management.get('fuel_status'),
        'fuel_status_message': fuel_management.get('fuel_status_message'),
        'fuel_status_detail': fuel_management.get('fuel_status_detail'),
        'emergency_fuel_alert': fuel_management.get('emergency_fuel_alert'),
        'emergency_fuel_alert_label': fuel_management.get('emergency_fuel_alert_label'),
        'mandatory_refuel_instruction': fuel_management.get('mandatory_refuel_instruction'),
    }


@st.cache_data
def load_locations_list():
    """Load all available locations from database."""
    try:
        conn = get_db_connection()
        if not conn:
            return []
        
        cursor = conn.cursor()
        query = """
            SELECT DISTINCT City, Address
            FROM dbo.LovesLocations
            ORDER BY City, Address
        """
        cursor.execute(query)
        
        locations = []
        for row in cursor.fetchall():
            locations.append(f"{row[0]}, {row[1]}" if row[1] else row[0])
        
        conn.close()
        return sorted(locations)
    except Exception as e:
        print(f"Error loading locations: {e}")
        return []


def main():
    """Render the Streamlit UI for distance calculator."""
    st.set_page_config(page_title="Distance Calculator", page_icon="🚚", layout="wide")
    st.title("🚚 Distance & Fuel Calculator")
    st.write("Calculate the distance between any two locations and the fuel required for a specific truck.")
    
    # Load truck list
    truck_result = get_available_trucks()
    available_trucks = truck_result.get('trucks', []) if truck_result.get('success') else []
    
    if not available_trucks:
        st.warning("⚠️ No trucks available in database. Please add trucks first.")
        return
    
    st.caption("Enter any place name, address, city, or landmark. The app resolves locations through OpenRouteService.")

    # Create two columns for inputs
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Starting Point")
        starting_point = st.text_input(
            "Enter starting location",
            key="starting_point",
            placeholder="e.g. Calgary, AB or a street address"
        )
    
    with col2:
        st.subheader("Destination")
        destination_point = st.text_input(
            "Enter destination location",
            key="destination_point",
            placeholder="e.g. Edmonton, AB or a street address"
        )
    
    # Truck selection
    st.subheader("Truck Selection")
    col1, col2 = st.columns(2)
    
    with col1:
        truck_name = st.selectbox(
            "Select truck",
            options=available_trucks,
            key="truck_name",
            index=None,
            placeholder="Choose a truck..."
        )

    st.subheader("Fuel Availability")
    current_fuel_l = st.number_input(
        "Current Fuel in Truck (Litres)",
        min_value=0.0,
        max_value=200.0,
        value=None,
        step=1.0,
        format="%.2f",
        placeholder="Example: 120",
        help="Used to calculate usable range and the emergency reserve buffer.",
    )
    
    # Calculate button
    col_btn1, col_btn2 = st.columns([1, 4])
    with col_btn1:
        calculate_btn = st.button("📍 Calculate Distance", type="primary")
    
    # Process calculation
    if calculate_btn:
        if not starting_point:
            st.error("❌ Please select a starting point")
            return
        
        if not destination_point:
            st.error("❌ Please select a destination")
            return
        
        if not truck_name:
            st.error("❌ Please select a truck")
            return
        
        if current_fuel_l is None:
            st.error("Please enter current fuel availability")
            return

        with st.spinner("Calculating distance..."):
            result = calculate_and_store_trip_record(
                starting_point=starting_point,
                destination_point=destination_point,
                truck_name=truck_name,
                current_fuel_l=current_fuel_l,
            )

            if not result.get('success'):
                st.error(f"❌ {result.get('error', 'Failed to calculate distance')}")
                return
            
            # Display results
            st.success(f"✅ Calculation completed and stored as {result.get('trip_record_label') or 'the new record'}!")
            
            # Create result columns
            res_col1, res_col2, res_col3 = st.columns(3)
            
            with res_col1:
                st.metric(
                    "Distance (KM)",
                    f"{result['distance']['distance_km']} km",
                    delta=f"{result['distance']['distance_miles']:.1f} mi"
                )
            
            with res_col2:
                st.metric(
                    "Fuel Required",
                    f"{result['fuel_required_l']} L",
                    delta=f"Avg: {result['truck']['fuel_average_l']} L/100km"
                )
            
            with res_col3:
                st.metric(
                    "Estimated Duration",
                    f"{result['estimated_duration_hours']:.1f} hours",
                    delta=f"{result['distance']['duration_seconds']} seconds"
                )

            fuel_management = result.get('fuel_management', {})
            if fuel_management:
                st.divider()
                st.subheader("⛽ Fuel Management")
                fuel_col1, fuel_col2, fuel_col3 = st.columns(3)
                with fuel_col1:
                    st.metric("Current Fuel", f"{fuel_management.get('current_fuel_l', 0):.2f} L")
                with fuel_col2:
                    st.metric("Usable Travel Range", f"{fuel_management.get('available_travel_range_km', 0):.2f} km")
                with fuel_col3:
                    st.metric("Safe Operating Range", f"{fuel_management.get('safe_operating_range_km', 0):.2f} km", delta="20 km reserve")

                fuel_status = fuel_management.get('fuel_status', 'Unknown')
                fuel_status_message = fuel_management.get('fuel_status_message')
                if fuel_status == 'Sufficient':
                    st.success("Fuel Status: Sufficient")
                    if fuel_status_message:
                        st.write(fuel_status_message)
                elif fuel_status == 'Insufficient':
                    st.error("Fuel Status: Insufficient")
                    st.error("⚠️ EMERGENCY FUEL ALERT")
                    if fuel_status_message:
                        st.write(fuel_status_message)
                    if fuel_management.get('mandatory_refuel_instruction'):
                        st.write(fuel_management['mandatory_refuel_instruction'])

                recommended_stops = fuel_management.get('recommended_fueling_points', [])
                if recommended_stops:
                    st.metric("Total Recommended Stops", fuel_management.get('total_recommended_fuel_stops', len(recommended_stops)))
                    for recommended_stop in recommended_stops:
                        stop_label = recommended_stop.get('fuel_stop_marker_label', f"FUEL STOP #{recommended_stop.get('stop_sequence', '')}")
                        with st.expander(f"{stop_label}: {recommended_stop.get('address')}, {recommended_stop.get('city')}", expanded=True):
                            stop_col1, stop_col2 = st.columns(2)
                            with stop_col1:
                                st.write(f"**Address:** {recommended_stop.get('address')}, {recommended_stop.get('city')}")
                                st.write(f"**Store Number:** {recommended_stop.get('store_number', 'N/A')}")
                                st.write(f"**Distance from start:** {recommended_stop.get('distance_along_route_km', 0):.2f} km")
                                st.write(f"**Distance from previous fuel stop:** {recommended_stop.get('distance_from_previous_stop_km', 0):.2f} km")
                                st.write(f"**Remaining distance after this stop:** {recommended_stop.get('remaining_distance_after_stop_km', 0):.2f} km")
                            with stop_col2:
                                st.write(f"**Fuel used since last stop:** {recommended_stop.get('fuel_used_since_previous_stop_l', 0):.2f} L")
                                st.write(f"**Fuel remaining at stop:** {recommended_stop.get('fuel_remaining_at_stop_l', 0):.2f} L")
                                st.write(f"**Refuel amount:** {recommended_stop.get('refuel_amount_l', 0):.2f} L")
                                st.write(f"**Savings:** {recommended_stop.get('savings_display', recommended_stop.get('savings', 'N/A'))}")
                                st.write(f"**Selected after half-tank usage:** {recommended_stop.get('selected_after_half_tank_usage')}")
                            st.write(f"**Reason:** {recommended_stop.get('fuel_stop_reason', 'planned')}")
                            if recommended_stop.get('warning'):
                                st.warning(recommended_stop['warning'])
                elif fuel_management.get('safety_failure'):
                    failure = fuel_management['safety_failure']
                    st.error("No safe refuelling station was found before the truck exceeds its safe operating fuel range.")
                    st.write(f"**Current position:** {failure.get('current_position_km', 0):.2f} km")
                    st.write(f"**Maximum reachable:** {failure.get('maximum_reachable_km', 0):.2f} km")
                    st.write(f"**Remaining destination distance:** {failure.get('remaining_destination_distance_km', 0):.2f} km")
                elif fuel_management.get('recommended_stop_required'):
                    st.warning("No suitable fueling point was found before the safe operating threshold on the current route.")
                else:
                    st.info("No fueling stop is required before the destination while preserving the 20 km emergency reserve.")

                verification_rows = fuel_management.get('savings_verification_rows', [])
                if verification_rows:
                    with st.expander("Fuel Savings Recommendation Verification", expanded=True):
                        st.caption("Candidate stations are ranked by savings, route progress, and route proximity while respecting tank range and half-tank rules.")
                        st.dataframe(
                            verification_rows,
                            hide_index=True,
                            use_container_width=True,
                        )
            
            # Detailed information
            st.divider()
            st.subheader("📋 Trip Details")
            
            detail_col1, detail_col2 = st.columns(2)
            
            with detail_col1:
                st.write("**Starting Point**")
                st.write(f"Location: {result.get('start_location', result['starting_point'])}")
                st.write(f"Coordinates: ({result['start_coordinates']['latitude']}, {result['start_coordinates']['longitude']})")
                if result['start_coordinates'].get('city'):
                    st.write(f"City: {result['start_coordinates']['city']}")
                if result['start_coordinates'].get('address'):
                    st.write(f"Address: {result['start_coordinates']['address']}")
            
            with detail_col2:
                st.write("**Destination**")
                st.write(f"Location: {result.get('destination_location', result['destination_point'])}")
                st.write(f"Coordinates: ({result['destination_coordinates']['latitude']}, {result['destination_coordinates']['longitude']})")
                if result['destination_coordinates'].get('city'):
                    st.write(f"City: {result['destination_coordinates']['city']}")
                if result['destination_coordinates'].get('address'):
                    st.write(f"Address: {result['destination_coordinates']['address']}")
            
            st.divider()
            st.subheader("🚚 Vehicle Information")
            
            truck_col1, truck_col2 = st.columns(2)
            
            with truck_col1:
                st.write(f"**Truck Name:** {result['truck']['truck_name']}")
            
            with truck_col2:
                st.write(f"**Fuel Average:** {result['truck']['fuel_average_l']} L/100km")

            st.divider()
            st.subheader("📍 Addresses Along Route")
            
            addresses_along_route = result.get('addresses_along_route', [])
            if addresses_along_route:
                st.info(f"✓ Found {len(addresses_along_route)} address(es) within 10 km of the route")
                
                for idx, addr in enumerate(addresses_along_route, 1):
                    label_prefix = (
                        f"⛽ {addr.get('fuel_stop_marker_label', 'FUEL STOP')} - "
                        if addr.get('recommended_fueling_point')
                        else "📌 "
                    )
                    with st.expander(f"{label_prefix}{addr['address']}, {addr['city']} (Distance from start: {addr['distance_from_start_km']} km)"):
                        col_a1, col_a2 = st.columns(2)
                        with col_a1:
                            st.write(f"**City:** {addr['city']}")
                            st.write(f"**Address:** {addr['address']}")
                            st.write(f"**Coordinates:** ({addr['latitude']}, {addr['longitude']})")
                        with col_a2:
                            st.write(f"**Distance from Starting Point:** {addr['distance_from_start_km']} km")
                            st.write(f"**Distance from Route:** {addr['distance_from_route_km']} km")
                            st.write(f"**Distance along Route:** {addr['distance_along_route_km']} km")
                            if addr.get('savings') is not None:
                                st.write(f"**Savings:** {addr.get('savings_display', addr.get('savings'))}")
                            if addr.get('recommended_fueling_point'):
                                st.write(f"**Status:** {addr.get('fuel_stop_marker_label', 'FUEL STOP')}")
                                st.write(f"**Distance from previous stop:** {addr.get('distance_from_previous_stop_km', 0):.2f} km")
                                st.write(f"**Fuel used since previous stop:** {addr.get('fuel_used_since_previous_stop_l', 0):.2f} L")
                                st.write(f"**Fuel Remaining at Stop:** {addr.get('fuel_remaining_at_stop_l', 0):.2f} L")
                                st.write(f"**Refuel amount:** {addr.get('refuel_amount_l', 0):.2f} L")
                                st.write(f"**Remaining distance after stop:** {addr.get('remaining_distance_after_stop_km', 0):.2f} km")
            else:
                st.info("ℹ️ No addresses found within 10 km of the selected route.")

            st.divider()
            st.subheader("�💾 Stored Record")
            st.write(f"**Record ID:** {result.get('trip_record_label') or 'N/A'}")
            st.write(f"**Calculation Date:** {result.get('calculation_date', '')}")

            st.divider()
            st.subheader("🗺️ Open Route Map")
            map_file_uri = _build_route_map_file_uri(result)

            if map_file_uri:
                st.link_button("Open route map", map_file_uri)
            else:
                st.info("Route geometry is unavailable for this trip, so the map tab could not be generated.")


if __name__ == "__main__":
    main()

