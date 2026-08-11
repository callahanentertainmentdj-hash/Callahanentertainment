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
IO_BASE_URL = os.getenv(
    "IO_BASE_URL",
    "https://rental.software/api6"
).rstrip("/")

app = FastAPI(
    title="Callahan InflatableOffice Bridge",
    version="1.3.0"
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
            detail=(
                "InflatableOffice connection failed: "
                f"{exc.__class__.__name__}"
            )
        )

    if response.status_code == 429:
        raise HTTPException(
            status_code=429,
            detail="InflatableOffice rate limit reached"
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=(
                "InflatableOffice returned HTTP "
                f"{response.status_code}"
            )
        )

    try:
        return response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="InflatableOffice returned a non-JSON response"
        )


async def io_get_all(path: str, params: Optional[dict] = None, page_size: int = 100):
    all_items = []
    offset = 0

    while True:
        page_params = dict(params or {})
        page_params["offset"] = offset
        page_params["limit"] = page_size

        data = await io_get(path, page_params)

        if isinstance(data, list):
            all_items.extend(data)
            break

        if not isinstance(data, dict):
            break

        items = data.get("items", [])
        if not isinstance(items, list):
            break

        all_items.extend(items)

        if len(items) < page_size:
            break

        offset += page_size

    return all_items


def parse_io_date(value):
    if not value:
        return None

    text = str(value).strip()

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).date()
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    return None


def is_confirmed_lead(lead):
    if not isinstance(lead, dict):
        return False

    status = lead.get("status")

    if isinstance(status, dict):
        confirmed_flag = str(
            status.get("confirmed", "")
        ).strip().lower()

        status_name = str(
            status.get("name", "")
        ).strip().lower()

        return (
            confirmed_flag in {"1", "true", "yes"}
            or status_name == "confirmed"
        )

    status_name = str(
        lead.get("statusname", "")
        or lead.get("status_name", "")
    ).strip().lower()

    return status_name == "confirmed"


def is_inflatable_rental(rental):
    if not isinstance(rental, dict):
        return False

    name = str(rental.get("ridename", "")).lower()
    category = str(rental.get("category_name", "")).lower()

    haystack = f"{category} {name}"

    inflatable_words = (
        "inflatable",
        "bounce",
        "bouncer",
        "moonwalk",
        "water slide",
        "waterslide",
        "dry slide",
        "obstacle",
        "combo",
        "jumper",
        "sports game",
        "interactive",
        "axe throw",
        "soccer darts",
        "basketball",
        "football",
        "baseball",
        "frisbee",
        "tic tac toe",
    )

    non_inflatable_words = (
        "tent",
        "table",
        "chair",
        "generator",
        "concession",
        "popcorn",
        "cotton candy",
        "snow cone",
        "hot dog",
        "photo booth",
        "photobooth",
        "mini golf",
        "karaoke",
        "speaker",
        "lighting",
        "foam",
        "bubble",
    )

    if any(word in haystack for word in non_inflatable_words):
        return False

    return any(word in haystack for word in inflatable_words)


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

    lead_summaries = await io_get_all(
        "leads/",
        {"_body": "false"}
    )

    weekend_lead_ids = []

    for lead in lead_summaries:
        if not isinstance(lead, dict):
            continue

        event_date = parse_io_date(
            lead.get("eventstarttime")
            or lead.get("fullstart")
        )

        if event_date in (saturday, sunday):
            lead_id = lead.get("id")
            if lead_id:
                weekend_lead_ids.append(str(lead_id))

    rental_cache = {}
    totals = Counter()
    confirmed_lead_count = 0

    for lead_id in weekend_lead_ids:
        detail = await io_get(
            f"leads/{lead_id}",
            {"_body": "true"}
        )

        if not is_confirmed_lead(detail):
            continue

        confirmed_lead_count += 1

        selectedrides = detail.get("selectedrides", [])
        rentalqty = detail.get("rentalqty", {})

        if not isinstance(selectedrides, list):
            continue

        if not isinstance(rentalqty, dict):
            rentalqty = {}

        for rental_id in selectedrides:
            rental_id = str(rental_id)

            if rental_id not in rental_cache:
                rental_cache[rental_id] = await io_get(
                    f"rentals/{rental_id}"
                )

            rental = rental_cache[rental_id]

            if not is_inflatable_rental(rental):
                continue

            name = (
                rental.get("ridename")
                or f"Rental {rental_id}"
            )

            try:
                qty = int(
                    float(rentalqty.get(rental_id, 1))
                )
            except Exception:
                qty = 1

            totals[name] += max(qty, 1)

    return {
        "weekend": {
            "saturday": str(saturday),
            "sunday": str(sunday)
        },
        "status": "confirmed only",
        "confirmedLeadCount": confirmed_lead_count,
        "totalInflatables": sum(totals.values()),
        "items": [
            {
                "name": name,
                "quantity": quantity
            }
            for name, quantity in sorted(
                totals.items(),
                key=lambda x: (-x[1], x[0].lower())
            )
        ]
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )
