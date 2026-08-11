import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import httpx

load_dotenv()

IO_API_KEY = os.getenv("INFLATABLE_OFFICE_API_KEY", "").strip()
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
IO_BASE_URL = os.getenv("IO_BASE_URL", "https://rental.software/api6").rstrip("/")

app = FastAPI(title="Callahan InflatableOffice Bridge", version="0.1.0")


def require_config():
    if not IO_API_KEY:
        raise HTTPException(status_code=500, detail="INFLATABLE_OFFICE_API_KEY is not configured")
    if not BRIDGE_TOKEN:
        raise HTTPException(status_code=500, detail="BRIDGE_TOKEN is not configured")


def authorize(authorization: Optional[str]):
    require_config()
    expected = f"Bearer {BRIDGE_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def io_get(path: str, params: Optional[dict] = None):
    require_config()
    params = dict(params or {})
    params["apiKey"] = IO_API_KEY
    url = f"{IO_BASE_URL}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params, headers={"Accept": "application/json"})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"InflatableOffice connection failed: {exc.__class__.__name__}")

    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="InflatableOffice rate limit reached")
    if response.status_code >= 400:
        # Never return a requested URL here because it includes the IO API key.
        raise HTTPException(status_code=502, detail=f"InflatableOffice returned HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="InflatableOffice returned a non-JSON response")


@app.get("/")
async def root():
    return {"service": "Callahan InflatableOffice Bridge", "status": "ok", "mode": "read-only"}


@app.get("/health")
async def health(authorization: Optional[str] = Header(default=None)):
    authorize(authorization)
    # Rentals is the endpoint InflatableOffice documents as an API-key test.
    data = await io_get("rentals", {"limit": 1})
    return {"bridge": "ok", "inflatableOffice": "ok", "sampleCount": len(data.get("items", [])) if isinstance(data, dict) else None}


@app.get("/leads")
async def leads(
    authorization: Optional[str] = Header(default=None),
    filter: Optional[str] = Query(default=None),
    body: bool = Query(default=True),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    authorize(authorization)
    params = {"_body": "true" if body else "false", "offset": offset, "limit": limit}
    if filter:
        params["filter"] = filter
    return await io_get("leads/", params)


@app.get("/leads/{lead_id}")
async def lead_detail(
    lead_id: int,
    authorization: Optional[str] = Header(default=None),
    body: bool = Query(default=True),
):
    authorize(authorization)
    return await io_get(f"leads/{lead_id}", {"_body": "true" if body else "false"})


@app.get("/rentals")
async def rentals(
    authorization: Optional[str] = Header(default=None),
    body: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    authorize(authorization)
    return await io_get("rentals", {"_body": "true" if body else "false", "offset": offset, "limit": limit})


@app.get("/workers")
async def workers(
    authorization: Optional[str] = Header(default=None),
    approved: bool = Query(default=True),
    vehicles: bool = Query(default=False),
    body: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    authorize(authorization)
    params = {
        "_body": "true" if body else "false",
        "approved": 1 if approved else 0,
        "vehicle": 1 if vehicles else 0,
        "offset": offset,
        "limit": limit,
    }
    return await io_get("workers/", params)


@app.get("/vehicles")
async def vehicles(
    authorization: Optional[str] = Header(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    authorize(authorization)
    return await io_get("workers/", {"vehicle": 1, "offset": offset, "limit": limit})


@app.get("/categories")
async def categories(
    authorization: Optional[str] = Header(default=None),
    wpid: int = Query(default=0, ge=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    authorize(authorization)
    return await io_get("categories_list/", {"wpid": wpid, "offset": offset, "limit": limit})


@app.get("/locations")
async def locations(
    authorization: Optional[str] = Header(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    authorize(authorization)
    return await io_get("locations/", {"offset": offset, "limit": limit})


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
