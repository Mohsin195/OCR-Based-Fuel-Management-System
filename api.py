from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Annotated, Any, Optional

import app as pdf_app
import app_1 as truck_app
import app_2 as distance_app
import pandas as pd
import uvicorn
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field


app = FastAPI(
    title="Fuel Management & Truck Registration System",
    version="1.1.0",
    description=(
        "Production FastAPI entrypoint for the PDF price processor, truck registry, "
        "and distance/fuel calculator applications. The request forms mirror the "
        "current Streamlit workflows and include examples for Swagger UI users."
    ),
)


class ApiMessage(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"message": "New Work FastAPI service is running"}]})

    message: str = Field(description="Human-readable service message.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "ok"}]})

    status: str = Field(description="Health status for the API process.")


class PdfProcessResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "success": True,
                    "filename": "pcn-usd-8467454-4338.pdf",
                    "row_count": 25,
                    "columns": ["Name", "Your Price", "Saving"],
                    "preview": [{"Name": "CALGARY-REMINGTON", "Your Price": "1.234", "Saving": "0.045"}],
                    "csv": "Name,Your Price,Saving\nCALGARY-REMINGTON,1.234,0.045\n",
                    "database_updated": True,
                    "database_update_error": None,
                }
            ]
        }
    )

    success: bool = Field(description="Whether the PDF was parsed successfully.")
    filename: str = Field(description="Name of the uploaded or server-side PDF file.")
    row_count: int = Field(description="Number of output rows extracted from the PDF.")
    columns: list[str] = Field(description="CSV column names returned by the PDF processor.")
    preview: list[dict[str, Any]] = Field(description="First rows of the generated CSV for quick inspection.")
    csv: str = Field(description="Complete generated CSV text encoded as UTF-8.")
    database_updated: bool = Field(description="True when no database update warning was reported.")
    database_update_error: Optional[str] = Field(
        default=None,
        description="Database update warning from the PDF app. CSV output may still be valid when this is set.",
    )


class TruckCreateRequest(BaseModel):
    truck_name: str = Field(
        ...,
        min_length=1,
        examples=["Truck 12"],
        description="Display name or number of the truck. Required; leading/trailing spaces are normalized.",
    )
    fuel_average_l: float = Field(
        ...,
        gt=0,
        examples=[30.5],
        description="Truck fuel consumption in litres per 100 km. Must be greater than 0.",
    )
    truck_fuel_capacity: float = Field(
        ...,
        gt=0,
        examples=[200.0],
        description="Truck fuel tank capacity in litres. Must be greater than 0.",
    )


class TruckDeleteRequest(BaseModel):
    truck_name: str = Field(
        ...,
        min_length=1,
        examples=["Truck 12"],
        description="Exact truck name to remove. Matching is case-insensitive after trimming spaces.",
    )


class TruckMutationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = Field(description="Whether the requested truck operation succeeded.")
    message: Optional[str] = Field(default=None, description="Success message returned by the truck registry app.")
    error: Optional[str] = Field(default=None, description="Failure message returned by the truck registry app.")
    truck_name: Optional[str] = Field(default=None, description="Normalized truck name affected by the operation.")


class TruckListResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"success": True, "trucks": ["Truck 12"], "count": 1}]})

    success: bool = Field(description="Whether trucks were loaded successfully.")
    trucks: list[str] = Field(description="Available truck names sorted by the database query.")
    count: int = Field(description="Number of trucks returned.")


class TruckDetailsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = Field(description="Whether the truck was found and has a valid fuel average.")
    truck_name: Optional[str] = Field(default=None, description="Truck display name from the database.")
    fuel_average_l: Optional[float] = Field(default=None, description="Fuel consumption in litres per 100 km.")
    error: Optional[str] = Field(default=None, description="Failure message when the truck cannot be loaded.")


class LocationListResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"success": True, "locations": ["Calgary, 123 Main St"], "count": 1}]}
    )

    success: bool = Field(description="Whether the location list endpoint completed.")
    locations: list[str] = Field(description="Location labels built from LovesLocations city and address rows.")
    count: int = Field(description="Number of locations returned.")


class DistanceCalculationRequest(BaseModel):
    starting_point: str = Field(
        ...,
        min_length=1,
        description=(
            "Required starting location. Accepts a place name, city, landmark, or address. "
            "Example: Calgary, AB."
        ),
    )
    destination_point: str = Field(
        ...,
        min_length=1,
        description=(
            "Required destination location. Accepts a place name, city, landmark, or address. "
            "Example: Edmonton, AB."
        ),
    )
    truck_name: str = Field(
        ...,
        min_length=1,
        description="Required truck name. Must exist in the Truck_Detail or Truck_Details table. Example: Truck 12.",
    )
    current_fuel_l: float = Field(
        ...,
        ge=0,
        le=200,
        description=(
            "Current fuel available in the truck, in litres. Required by the updated app logic; "
            "values are constrained to the app's 0-200 L tank range. Example: 120."
        ),
    )


class RouteMapResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "route_map_file_uri": "file:///F:/PROJECTS/new_work/generated_maps/route_T1_ab12cd34.html"
                }
            ]
        }
    )

    route_map_file_uri: str = Field(description="Direct file URI for the generated HTML route map.")


def _csv_bytes_to_preview(csv_bytes: bytes, preview_rows: int = 25) -> tuple[list[str], list[dict[str, Any]], str, int]:
    text = csv_bytes.decode("utf-8-sig")
    df = pd.read_csv(io.StringIO(text))
    preview = df.head(preview_rows).fillna("").to_dict(orient="records")
    return list(df.columns), preview, text, len(df)


def _resolve_pdf_path(uploaded_file: Optional[UploadFile], server_path: Optional[str]) -> tuple[Path, Optional[Path]]:
    if uploaded_file is not None:
        suffix = Path(uploaded_file.filename or "upload.pdf").suffix.lower() or ".pdf"
        if suffix != ".pdf":
            raise HTTPException(status_code=422, detail="uploaded_file must be a PDF file with a .pdf extension")

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=pdf_app.BASE_DIR)
        temp_path = Path(temp_file.name)
        try:
            temp_file.write(uploaded_file.file.read())
        finally:
            temp_file.close()
        return temp_path, temp_path

    if server_path:
        resolved_path = Path(server_path)
        if not resolved_path.exists():
            raise HTTPException(status_code=404, detail=f"Server path not found: {server_path}")
        if not resolved_path.is_file():
            raise HTTPException(status_code=422, detail=f"Server path is not a file: {server_path}")
        if resolved_path.suffix.lower() != ".pdf":
            raise HTTPException(status_code=422, detail="server_path must point to a .pdf file")
        return resolved_path, None

    if pdf_app.DEMO_PDF.exists():
        return pdf_app.DEMO_PDF, None

    raise HTTPException(status_code=400, detail="Upload a PDF file or provide a valid server_path")


def _raise_for_app_error(result: dict[str, Any], fallback_detail: str) -> None:
    detail = result.get("error") or fallback_detail
    detail_text = str(detail).lower()
    if "not found" in detail_text:
        status_code = 404
    elif "required" in detail_text or "must be greater" in detail_text or "invalid" in detail_text:
        status_code = 422
    elif "database connection failed" in detail_text or "api_key is not configured" in detail_text:
        status_code = 503
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=detail)


@app.get(
    "/",
    tags=["Service"],
    summary="Show service entrypoints",
    description="Returns a small index of the available API groups and the Swagger UI path.",
)
def root() -> dict[str, Any]:
    return {
        "message": "New Work FastAPI service is running",
        "docs": "/docs",
        "pdf_processor": "/pdf/process",
        "truck_registry": "/truck-registry",
        "distance_calculator": "/distance",
    }


@app.get(
    "/health",
    tags=["Service"],
    response_model=HealthResponse,
    summary="Check API health",
    description="Lightweight health endpoint for process-level monitoring.",
)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


pdf_router = APIRouter(prefix="/pdf", tags=["PDF Processor"])


@pdf_router.post(
    "/process",
    response_model=PdfProcessResponse,
    summary="Process a pricing PDF",
    description=(
        "Upload a PDF or provide a server-side PDF path. The endpoint runs the same parser as the PDF app, "
        "generates CSV output, and attempts to update the Discounted_Fuel_Price table. If the database update "
        "fails, CSV output is still returned with database_update_error populated."
    ),
)
def process_pdf(
    uploaded_file: Annotated[
        Optional[UploadFile],
        File(description="Optional PDF upload. Takes priority over server_path when both are provided."),
    ] = None,
    server_path: Annotated[
        Optional[str],
        Form(
            description="Optional absolute or relative server-side path to a .pdf file.",
            examples=["pcn-usd-8467454-4338.pdf"],
        ),
    ] = None,
) -> PdfProcessResponse:
    pdf_path, temp_path = _resolve_pdf_path(uploaded_file, server_path)

    try:
        csv_bytes = pdf_app.process_pdf_to_csv(pdf_path)
        columns, preview, csv_text, row_count = _csv_bytes_to_preview(csv_bytes)
        database_update_error = pdf_app.LAST_DB_UPDATE_ERROR
        return PdfProcessResponse(
            success=True,
            filename=pdf_path.name,
            row_count=row_count,
            columns=columns,
            preview=preview,
            csv=csv_text,
            database_updated=database_update_error is None,
            database_update_error=database_update_error,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


truck_router = APIRouter(prefix="/truck-registry", tags=["Truck Registry"])


@truck_router.post(
    "/add",
    response_model=TruckMutationResponse,
    summary="Register a truck",
    description=(
        "Adds a truck to the Truck_Detail or Truck_Details table using the updated registry logic. "
        "The current app requires truck name, fuel average, and fuel capacity."
    ),
)
def add_truck(payload: Annotated[TruckCreateRequest, Form()]) -> dict[str, Any]:
    result = truck_app.add_truck_to_db(
        payload.truck_name,
        payload.fuel_average_l,
        payload.truck_fuel_capacity,
    )
    if not result.get("success"):
        _raise_for_app_error(result, "Failed to add truck")
    return result


@truck_router.post(
    "/delete",
    response_model=TruckMutationResponse,
    summary="Delete a truck",
    description="Removes a truck by name using the same case-insensitive matching as the registry app.",
)
def delete_truck(payload: Annotated[TruckDeleteRequest, Form()]) -> dict[str, Any]:
    result = truck_app.delete_truck_from_db(payload.truck_name)
    if not result.get("success"):
        _raise_for_app_error(result, "Failed to delete truck")
    return result


@truck_router.get(
    "/trucks",
    response_model=TruckListResponse,
    summary="List available trucks",
    description="Loads truck names from the database using the distance calculator's schema-detection logic.",
)
def list_trucks() -> TruckListResponse:
    result = distance_app.get_available_trucks()
    if not result.get("success"):
        _raise_for_app_error(result, "Failed to load trucks")
    trucks = result.get("trucks", [])
    return TruckListResponse(success=True, trucks=trucks, count=len(trucks))


@truck_router.get(
    "/trucks/{truck_name}",
    response_model=TruckDetailsResponse,
    summary="Get truck fuel average",
    description="Returns the fuel average for a truck, matching the lookup used before distance calculation.",
)
def truck_details(truck_name: str) -> dict[str, Any]:
    result = distance_app.get_truck_fuel_average(truck_name)
    if not result.get("success"):
        _raise_for_app_error(result, "Truck not found")
    return result


distance_router = APIRouter(prefix="/distance", tags=["Distance Calculator"])


@distance_router.get(
    "/locations",
    response_model=LocationListResponse,
    summary="List known fuel station locations",
    description="Returns city/address labels from LovesLocations for users who want database-backed location examples.",
)
def list_locations() -> LocationListResponse:
    locations = distance_app.load_locations_list()
    return LocationListResponse(success=True, locations=locations, count=len(locations))


@distance_router.post(
    "/calculate",
    response_model=RouteMapResponse,
    summary="Calculate distance, fuel plan, and save trip",
    description=(
        "Resolves both locations, calculates the fastest OpenRouteService route, evaluates current fuel against "
        "the app's 200 L tank and 20 km reserve rules, recommends required refuelling stops, stores a Trip_Records "
        "row, creates a local HTML route map, and returns only the map link."
    ),
)
def calculate_distance(payload: Annotated[DistanceCalculationRequest, Form()]) -> RouteMapResponse:
    try:
        result = distance_app.calculate_and_store_trip_record(
            starting_point=payload.starting_point,
            destination_point=payload.destination_point,
            truck_name=payload.truck_name,
            current_fuel_l=payload.current_fuel_l,
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Unexpected distance calculation error: {error}") from error

    if not result.get("success"):
        _raise_for_app_error(result, "Failed to calculate distance")

    try:
        route_map_file_uri = distance_app._build_route_map_file_uri(result)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Route map generation failed: {error}") from error

    if not route_map_file_uri:
        raise HTTPException(status_code=500, detail="Route map generation failed: no route coordinates were returned")

    return RouteMapResponse(route_map_file_uri=route_map_file_uri)


app.include_router(pdf_router)
app.include_router(truck_router)
app.include_router(distance_router)


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8501, reload=False)


#python -m uvicorn api:app --host 127.0.0.1 --port 8501   --run in bash
#http://127.0.0.1:8501/docs  -- Run in web