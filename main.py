import os
from typing import Optional
from datetime import datetime, timedelta
from collections import Counter

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

load_dotenv()

IO_API_KEY = os.getenv("INFLATABLE_OFFICE_API_KEY", "").strip()
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
IO_BASE_URL = os.getenv("IO_BASE_URL", "https://rental.software/api6").rstrip("/")

app = FastAPI(
    title="Callahan InflatableOffice Bridge",
    version="1.2.0"
)

security = HTTPBearer()


def require_config():
    if not IO_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="INFLATABLE_OFFICE_API_KEY is not configured"
        )
    if not BRIDGE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="BRIDGE_TOKEN is not configured"
        )


def check_token(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    require_config()

    if credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    return True


async def io_get(path: str, params: Optional[dict] = None):
    require_config()

    params = dict(params or {})
    params["apiKey"] = IO_API_KEY
    url = f"{IO_BASE_URL}/{path.lstrip('/')}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                url,
                params=params,
                headers={"Accept": "application/json"}
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"InflatableOffice connection failed: {exc.__class__.__name__}"
        )

    if response.status_code == 429:
        raise HTTPException(
            status_code=429,
            detail="InflatableOffice rate limit reached"
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"InflatableOffice returned HTTP {response.status_code}"
        )

    try:
        return response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="InflatableOffice returned a non-JSON response"
        )


def _find_list(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("items", "results", "data", "leads"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def _parse_date(value):
    if not value:
        return None

    text = str(value).strip()

    formats = (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %I:%M %p",
    )

    for fmt in formats:
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date()
    except Exception:
        return None


def _lead_date(lead):
    if not isinstance(lead, dict):
        return None

    for key in (
        "eventStart",
        "event_start",
        "start",
        "startDate",
        "start_date",
        "date",
        "eventDate",
        "event_date",
    ):
        if key in lead:
            parsed = _parse_date(lead.get(key))
            if parsed:
                return parsed

    body = lead.get("body")
    if isinstance(body, dict):
        return _lead_date(body)

    return None


def _rental_items(lead):
    if not isinstance(lead, dict):
        return []

    possible = []

    for key in ("rentals", "rentalItems", "rental_items", "items"):
        value = lead.get(key)
        if isinstance(value, list):
            possible.extend(value)

    body = lead.get("body")
    if isinstance(body, dict):
        possible.extend(_rental_items(body))

    output = []

    for item in possible:
        if isinstance(item, str):
            output.append((item, 1))
            continue

        if not isinstance(item, dict):
            continue

        name = (
            item.get("name")
            or item.get("rentalName")
            or item.get("rental_name")
            or item.get("title")
        )

        qty = (
            item.get("quantity")
            or item.get("qty")
            or item.get("count")
            or 1
        )

        if name:
            try:
                qty = int(qty)
            except Exception:
                qty = 1

            output.append((str(name), qty))

    return output


@app.get("/")
async def root():
    return {
        "service": "Callahan InflatableOffice Bridge",
        "status": "ok",
        "mode": "read-only"
    }


@app.get("/health")
async def health(_: bool = Depends(check_token)):
    data = await io_get("rentals", {"limit": 1})

    return {
        "bridge": "ok",
        "inflatableOffice": "ok",
        "sampleCount": (
            len(data.get("items", []))
            if isinstance(data, dict)
            else None
        )
    }


@app.get("/leads")
async def leads(
    _: bool = Depends(check_token),
    filter: Optional[str] = Query(default=None),
    body: bool = Query(default=True),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    params = {
        "_body": "true" if body else "false",
        "offset": offset,
        "limit": limit
    }

    if filter:
        params["filter"] = filter

    return await io_get("leads/", params)


@app.get("/leads/{lead_id}")
async def lead_detail(
    lead_id: int,
    _: bool = Depends(check_token),
    body: bool = Query(default=True),
):
    return await io_get(
        f"leads/{lead_id}",
        {"_body": "true" if body else "false"}
    )


@app.get("/rentals")
async def rentals(
    _: bool = Depends(check_token),
    body: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    return await io_get(
        "rentals",
        {
            "_body": "true" if body else "false",
            "offset": offset,
            "limit": limit
        }
    )


@app.get("/workers")
async def workers(
    _: bool = Depends(check_token),
    approved: bool = Query(default=True),
    vehicles: bool = Query(default=False),
    body: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
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
    _: bool = Depends(check_token),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    return await io_get(
        "workers/",
        {
            "vehicle": 1,
            "offset": offset,
            "limit": limit
        }
    )


@app.get("/categories")
async def categories(
    _: bool = Depends(check_token),
    wpid: int = Query(default=0, ge=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    return await io_get(
        "categories_list/",
        {
            "wpid": wpid,
            "offset": offset,
            "limit": limit
        }
    )


@app.get("/locations")
async def locations(
    _: bool = Depends(check_token),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    return await io_get(
        "locations/",
        {
            "offset": offset,
            "limit": limit
        }
    )


@app.get("/public/weekend-items")
async def public_weekend_items():
    today = datetime.now().date()

    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)

    data = await io_get(
        "leads/",
        {
            "_body": "true",
            "offset": 0,
            "limit": 100
        }
    )

    lead_list = _find_list(data)
    totals = Counter()
    matching_leads = 0

    for lead in lead_list:
        if not isinstance(lead, dict):
            continue

        event_date = _lead_date(lead)

        if event_date not in (saturday, sunday):
            continue

        matching_leads += 1

        for name, qty in _rental_items(lead):
            totals[name] += qty

    return {
        "weekend": {
            "saturday": str(saturday),
            "sunday": str(sunday)
        },
        "leadCount": matching_leads,
        "items": [
            {
                "name": name,
                "quantity": quantity
            }
            for name, quantity in sorted(totals.items())
        ]
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )
