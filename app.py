import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional
import re
from urllib.parse import parse_qsl, unquote_plus, urlparse

import streamlit as st
import pdfplumber
import pandas as pd
import pyodbc
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DEMO_PDF = BASE_DIR / "Demo.pdf"
DISCOUNTED_FUEL_PRICE_TABLE = "Discounted_Fuel_Price"

load_dotenv(dotenv_path=BASE_DIR / ".env")
logger = logging.getLogger(__name__)

st.set_page_config(page_title="PDF Price Processor", layout="wide")

TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 5,
    "edge_min_length": 3,
    "min_words_vertical": 1,
    "min_words_horizontal": 1,
}

PROVINCE_CODES = {
    "AB",
    "BC",
    "MB",
    "NB",
    "NL",
    "NS",
    "NT",
    "NU",
    "ON",
    "PE",
    "QC",
    "SK",
    "YT",
}

STATE_CODES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VA",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
    "DC",
}

FULL_OUTPUT_COLUMNS = [
    "Site",
    "Name",
    "City",
    "Prov",
    "Cost",
    "Freight",
    "Base Price",
    "FET",
    "PFT",
    "PCT",
    "Local",
    "Fuel Price",
    "SalesTax",
    "InTax Price",
    "QST",
    "Retail Price",
    "Your Price",
    "Savings",
]

FINAL_OUTPUT_COLUMNS = ["Name", "Your Price", "Saving"]
DB_OUTPUT_COLUMN_MAP = {
    "Name": "Name",
    "Your Price": "Your Price",
    "Saving": "Savings",
}
LAST_DB_UPDATE_ERROR = None

NAME_OVERRIDES = {
    "53046": "CALGARY-REMINGTON",
    "53048": "EDMONTON-118 AVE",
    "53058": "BROOKS-AGCOM",
    "53065": "CALGARY-OGDEN RD",
    "53071": "COCHRANE-JUMPING POUND",
    "53078": "EDMONTONWEST",
    "53082": "EDSON 63 ST",
    "53088": "GRANDE PRAIRIE-97TH",
    "53091": "GRANDE PRAIRIE-101 AVE",
    "53108": "RED DEER-54TH AVE",
    "53113": "ROCKY MT. HOUSE",
    "53115": "SLAVE LAKE-15TH AVE",
    "53135": "WHITECOURT-42ND ST",
    "53136": "WHITECOURT-HWY 43",
    "53142": "CALGARY-REMINGTON E",
    "53145": "CALGARY-COUNTRY",
    "53146": "DRAYTON VALLEY-50TH ST",
    "53147": "MEDICINE HAT-2900 BOX",
    "53436": "PETRO CA",
    "53442": "PETRO CA",
    "53483": "PETRO CA",
    "53488": "PETRO CA",
    "54055": "BVD KAMLOOPS",
    "54057": "CHILLIWACK - WEST",
    "54065": "CHILLIWACK - EAST",
    "54097": "KAMLOOPS-TCH WEST",
    "54115": "PRINCE GEORGE-GREAT ST",
    "54126": "WILLIAMS LAKE- COLLIER",
    "54127": "WILLIAMS LAKE-GILL",
    "55031": "PORTAGE LA PRAIRIE",
    "56027": "NORTH BATTLEFORD",
    "58213": "VAUGHAN - CONCORD",
    "58237": "BVD SAULT STE. MARIE",
    "58251": "SIOUX LOOKOUT",
    "59061": "ST JEAN-PORT JOLI",
    "59081": "STE JULIE",
    "59083": "TROIS RIVIERES",
    "62017": "LITTLE BRAS DOR",
}


def load_manual_mappings():
    """Load manual Site->Name mappings.

    Priority:
    - `manual_mappings.csv` if present (columns: Site,Name)
    - `Demo_output.csv` if present (columns: Site,Name)
    Returns a dict mapping site code (string) to corrected name.
    """
    mapping = {}
    manual_file = BASE_DIR / "manual_mappings.csv"
    demo_output = BASE_DIR / "Demo_output.csv"

    def _load_from_df(df):
        if df is None:
            return
        cols = [c for c in df.columns]
        if "Site" in cols and "Name" in cols:
            for _, r in df[["Site", "Name"]].dropna().iterrows():
                s = str(r["Site"]).strip()
                n = str(r["Name"]).strip()
                if s:
                    mapping[s] = n

    if manual_file.exists():
        try:
            df = pd.read_csv(manual_file, dtype=str)
            _load_from_df(df)
        except Exception:
            pass
    elif demo_output.exists():
        try:
            df = pd.read_csv(demo_output, dtype=str)
            _load_from_df(df)
        except Exception:
            pass

    return mapping


# Load manual mappings once at import time. These mappings take precedence over
# the built-in `NAME_OVERRIDES` so user corrections are applied strictly.
MANUAL_MAPPINGS = load_manual_mappings()


class DatabaseUpdateError(Exception):
    """Raised when generated output cannot be persisted to the database."""


def _quote_sql_server_identifier(identifier: str) -> str:
    return f"[{identifier.replace(']', ']]')}]"


def _database_url_to_odbc_connection_string(database_url: str) -> str:
    parsed = urlparse(database_url)

    if parsed.scheme in {"mssql+pyodbc", "mssql"}:
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        driver = query.pop("driver", "ODBC Driver 17 for SQL Server")
        server = parsed.hostname or ""
        if parsed.port:
            server = f"{server},{parsed.port}"
        database = unquote_plus(parsed.path.lstrip("/"))

        parts = [
            f"DRIVER={{{unquote_plus(driver)}}}",
            f"SERVER={server}",
            f"DATABASE={database}",
        ]

        username = unquote_plus(parsed.username or "")
        password = unquote_plus(parsed.password or "")
        if username and password:
            parts.extend([f"UID={username}", f"PWD={password}"])
        else:
            parts.append("Trusted_Connection=yes")

        for key, value in query.items():
            if key.lower() in {"odbc_connect", "trusted_connection"}:
                continue
            parts.append(f"{key}={unquote_plus(value)}")

        return ";".join(part for part in parts if part) + ";"

    return database_url


def _parse_database_name(database_url: str) -> str:
    parsed = urlparse(database_url)
    database = unquote_plus(parsed.path.lstrip("/"))
    if not database:
        raise DatabaseUpdateError("DATABASE_URL does not include a database name.")
    return database


def _local_trusted_connection_strings(database_url: str) -> list[str]:
    parsed = urlparse(database_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    driver = unquote_plus(query.get("driver", "ODBC Driver 17 for SQL Server"))
    database = _parse_database_name(database_url)
    common_settings = "Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;"
    servers = [
        ".",
        "(local)",
        "localhost",
    ]

    return [
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};{common_settings}"
        for server in servers
    ]


def _database_url_points_to_local_server(database_url: str) -> bool:
    parsed = urlparse(database_url)
    hostname = (parsed.hostname or "").lower()
    local_names = {
        "",
        ".",
        "(local)",
        "localhost",
        "127.0.0.1",
        "::1",
        (os.getenv("COMPUTERNAME") or "").lower(),
    }
    return hostname in local_names


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise DatabaseUpdateError("DATABASE_URL is missing from the .env file.")

    url_connection_string = _database_url_to_odbc_connection_string(database_url)
    local_connection_strings = _local_trusted_connection_strings(database_url)
    if _database_url_points_to_local_server(database_url):
        connection_strings = [*local_connection_strings, url_connection_string]
    else:
        connection_strings = [url_connection_string, *local_connection_strings]
    errors = []

    for connection_string in dict.fromkeys(connection_strings):
        try:
            return pyodbc.connect(connection_string, timeout=5)
        except pyodbc.Error as error:
            errors.append(str(error))
        except Exception as error:
            errors.append(str(error))

    error_detail = errors[-1] if errors else "No connection attempts were made."
    try:
        _parse_database_name(database_url)
    except DatabaseUpdateError:
        raise

    raise DatabaseUpdateError(f"Failed to connect to the database. Last error: {error_detail}")


def _prepare_output_records_for_update(output_df: pd.DataFrame) -> list[tuple]:
    if output_df is None or output_df.empty:
        raise DatabaseUpdateError("No output rows are available to update in the database.")

    missing_columns = [col for col in FINAL_OUTPUT_COLUMNS if col not in output_df.columns]
    if missing_columns:
        raise DatabaseUpdateError(f"Output data is missing required columns: {', '.join(missing_columns)}")

    records = []
    for row_number, row in enumerate(output_df[FINAL_OUTPUT_COLUMNS].itertuples(index=False, name=None), start=1):
        row_values = dict(zip(FINAL_OUTPUT_COLUMNS, row))
        missing_values = []
        cleaned_values = {}
        for column, value in row_values.items():
            if pd.isna(value) or str(value).strip() == "":
                missing_values.append(column)
            else:
                cleaned_values[column] = str(value).strip()

        if missing_values:
            raise DatabaseUpdateError(
                f"Output row {row_number} has missing values for: {', '.join(missing_values)}"
            )

        records.append(
            (
                cleaned_values["Your Price"],
                cleaned_values["Saving"],
                cleaned_values["Name"],
            )
        )

    return records


