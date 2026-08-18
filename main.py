import os
import time
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
CONFIRMED_STATUS_ID = os.getenv("CONFIRMED_STATUS_ID", "").strip()

app = FastAPI(title="Callahan InflatableOffice Bridge", version="2.0.0")
security = HTTPBearer()

RENTAL_CACHE_SECONDS = 21600
SUMMARY_CACHE_SECONDS = 300
_rental_cache = {"expires": 0.0, "items": {}}
_summary_cache = {}


def require_io_key():
    if not IO_API_KEY:
        raise HTTPException(status_code=500, detail="INFLATABLE_OFFICE_API_KEY is not configured")


def require_bridge_config():
    require_io_key()
    if not BRIDGE_TOKEN:
        raise HTTPException(status_code=500, detail="BRIDGE_TOKEN is not configured")


def check_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    require_bridge_config()
    if credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


async def io_get(path: str, params: Optional[dict] = None):
    require_io_key()
    params = dict(params or {})
    params["apiKey"] = IO_API_KEY
    url = f"{IO_BASE_URL}/{path.lstrip('/')}"

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.get(url, params=params, headers={"Accept": "application/json"})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"InflatableOffice connection failed: {exc.__class__.__name__}")

    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="InflatableOffice rate limit reached. Wait a few minutes and try again.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"InflatableOffice returned HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="InflatableOffice returned a non-JSON response")


def extract_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results", "data", "leads", "rentals"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


async def io_get_pages(path: str, params: Optional[dict] = None, max_pages: int = 3):
    page_size = 100
    all_items = []
    for page in range(max_pages):
        page_params = dict(params or {})
        page_params["offset"] = page * page_size
        page_params["limit"] = page_size
        data = await io_get(path, page_params)
        items = extract_items(data)
        all_items.extend(items)
        if len(items) < page_size:
            break
    return all_items


def parse_io_date(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def lead_event_date(lead):
    if not isinstance(lead, dict):
        return None
    for key in ("eventstarttime", "fullstart", "eventStart", "event_date"):
        parsed = parse_io_date(lead.get(key))
        if parsed:
            return parsed
    return None


def confirmed_from_record(lead):
    if not isinstance(lead, dict):
        return False

    status = lead.get("status")
    if isinstance(status, dict):
        flag = str(status.get("confirmed", "")).strip().lower()
        name = str(status.get("name", "")).strip().lower()
        if flag in {"1", "true", "yes"}:
            return True
        if flag in {"0", "false", "no"}:
            return False
        if name:
            return name == "confirmed"

    status_name = str(lead.get("statusname", "") or lead.get("status_name", "")).strip().lower()
    if status_name:
        return status_name == "confirmed"

    if CONFIRMED_STATUS_ID:
        status_id = str(lead.get("statusid", "") or lead.get("status_id", "")).strip()
        if status_id:
            return status_id == CONFIRMED_STATUS_ID

    return None


def selected_rentals_from_record(lead):
    if not isinstance(lead, dict):
        return [], {}
    selected = lead.get("selectedrides", [])
    qty = lead.get("rentalqty", {})
    if isinstance(selected, str):
        selected = [] if not selected.strip() else [selected.strip()]
    if not isinstance(selected, list):
        selected = []
    if not isinstance(qty, dict):
        qty = {}
    return [str(x) for x in selected], qty


async def get_confirmed_lead_details(lead):
    known_status = confirmed_from_record(lead)
    selected, _ = selected_rentals_from_record(lead)

    if known_status is not None and selected:
        return lead if known_status else None

    lead_id = lead.get("id") if isinstance(lead, dict) else None
    if not lead_id:
        return None

    detail = await io_get(f"leads/{lead_id}", {"_body": "true"})
    return detail if confirmed_from_record(detail) is True else None


async def get_rental_lookup():
    now = time.time()
    if now < _rental_cache["expires"] and _rental_cache["items"]:
        return _rental_cache["items"]

    rentals = await io_get_pages("rentals", {"_body": "true"}, max_pages=5)
    lookup = {}

    for rental in rentals:
        if not isinstance(rental, dict):
            continue
        rid = rental.get("id") or rental.get("rentalid") or rental.get("rideid")
        if rid is not None:
            lookup[str(rid)] = rental

    _rental_cache["items"] = lookup
    _rental_cache["expires"] = now + RENTAL_CACHE_SECONDS
    return lookup


def rental_name(rental, rental_id):
    if not isinstance(rental, dict):
        return f"Rental {rental_id}"
    return str(rental.get("ridename") or rental.get("name") or rental.get("title") or f"Rental {rental_id}")


def is_inflatable(rental):
    if not isinstance(rental, dict):
        return False

    wanted_categories = {
        "bounce house slide combos",
        "bounce houses",
        "obstacle courses",
        "water slides",
        "games",
    }

    category_values = []

    for key in ("category_name", "category", "categories"):
        value = rental.get(key)

        if isinstance(value, str):
            category_values.append(value)

        elif isinstance(value, dict):
            name = value.get("name")
            if name:
                category_values.append(name)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    category_values.append(item)
                elif isinstance(item, dict):
                    name = item.get("name")
                    if name:
                        category_values.append(name)

    normalized = {
        str(category).strip().lower()
        for category in category_values
    }

    return bool(normalized & wanted_categories)


def parse_requested_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")


async def build_item_summary(start_date, end_date):
    cache_key = f"{start_date}:{end_date}"
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    date_filter = f"{start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')}"
    params = {"_body": "true", "date": date_filter}

    if CONFIRMED_STATUS_ID:
        params["status[]"] = CONFIRMED_STATUS_ID

    lead_rows = await io_get_pages("leads/", params, max_pages=3)

    date_filtered = []
    for lead in lead_rows:
        event_date = lead_event_date(lead)
        if event_date is None or start_date <= event_date <= end_date:
            date_filtered.append(lead)

    confirmed_leads = []
    for lead in date_filtered:
        detail = await get_confirmed_lead_details(lead)
        if not detail:
            continue
        event_date = lead_event_date(detail)
        if event_date and not (start_date <= event_date <= end_date):
            continue
        confirmed_leads.append(detail)

    rental_lookup = await get_rental_lookup()
    totals = Counter()

    for lead in confirmed_leads:
        selected, qty_map = selected_rentals_from_record(lead)
        for rental_id in selected:
            rental = rental_lookup.get(str(rental_id), {})
            if not is_inflatable(rental):
                continue
            try:
                quantity = int(float(qty_map.get(str(rental_id), 1)))
            except Exception:
                quantity = 1
            totals[rental_name(rental, rental_id)] += max(quantity, 1)

    result = {
        "dateRange": {"start": str(start_date), "end": str(end_date)},
        "status": "confirmed only",
        "confirmedLeadCount": len(confirmed_leads),
        "totalInflatables": sum(totals.values()),
        "items": [
            {"name": name, "quantity": quantity}
            for name, quantity in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0].lower()))
        ],
        "cache": "miss"
    }

    _summary_cache[cache_key] = {"expires": now + SUMMARY_CACHE_SECONDS, "data": result}
    return result


