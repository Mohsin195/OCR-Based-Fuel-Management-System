import os
from pathlib import Path

import pyodbc
import streamlit as st
from dotenv import load_dotenv


env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

API_KEY = os.getenv("API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

st.set_page_config(page_title="Truck Registry", layout="wide")

if "message" not in st.session_state:
    st.session_state.message = ""

print(f"[app_1.py] API_KEY loaded: {API_KEY is not None}")
print(f"[app_1.py] DATABASE_URL loaded: {DATABASE_URL is not None}")


def _parse_database_name(url):
    if not url or "/" not in url:
        return "Fuel_Station"
    tail = url.rsplit("/", 1)[1]
    return tail.split("?", 1)[0] or "Fuel_Station"


def get_db_connection():
    """Open a local SQL Server connection using the configured database name."""
    database_name = _parse_database_name(DATABASE_URL or os.getenv("DATABASE_URL"))
    candidates = [
        "DESKTOP-7K48LCE\\SQLEXPRESS",
        "localhost\\SQLEXPRESS",
    ]
    engine = None
    last_error = None
    for server_name in candidates:
        conn_str = (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            f"SERVER={server_name};"
            f"DATABASE={database_name};"
            "Trusted_Connection=yes;"
            "Encrypt=no;"
            "TrustServerCertificate=yes;"
        )
        try:
            return pyodbc.connect(conn_str, timeout=5)
        except Exception as error:
            last_error = error

    print(f"Database connection error: {last_error}")
    return None



def _normalize_name(value):
    return " ".join(str(value).strip().split())


def _get_truck_details_columns(cursor):
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
        return None, None, None, None, None, None

    table_columns = {}
    for row in rows:
        key = (row[0], row[1])
        table_columns.setdefault(key, []).append((row[2], (row[3] or "").lower()))

    def pick_columns(columns_with_types):
        columns = [column_name for column_name, _ in columns_with_types]
        normalized = {column.lower(): column for column in columns}

        id_col = normalized.get("id")

        truck_name_candidates = [
            "truck_name",
            "truckname",
            "name",
            "truck",
            "truck_no",
            "truck_number",
            "vehicle_name",
            "vehicle",
        ]
        fuel_avg_candidates = [
            "fuel_average_l",
            "fuelaverage_l",
            "fuel_avg_l",
            "average_fuel_l",
            "avg_fuel_l",
            "fuel_average",
            "average_fuel",
            "fuel_avg",
        ]
        fuel_capacity_candidates = [
            "truck_fuel_capacity",
            "truckfuelcapacity",
            "fuel_capacity",
            "fuel_capacity_l",
            "capacity_fuel",
            "tank_capacity",
            "tank_capacity_l",
            "fuel_tank_capacity",
            "fuel_tank_capacity_l",
        ]

        truck_name_col = next((normalized[candidate] for candidate in truck_name_candidates if candidate in normalized), None)
        fuel_avg_col = next((normalized[candidate] for candidate in fuel_avg_candidates if candidate in normalized), None)
        fuel_capacity_col = next((normalized[candidate] for candidate in fuel_capacity_candidates if candidate in normalized), None)

        if not truck_name_col:
            truck_name_col = next(
                (column for column in columns if "truck" in column.lower() and "id" not in column.lower()),
                None,
            )

        if not truck_name_col:
            truck_name_col = next(
                (column for column in columns if "vehicle" in column.lower() and "id" not in column.lower()),
                None,
            )

        if not truck_name_col:
            truck_name_col = next(
                (
                    column
                    for column in columns
                    if "fuel" not in column.lower()
                    and "avg" not in column.lower()
                    and "average" not in column.lower()
                    and "liter" not in column.lower()
                    and column.lower() not in {"id"}
                ),
                None,
            )

        if not truck_name_col and columns:
            truck_name_col = columns[0]

        if not fuel_avg_col:
            fuel_avg_col = next(
                (
                    column
                    for column in columns
                    if "fuel" in column.lower() and ("avg" in column.lower() or "average" in column.lower())
                ),
                None,
            )

        if not fuel_avg_col:
            fuel_avg_col = next((column for column in columns if "fuel" in column.lower()), None)

        if not fuel_avg_col:
            numeric_types = {"decimal", "numeric", "float", "real", "int", "bigint", "smallint"}
            fuel_avg_col = next(
                (column for column, data_type in columns_with_types if data_type in numeric_types),
                None,
            )

        if not fuel_capacity_col:
            fuel_capacity_col = next(
                (
                    column
                    for column in columns
                    if "fuel" in column.lower()
                    and ("capacity" in column.lower() or "tank" in column.lower())
                    and column != fuel_avg_col
                ),
                None,
            )

        if not fuel_capacity_col:
            fuel_capacity_col = next(
                (
                    column
                    for column in columns
                    if ("capacity" in column.lower() or "tank" in column.lower())
                    and column != fuel_avg_col
                ),
                None,
            )

        return id_col, truck_name_col, fuel_avg_col, fuel_capacity_col

    best = None
    best_score = -1
    for (table_schema, table_name), columns_with_types in table_columns.items():
        id_col, truck_name_col, fuel_avg_col, fuel_capacity_col = pick_columns(columns_with_types)
        score = 0
        if id_col:
            score += 2
        if truck_name_col:
            score += 2
        if fuel_avg_col:
            score += 2
        if fuel_capacity_col:
            score += 2
        if table_schema == "dbo":
            score += 1
        if table_name == "Truck_Detail":
            score += 1

        if score > best_score:
            best_score = score
            best = (table_schema, table_name, id_col, truck_name_col, fuel_avg_col, fuel_capacity_col)

    return best if best else (None, None, None, None, None, None)


def _resolve_truck_table(cursor):
    table_schema, table_name, id_col, truck_name_col, fuel_avg_col, fuel_capacity_col = _get_truck_details_columns(cursor)
    if not table_schema or not table_name or not truck_name_col:
        return None
    return {
        "schema": table_schema,
        "table": table_name,
        "id_col": id_col,
        "truck_name_col": truck_name_col,
        "fuel_avg_col": fuel_avg_col,
        "fuel_capacity_col": fuel_capacity_col,
        "table_ref": f"[{table_schema}].[{table_name}]",
    }


def _truck_exists(cursor, table_ref, truck_name_col, truck_name):
    query = f"""
        SELECT TOP 1 [{truck_name_col}]
        FROM {table_ref}
        WHERE LOWER(LTRIM(RTRIM(CAST([{truck_name_col}] AS NVARCHAR(255))))) = LOWER(?)
    """
    cursor.execute(query, (truck_name,))
    return cursor.fetchone() is not None


def _next_truck_id(cursor, table_ref, id_col):
    query = f"SELECT [{id_col}] FROM {table_ref} WHERE [{id_col}] IS NOT NULL"
    cursor.execute(query)
    rows = cursor.fetchall()

    highest_prefix = "T"
    highest_number = 0

    for row in rows:
        value = str(row[0]).strip()
        if not value:
            continue
        index = len(value)
        while index > 0 and value[index - 1].isdigit():
            index -= 1
        prefix = value[:index] or "T"
        digits = value[index:]
        if not digits:
            continue
        try:
            number = int(digits)
        except ValueError:
            continue
        if number > highest_number:
            highest_prefix = prefix
            highest_number = number

    return f"{highest_prefix}{highest_number + 1:03d}"




def add_truck_to_db(truck_name, fuel_average_l, truck_fuel_capacity):
    """Add a truck to the database."""
    truck_name = _normalize_name(truck_name)

    if not truck_name:
        return {"success": False, "error": "truck_name is required"}

    if fuel_average_l is None:
        return {"success": False, "error": "fuel_average_l is required"}

    if fuel_average_l <= 0:
        return {"success": False, "error": "fuel_average_l must be greater than 0"}

    if truck_fuel_capacity is None:
        return {"success": False, "error": "truck_fuel_capacity is required"}

    if truck_fuel_capacity <= 0:
        return {"success": False, "error": "truck_fuel_capacity must be greater than 0"}

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database connection failed"}

        cursor = conn.cursor()
        table_info = _resolve_truck_table(cursor)
        if not table_info:
            return {"success": False, "error": "Truck_Detail/Truck_Details table not found"}

        if not table_info["id_col"]:
            return {"success": False, "error": "Id column not found"}

        if not table_info["fuel_avg_col"]:
            return {"success": False, "error": "Fuel average column not found"}

        if not table_info["fuel_capacity_col"]:
            return {"success": False, "error": "Truck fuel capacity column not found"}

        if _truck_exists(cursor, table_info["table_ref"], table_info["truck_name_col"], truck_name):
            return {"success": False, "error": f'Truck "{truck_name}" already exists'}

        truck_id = _next_truck_id(cursor, table_info["table_ref"], table_info["id_col"])

        insert_query = f"""
            INSERT INTO {table_info["table_ref"]}
                ([{table_info["id_col"]}], [{table_info["truck_name_col"]}], [{table_info["fuel_avg_col"]}], [{table_info["fuel_capacity_col"]}])
            VALUES (?, ?, ?, ?)
        """
        cursor.execute(insert_query, (truck_id, truck_name, fuel_average_l, truck_fuel_capacity))
        conn.commit()

        return {
            "success": True,
            "message": f'✅ Truck "{truck_name}" registered successfully (ID: {truck_id})',
            "id": truck_id,
            "truck_name": truck_name,
            "fuel_average_l": fuel_average_l,
            "truck_fuel_capacity": truck_fuel_capacity,
        }
    except Exception as error:
        if conn:
            conn.rollback()
        return {"success": False, "error": str(error)}
    finally:
        if conn:
            conn.close()


def delete_truck_from_db(truck_name):
    """Delete a truck from the database."""
    truck_name = _normalize_name(truck_name)

    if not truck_name:
        return {"success": False, "error": "truck_name is required"}

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {"success": False, "error": "Database connection failed"}

        cursor = conn.cursor()
        table_info = _resolve_truck_table(cursor)
        if not table_info:
            return {"success": False, "error": "Truck_Detail/Truck_Details table not found"}

        delete_query = f"""
            DELETE FROM {table_info["table_ref"]}
            WHERE LOWER(LTRIM(RTRIM(CAST([{table_info["truck_name_col"]}] AS NVARCHAR(255))))) = LOWER(?)
        """
        cursor.execute(delete_query, (truck_name,))
        deleted_rows = cursor.rowcount
        conn.commit()

        if deleted_rows == 0:
            return {"success": False, "error": f'Truck "{truck_name}" not found'}

        return {
            "success": True,
            "message": f'✅ Truck "{truck_name}" removed successfully',
            "truck_name": truck_name,
            "deleted_rows": deleted_rows,
        }
    except Exception as error:
        if conn:
            conn.rollback()
        return {"success": False, "error": str(error)}
    finally:
        if conn:
            conn.close()


def main():
    """Render the Streamlit UI for truck registry."""
    st.title("🚚 Truck Registry")
    st.write("Register a truck with its fuel average and fuel capacity, or remove a truck by name.")
    action = st.radio(
        "What would you like to do?",
        options=["Add new truck", "Delete truck"],
        horizontal=True,
        key="truck_registry_action",
    )

    if action == "Add new truck":
        st.subheader("Add Truck")
        with st.form("add_truck_form"):
            truck_name_add = st.text_input("Truck Name", placeholder="Example: Truck 12")
            fuel_average_l = st.number_input(
                "Fuel Average (L)",
                min_value=0.01,
                step=0.01,
                value=None,
                placeholder="Example: 4.50",
            )
            truck_fuel_capacity = st.number_input(
                "Truck Fuel Capacity",
                min_value=0.01,
                step=0.01,
                value=None,
                placeholder="Example: 400",
            )
            submitted = st.form_submit_button("Register Truck", type="primary")

        if submitted:
            if not truck_name_add:
                st.error("Truck name is required")
            elif fuel_average_l is None:
                st.error("Fuel average is required")
            elif fuel_average_l <= 0:
                st.error("Fuel average must be greater than 0")
            elif truck_fuel_capacity is None:
                st.error("Truck fuel capacity is required")
            elif truck_fuel_capacity <= 0:
                st.error("Truck fuel capacity must be greater than 0")
            else:
                result = add_truck_to_db(truck_name_add, fuel_average_l, truck_fuel_capacity)
                if result["success"]:
                    st.success(result["message"])
                else:
                    st.error(result["error"])

    elif action == "Delete truck":
        st.subheader("Delete Truck")
        with st.form("delete_truck_form"):
            truck_name_delete = st.text_input("Truck Name to Delete", placeholder="Example: Truck 12")
            submitted = st.form_submit_button("Remove Truck", type="secondary")

        if submitted:
            if not truck_name_delete:
                st.error("Truck name is required")
            else:
                result = delete_truck_from_db(truck_name_delete)
                if result["success"]:
                    st.success(result["message"])
                else:
                    st.error(result["error"])


if __name__ == "__main__":
    main()


# Tacoma
# Napavine