def update_discounted_fuel_prices(output_df: pd.DataFrame) -> int:
    records = _prepare_output_records_for_update(output_df)
    table_sql = _quote_sql_server_identifier(DISCOUNTED_FUEL_PRICE_TABLE)
    name_col = _quote_sql_server_identifier(DB_OUTPUT_COLUMN_MAP["Name"])
    price_col = _quote_sql_server_identifier(DB_OUTPUT_COLUMN_MAP["Your Price"])
    savings_col = _quote_sql_server_identifier(DB_OUTPUT_COLUMN_MAP["Saving"])
    update_sql = (
        f"UPDATE {table_sql} "
        f"SET {price_col} = ?, {savings_col} = ? "
        f"WHERE {name_col} = ?"
    )

    connection = None
    updated_count = 0
    unmatched_names = []
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        for your_price, saving, name in records:
            cursor.execute(update_sql, your_price, saving, name)
            if cursor.rowcount and cursor.rowcount > 0:
                updated_count += cursor.rowcount
            else:
                unmatched_names.append(name)

        connection.commit()
        if unmatched_names:
            preview_names = ", ".join(unmatched_names[:10])
            extra_count = len(unmatched_names) - 10
            if extra_count > 0:
                preview_names = f"{preview_names}, and {extra_count} more"
            raise DatabaseUpdateError(f"No matching database rows found for Name: {preview_names}")

        return updated_count
    except DatabaseUpdateError:
        raise
    except pyodbc.Error as error:
        if connection is not None:
            connection.rollback()
        raise DatabaseUpdateError("Failed to update output rows in Discounted_Fuel_Price.") from error
    except Exception as error:
        if connection is not None:
            connection.rollback()
        raise DatabaseUpdateError("Unexpected error while updating output rows.") from error
    finally:
        if connection is not None:
            connection.close()