@app.get("/")
async def root():
    return {"service": "Callahan InflatableOffice Bridge", "status": "ok", "mode": "read-only", "version": "2.0.0"}


@app.get("/health")
async def health(_: bool = Depends(check_token)):
    data = await io_get("rentals", {"limit": 1})
    return {"bridge": "ok", "inflatableOffice": "ok", "rentalCacheLoaded": bool(_rental_cache["items"])}


@app.get("/leads")
async def leads(
    _: bool = Depends(check_token),
    filter: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
    body: bool = Query(default=True),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    params = {"_body": "true" if body else "false", "offset": offset, "limit": limit}
    if filter:
        params["filter"] = filter
    if date:
        params["date"] = date
    return await io_get("leads/", params)


@app.get("/leads/{lead_id}")
async def lead_detail(
    lead_id: int,
    _: bool = Depends(check_token),
    body: bool = Query(default=True),
):
    return await io_get(f"leads/{lead_id}", {"_body": "true" if body else "false"})


@app.get("/rentals")
async def rentals(
    _: bool = Depends(check_token),
    body: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
):
    return await io_get("rentals", {"_body": "true" if body else "false", "offset": offset, "limit": limit})


@app.get("/public/weekend-items")
async def public_weekend_items():
    today = datetime.now().date()
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)
    return await build_item_summary(saturday, sunday)


@app.get("/public/day-items")
async def public_day_items(date: str = Query(..., description="YYYY-MM-DD")):
    requested = parse_requested_date(date)
    return await build_item_summary(requested, requested)


@app.get("/public/range-items")
async def public_range_items(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    start_date = parse_requested_date(start)
    end_date = parse_requested_date(end)
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if (end_date - start_date).days > 31:
        raise HTTPException(status_code=400, detail="Public range is limited to 31 days")
    return await build_item_summary(start_date, end_date)


@app.post("/admin/clear-cache")
async def clear_cache(_: bool = Depends(check_token)):
    _rental_cache["expires"] = 0.0
    _rental_cache["items"] = {}
    _summary_cache.clear()
    return {"status": "cache cleared"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