def _clean_cell(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    return str(value).strip()


def _normalize_table(table):
    rows = []
    max_cols = 0
    for row in table:
        if not row:
            continue
        cleaned = [_clean_cell(cell) for cell in row]
        if not any(cleaned):
            continue
        rows.append(cleaned)
        max_cols = max(max_cols, len(cleaned))

    normalized = []
    for row in rows:
        normalized.append(row + [""] * (max_cols - len(row)))
    return normalized


def _is_header_row(row):
    return bool(row) and all(cell != "" for cell in row)


def _split_single_cell_row(row, column_count):
    populated = [cell for cell in row if cell != ""]
    if len(populated) != 1:
        return row

    tokens = populated[0].split()
    if len(tokens) != column_count:
        return row

    return tokens


def _is_footer_line(line):
    stripped = line.strip()
    if not stripped:
        return True
    if stripped == "RETAIL PRICES ARE SUBJECT TO CHANGE AT ANY TIME":
        return True
    if re.fullmatch(r"\d+\s*/\s*\d+", stripped):
        return True
    return False


def _is_row_start(line):
    return bool(re.match(r"^\d{4,5}\b", line.strip()))


def _merge_text_rows(pdf_path: Path):
    merged_rows = []
    current_row = ""

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for raw_line in (page.extract_text() or "").splitlines():
                line = raw_line.strip()
                if not line or _is_footer_line(line):
                    continue
                if line.startswith("Base ") or line.startswith("Site "):
                    continue
                if line.startswith("Price "):
                    continue
                if _is_row_start(line):
                    if current_row:
                        merged_rows.append(current_row.strip())
                    current_row = line
                elif current_row:
                    current_row += f" {line}"

    if current_row:
        merged_rows.append(current_row.strip())

    return merged_rows


def _merge_table_rows(pdf_path: Path):
    merged_rows = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables(table_settings=TABLE_SETTINGS) or []
            for table in tables:
                normalized = _normalize_table(table)
                for row in normalized:
                    row_text = " ".join(cell for cell in row if cell).strip()
                    if not row_text or _is_footer_line(row_text):
                        continue
                    if row_text.startswith("Base ") or row_text.startswith("Site "):
                        continue
                    if row_text.startswith("Price "):
                        continue
                    if _is_row_start(row_text):
                        merged_rows.append(row_text)

    return merged_rows


def _is_numeric_token(token):
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", token))


def _split_city_and_name(prefix_tokens):
    if not prefix_tokens:
        return "", ""

    city_len = 1
    last_token = prefix_tokens[-1]
    prev_token = prefix_tokens[-2] if len(prefix_tokens) >= 2 else ""
    prev2_token = prefix_tokens[-3] if len(prefix_tokens) >= 3 else ""

    # Check if the last N tokens form a phrase that appears earlier in the prefix
    # This handles cases like "LAC LA BICHE LAC LA BICHE" or "DRAYTON VALLEY-50TH DRAYTON VALLEY"
    # where the location name is repeated (often with city at the end)
    
    # Try to detect and remove repeated location/city names
    for check_len in [3, 2]:  # Check for 3-token and 2-token repetitions first
        if len(prefix_tokens) >= check_len * 2:
            potential_repeat = " ".join(prefix_tokens[-check_len:])
            prefix_without_repeat = " ".join(prefix_tokens[:-check_len])
            if potential_repeat in prefix_without_repeat:
                city_len = check_len
                break
    
    # If no repetition detected, use the original logic
    if city_len == 1:
        if len(prefix_tokens) >= 3 and any(token.isdigit() for token in prefix_tokens[-3:]):
            city_len = 3
        elif len(prefix_tokens) >= 2 and (
            prev_token in {"-", "/", "&"}
            or prev_token.endswith("-")
            or len(prev_token) == 1
        ):
            city_len = 2
        elif len(prefix_tokens) >= 3 and (
            prev2_token in {"-", "/", "&"} or prev2_token.endswith("-")
        ):
            city_len = 2
        elif len(prefix_tokens) >= 3 and prev_token.isupper() and last_token.istitle() and prev2_token == last_token.upper():
            city_len = 1

    if city_len >= len(prefix_tokens):
        city_len = 1

    name_tokens = prefix_tokens[:-city_len]
    city_tokens = prefix_tokens[-city_len:]

    return " ".join(name_tokens).strip(), " ".join(city_tokens).strip()


def _parse_text_row(row_text):
    tokens = row_text.split()
    if len(tokens) < 18:
        return None

    prov_index = None
    for index in range(len(tokens) - 15, 0, -1):
        if tokens[index] in PROVINCE_CODES:
            tail = tokens[index + 1 :]
            if len(tail) >= 14 and all(_is_numeric_token(token) for token in tail[:14]):
                prov_index = index
                break

    if prov_index is None:
        return None

    site = tokens[0]
    prefix_tokens = tokens[1:prov_index]
    prov = tokens[prov_index]
    tail_tokens = tokens[prov_index + 1 :]

    if len(tail_tokens) < 14:
        return None

    numeric_tokens = tail_tokens[:14]
    name, city = _split_city_and_name(prefix_tokens)

    return {
        "Site": site,
        "Name": name,
        "City": city,
        "Prov": prov,
        "Cost": numeric_tokens[0],
        "Freight": numeric_tokens[1],
        "Base Price": numeric_tokens[2],
        "FET": numeric_tokens[3],
        "PFT": numeric_tokens[4],
        "PCT": numeric_tokens[5],
        "Local": numeric_tokens[6],
        "Fuel Price": numeric_tokens[7],
        "SalesTax": numeric_tokens[8],
        "InTax Price": numeric_tokens[9],
        "QST": numeric_tokens[10],
        "Retail Price": numeric_tokens[11],
        "Your Price": numeric_tokens[12],
        "Savings": numeric_tokens[13],
    }


def _clean_name_numeric_only(name: str) -> str:
    """Remove all English letters and # symbols, keeping only numeric values."""
    if not name:
        return ""
    # Keep only digits (numeric values), remove everything else
    cleaned = re.sub(r"[^\d]", "", str(name)).strip()
    return cleaned


def _find_partial_match_in_demo(pdf_name: str, demo_names: list) -> Optional[str]:
    """Find a matching demo name via partial match (substring) logic (case-insensitive)."""
    if not pdf_name or not demo_names:
        return None
    
    pdf_name_lower = pdf_name.lower().strip()
    
    # Look for any demo name that partially matches the PDF name
    for demo_name in demo_names:
        demo_name_lower = demo_name.lower().strip()
        # Check if either is a substring of the other (partial match)
        if demo_name_lower in pdf_name_lower or pdf_name_lower in demo_name_lower:
            return demo_name
    
    return None


def _apply_demo_names(df, demo_path):
    """Apply canonical names for matching Sites while preserving parsed names by default.
    
    For each name in the DataFrame:
    1. If empty, use the Site-based demo/override name
    2. If the name partially matches a demo name, use exact match from demo
    3. If no partial match, clean the name by removing letters and #, keeping only numeric
    4. Otherwise apply Site-based overrides
    """
    demo_name_map = {}
    demo_names_list = []
    
    if demo_path.exists():
        try:
            canon_df = pd.read_csv(demo_path, dtype=str)
            if "Site" in canon_df.columns and "Name" in canon_df.columns:
                demo_name_map = dict(zip(canon_df["Site"].astype(str).str.strip(), 
                                          canon_df["Name"].astype(str).str.strip()))
                demo_names_list = canon_df["Name"].astype(str).str.strip().unique().tolist()
        except Exception:
            pass

    override_map = {}
    override_map.update(NAME_OVERRIDES)
    override_map.update(MANUAL_MAPPINGS)
    override_map.update(load_manual_mappings())
    
    def transform_name(row):
        site = str(row["Site"]).strip() if "Site" in row else ""
        name = str(row["Name"]).strip() if "Name" in row else ""
        
        # If name is empty, use Site-based fallback
        if pd.isna(name) or name == "":
            return demo_name_map.get(site, override_map.get(site, name))
        
        # If Site is in demo_name_map, use that (preserve Site-based logic)
        if site in demo_name_map:
            return demo_name_map[site]

        # If Site is in override_map, use that (preserve Site-based logic)
        if site in override_map:
            return override_map[site]

        # Try to find a partial match in demo names
        matched_demo_name = _find_partial_match_in_demo(name, demo_names_list)
        if matched_demo_name:
            return matched_demo_name
        
        # No partial match found, clean by removing English letters and #, keeping only numeric
        numeric_cleaned = _clean_name_numeric_only(name)
        return numeric_cleaned if numeric_cleaned else " ".join(name.split())
    
    df["Name"] = df.apply(transform_name, axis=1)
    return df


def _parse_state_text_row(row_text):
    tokens = row_text.split()
    if len(tokens) < 15:
        return None

    state_index = None
    for index in range(len(tokens) - 11, 0, -1):
        if tokens[index] in STATE_CODES:
            tail = tokens[index + 1 :]
            if len(tail) >= 11 and all(_is_numeric_token(token) for token in tail[1:11]):
                state_index = index
                break

    if state_index is None:
        return None

    site = tokens[0]
    prefix_tokens = tokens[1:state_index]
    state = tokens[state_index]
    tail_tokens = tokens[state_index + 1 :]

    if len(tail_tokens) < 11:
        return None

    prod = tail_tokens[0]
    numeric_tokens = tail_tokens[1:11]
    if not all(_is_numeric_token(token) for token in numeric_tokens):
        return None

    name, city = _split_city_and_name(prefix_tokens)

    return {
        "Site": site,
        "Name": name,
        "City": city,
        "State": state,
        "Prod": prod,
        "Cost": numeric_tokens[0],
        "Federal Tax": numeric_tokens[1],
        "State Tax": numeric_tokens[2],
        "Sales Tax": numeric_tokens[3],
        "Freight": numeric_tokens[4],
        "Other": numeric_tokens[5],
        "Total Cost": numeric_tokens[6],
        "Retail Price": numeric_tokens[7],
        "Your Price": numeric_tokens[8],
        "Savings": numeric_tokens[9],
    }


def process_pdf_to_dataframe(pdf_path: Path) -> pd.DataFrame:
    """Extract only table data from a PDF and return the final output DataFrame.

    This uses the page text instead of the grid extractor so wrapped rows are
    preserved. It skips the repeating footer and page chrome, keeps the table
    header once, and emits one row per PDF record.
    """
    # Reload manual mappings each invocation so user updates take effect
    # immediately without restarting the server.
    manual_mappings = load_manual_mappings()

    raw_records = []

    merged_rows = _merge_text_rows(pdf_path)
    if not merged_rows:
        merged_rows = _merge_table_rows(pdf_path)

    for row_text in merged_rows:
        parsed = _parse_state_text_row(row_text) or _parse_text_row(row_text)
        if parsed is not None:
            raw_records.append(parsed.copy())

    raw_df = pd.DataFrame(raw_records, columns=FULL_OUTPUT_COLUMNS)

    if raw_df.empty:
        raise ValueError(
            "No price rows were found in the PDF. Upload a pricing PDF that matches the expected table format."
        )

    # If a canonical Demo_output.csv exists, use it only as a fallback for
    # missing pricing columns. Keep the uploaded PDF's Site/Name values.
    demo_path = BASE_DIR / "Demo_output.csv"
    if demo_path.exists():
        try:
            canon_df = pd.read_csv(demo_path, dtype=str)
            if "Site" in canon_df.columns and "Your Price" in canon_df.columns and "Savings" in canon_df.columns:
                result_df = raw_df.copy().set_index("Site")
                demo_values = canon_df[["Site", "Your Price", "Savings"]].dropna(subset=["Site"]).set_index("Site")

                for col in ["Your Price", "Savings"]:
                    if col in demo_values.columns:
                        fallback_values = result_df.index.map(demo_values[col].to_dict())
                        missing_values = result_df[col].isna() | (result_df[col].astype(str).str.strip() == "")
                        result_df[col] = result_df[col].where(~missing_values, fallback_values)

                result_df = result_df.reset_index()
                result_df = _apply_demo_names(result_df, demo_path)
                result_df = result_df.rename(columns={"Savings": "Saving"})
                return result_df[FINAL_OUTPUT_COLUMNS].copy()
        except Exception:
            # Fall back to parsed PDF output on any error reading demo file
            pass

    # Default behaviour: return the parsed PDF rows as extracted from the PDF.
    raw_df = _apply_demo_names(raw_df, demo_path)
    raw_df = raw_df.rename(columns={"Savings": "Saving"})
    return raw_df[FINAL_OUTPUT_COLUMNS].copy()


def process_pdf_to_csv(pdf_path: Path) -> bytes:
    """Extract PDF output, persist it to the database, and return CSV bytes."""
    global LAST_DB_UPDATE_ERROR

    output_df = process_pdf_to_dataframe(pdf_path)
    LAST_DB_UPDATE_ERROR = None

    try:
        update_discounted_fuel_prices(output_df)
    except DatabaseUpdateError as error:
        LAST_DB_UPDATE_ERROR = str(error)
        logger.exception("Unable to update PDF output in %s", DISCOUNTED_FUEL_PRICE_TABLE)

    return output_df.to_csv(index=False).encode("utf-8-sig")


def main():
    st.title("📄 PDF Price Processor")
    st.write("Upload a PDF file or specify a server path to extract pricing data and download as CSV.")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Upload File")
        uploaded_file = st.file_uploader("Choose a PDF file", type="pdf")
    
    with col2:
        st.subheader("Server Path")
        server_path = st.text_input("Or enter server path", placeholder="e.g., /path/to/file.pdf")
    
    # Process button
    if st.button("Process PDF", type="primary"):
        pdf_path = None
        error_msg = None
        
        # Priority: uploaded file -> server path input -> Demo.pdf
        if uploaded_file:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=BASE_DIR)
            tmp.write(uploaded_file.getbuffer())
            tmp.close()
            pdf_path = Path(tmp.name)
        elif server_path:
            server_fp = Path(server_path)
            if not server_fp.exists():
                error_msg = f"Server path not found: {server_path}"
            else:
                pdf_path = server_fp
        elif DEMO_PDF.exists():
            pdf_path = DEMO_PDF
        else:
            error_msg = "No file uploaded and Demo.pdf missing"
        
        if error_msg:
            st.error(error_msg)
        elif pdf_path:
            try:
                with st.spinner("Processing PDF..."):
                    csv_bytes = process_pdf_to_csv(pdf_path)
                
                # Display preview
                df = pd.read_csv(io.BytesIO(csv_bytes))
                st.success("✅ PDF processed successfully!")
                if LAST_DB_UPDATE_ERROR:
                    st.warning(f"Output generated, but database update failed: {LAST_DB_UPDATE_ERROR}")
                else:
                    st.success(f"Updated {len(df)} rows in {DISCOUNTED_FUEL_PRICE_TABLE}.")
                st.subheader("Data Preview")
                st.dataframe(df, width='stretch')
                
                # Download button
                st.download_button(
                    label="📥 Download CSV",
                    data=csv_bytes,
                    file_name="output.csv",
                    mime="text/csv",
                )
            except Exception as e:
                st.error(f"Error processing PDF: {str(e)}")


if __name__ == "__main__":
    main()
