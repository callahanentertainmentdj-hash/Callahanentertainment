import os
import time
from typing import Optional
from datetime import datetime, timedelta
from collections import Counter, defaultdict

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

load_dotenv()

IO_API_KEY = os.getenv("INFLATABLE_OFFICE_API_KEY", "").strip()
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()
IO_BASE_URL = os.getenv("IO_BASE_URL", "https://rental.software/api6").rstrip("/")
CONFIRMED_STATUS_ID = os.getenv("CONFIRMED_STATUS_ID", "226783").strip()

app = FastAPI(
    title="Callahan InflatableOffice Bridge",
    version="3.1.0"
)

security = HTTPBearer()

SUMMARY_CACHE_SECONDS = 300
_summary_cache = {}


# ============================================================
# CALLAHAN CONFIGURATION
# ============================================================

# Physical categories that matter for warehouse loading.
CATEGORY_LABELS = {
    "chairs": "Chairs",
    "tables": "Tables",
    "tents": "Tents",
    "inflatables_games": "Inflatables & Games",
    "concessions": "Concessions",
    "mini_golf": "Mini Golf",
    "foam": "Foam",
    "bubbles": "Bubbles",
    "photo_booth": "Photo Booth",
    "karaoke": "Karaoke",
    "audio": "Audio / DJ",
    "services": "Services / Non-Physical",
    "packages": "Packages",
    "other": "Other / Review",
}

# These are sales/service lines and should NOT count as physical chairs/tables/etc.
SERVICE_TERMS = (
    "setup and break down",
    "setup & break down",
    "set up and break down",
    "set up & break down",
    "delivery fee",
    "delivery charge",
    "distance charge",
    "distance charges",
    "staff cost",
    "staff costs",
    "attendant",
    "damage waiver",
    "discount",
    "labor",
    "installation fee",
)

# Names/terms that identify Callahan's inflatable and inflatable-game inventory.
# This catches names that do not literally contain "slide" or "combo".
INFLATABLE_TERMS = (
    "bounce house",
    "bouncer",
    "combo",
    "water slide",
    "waterslide",
    "dry slide",
    "obstacle",
    "inflatable",
    "moonwalk",
    "jumper",
    "dual lane",
    "purple marble",
    "red marble",
    "tropical inferno",
    "surf beach",
    "jungle falls",
    "fireblast",
    "tsunami",
    "liquid hot magma",
    "rocky marbles",
    "melting ice",
    "castle tower",
    "radical run",
    "soccer darts",
    "axe throw",
    "football game",
    "basketball game",
    "baseball game",
    "frisbee game",
    "tic tac toe",
    "toilet bowl",
    "balloon pop",
    "mega wire",
    "high striker",
)

# Known package mappings.
# Add package names here as we learn them. Component quantities are PER package.
# The package itself remains visible in "packages", while these physical components
# are added to warehouse totals.
PACKAGE_COMPONENTS = {
    "backyard deluxe package combo/slidehouse": {
        "20 X 20 Pole Tent": 1,
        "6 Ft Folding Table Grey": 6,
        "White Folding Chair": 24,
        # Generic label because the package lets the customer select a combo/slide.
        # If IO exposes the chosen option separately, it will already appear as its
        # own rental line and should not be duplicated here.
    },
    "tables and chairs small party package": {
        "6 Ft Folding Table Grey": 2,
        "White Folding Chair": 16,
    },
}

# Packages we know exist but whose internal quantities still need to be defined.
KNOWN_PACKAGE_TERMS = (
    "package",
    "bundle",
)


# ============================================================
# SECURITY + IO HELPERS
# ============================================================

def require_io_key():
    if not IO_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="INFLATABLE_OFFICE_API_KEY is not configured"
        )


def require_bridge_config():
    require_io_key()
    if not BRIDGE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="BRIDGE_TOKEN is not configured"
        )


def check_token(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
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
            detail="InflatableOffice rate limit reached. Wait a few minutes and try again."
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


def extract_items(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("items", "results", "data", "leads", "rentals"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


async def io_get_pages(
    path: str,
    params: Optional[dict] = None,
    max_pages: int = 3
):
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


# ============================================================
# LEAD / DATE / STATUS HELPERS
# ============================================================

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


def lead_event_date(lead):
    if not isinstance(lead, dict):
        return None

    for key in (
        "eventstarttime",
        "fullstart",
        "eventStart",
        "event_date",
    ):
        parsed = parse_io_date(lead.get(key))
        if parsed:
            return parsed

    return None


def confirmed_from_record(lead):
    if not isinstance(lead, dict):
        return False

    # Fastest and most reliable check for this account.
    status_id = str(
        lead.get("statusid", "")
        or lead.get("status_id", "")
    ).strip()

    if CONFIRMED_STATUS_ID and status_id:
        return status_id == CONFIRMED_STATUS_ID

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

    status_name = str(
        lead.get("statusname", "")
        or lead.get("status_name", "")
    ).strip().lower()

    if status_name:
        return status_name == "confirmed"

    return None


def parse_requested_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Date must be YYYY-MM-DD"
        )


# ============================================================
# RENTAL / LOADOUT HELPERS
# ============================================================

def normalize_name(value):
    return " ".join(str(value or "").strip().lower().split())


def rental_name_from_object(rental, rental_id):
    if isinstance(rental, dict):
        return str(
            rental.get("ridename")
            or rental.get("name")
            or rental.get("title")
            or f"Rental {rental_id}"
        )

    return f"Rental {rental_id}"


def extract_rentals_from_lead(lead):
    """
    Reads selectedrides + rentalqty + embedded rentals from a _body=true lead.
    NO separate rental API calls are required.
    """
    if not isinstance(lead, dict):
        return []

    selected = lead.get("selectedrides", [])
    qty_map = lead.get("rentalqty", {})
    rentals_obj = lead.get("rentals", {})

    if isinstance(selected, str):
        selected = [] if not selected.strip() else [selected.strip()]

    if not isinstance(selected, list):
        selected = []

    if not isinstance(qty_map, dict):
        qty_map = {}

    if not isinstance(rentals_obj, dict):
        rentals_obj = {}

    rows = []

    for rental_id in selected:
        rental_id = str(rental_id)
        rental = rentals_obj.get(rental_id, {})

        try:
            qty = int(float(qty_map.get(rental_id, 1)))
        except Exception:
            qty = 1

        rows.append({
            "rental_id": rental_id,
            "name": rental_name_from_object(rental, rental_id),
            "quantity": max(qty, 1),
            "rental": rental if isinstance(rental, dict) else {},
        })

    return rows


def classify_item(name):
    n = normalize_name(name)

    # Services must come before chair/table matching.
    if any(term in n for term in SERVICE_TERMS):
        return "services"

    # Packages are kept separate.
    if any(term in n for term in KNOWN_PACKAGE_TERMS):
        return "packages"

    if "chair" in n:
        return "chairs"

    if "table" in n:
        return "tables"

    if "tent" in n or "canopy" in n:
        return "tents"

    if any(term in n for term in INFLATABLE_TERMS):
        return "inflatables_games"

    if any(term in n for term in (
        "popcorn",
        "cotton candy",
        "snow cone",
        "hot dog",
    )):
        return "concessions"

    if "mini golf" in n or "9 hole" in n or "golf course" in n:
        return "mini_golf"

    if "foam" in n:
        return "foam"

    if "bubble" in n:
        return "bubbles"

    if "photo booth" in n or "photobooth" in n:
        return "photo_booth"

    if "karaoke" in n:
        return "karaoke"

    if any(term in n for term in ("speaker", "dj ", "audio")):
        return "audio"

    return "other"


def expand_package(package_name, package_qty):
    mapping = PACKAGE_COMPONENTS.get(normalize_name(package_name))

    if not mapping:
        return {}

    expanded = {}

    for component_name, component_qty in mapping.items():
        expanded[component_name] = component_qty * package_qty

    return expanded


def add_item_to_counters(
    name,
    quantity,
    category_totals,
    category_items,
    all_items
):
    category = classify_item(name)

    category_totals[category] += quantity
    category_items[category][name] += quantity
    all_items[name] += quantity

    return category


# ============================================================
# INFLATABLE TURNOVER / CLEANING HELPERS
# ============================================================

def is_inflatable_category_name(name):
    return classify_item(name) == "inflatables_games"


def rental_owned_quantity(rental):
    if not isinstance(rental, dict):
        return None

    value = rental.get("quantity")

    try:
        return int(float(value))
    except Exception:
        return None


def collect_inflatable_usage(leads):
    """
    Returns usage keyed by rental ID.

    Each record contains:
      name
      ownedQuantity
      dates -> booked quantity by event date
    """
    usage = {}

    for lead in leads:
        event_date = lead_event_date(lead)
        if not event_date:
            continue

        for row in extract_rentals_from_lead(lead):
            name = row["name"]

            if not is_inflatable_category_name(name):
                continue

            rid = row["rental_id"]
            qty = row["quantity"]
            rental = row.get("rental", {})

            if rid not in usage:
                usage[rid] = {
                    "rentalId": rid,
                    "name": name,
                    "ownedQuantity": rental_owned_quantity(rental),
                    "dates": Counter(),
                }

            usage[rid]["dates"][str(event_date)] += qty

    return usage


async def fetch_future_confirmed_leads(start_date, end_date):
    """
    Fetch confirmed future leads with embedded rental data.
    Kept to a bounded date range to protect the IO API limit.
    """
    return await fetch_confirmed_leads(start_date, end_date)


async def build_cleaning_plan(start_date, end_date, lookahead_days=60):
    """
    Cleaning/turnover plan:
      - inflatables used on multiple weekend days
      - definite vs possible turnover
      - next confirmed use after the weekend
    """
    cache_key = f"cleaning:{start_date}:{end_date}:{lookahead_days}"
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    weekend_leads = await fetch_confirmed_leads(start_date, end_date)
    weekend_usage = collect_inflatable_usage(weekend_leads)

    future_start = end_date + timedelta(days=1)
    future_end = end_date + timedelta(days=lookahead_days)

    future_leads = await fetch_future_confirmed_leads(
        future_start,
        future_end
    )

    future_usage = collect_inflatable_usage(future_leads)

    repeat_this_weekend = []
    next_use = []

    for rid, record in weekend_usage.items():
        dates = sorted(record["dates"].keys())
        owned_qty = record["ownedQuantity"]

        if len(dates) > 1:
            max_daily_qty = max(record["dates"].values())

            # If IO says only one physical unit is owned, multi-day use is
            # definitely the same unit turning around between dates.
            if owned_qty == 1:
                turnover_type = "definite"
            else:
                turnover_type = "possible"

            repeat_this_weekend.append({
                "rentalId": rid,
                "name": record["name"],
                "ownedQuantity": owned_qty,
                "turnover": turnover_type,
                "dates": [
                    {
                        "date": date_key,
                        "quantity": record["dates"][date_key]
                    }
                    for date_key in dates
                ],
                "maxBookedSameDay": max_daily_qty
            })

        future = future_usage.get(rid)

        if future and future["dates"]:
            first_future_date = sorted(future["dates"].keys())[0]
            next_date = first_future_date
            next_qty = future["dates"][first_future_date]
        else:
            next_date = None
            next_qty = 0

        next_use.append({
            "rentalId": rid,
            "name": record["name"],
            "lastWeekendUse": max(dates) if dates else None,
            "nextConfirmedUse": next_date,
            "nextQuantity": next_qty,
            "lookaheadDays": lookahead_days,
        })

    repeat_this_weekend.sort(
        key=lambda x: (
            0 if x["turnover"] == "definite" else 1,
            x["name"].lower()
        )
    )

    next_use.sort(
        key=lambda x: (
            x["nextConfirmedUse"] is None,
            x["nextConfirmedUse"] or "9999-12-31",
            x["name"].lower()
        )
    )

    result = {
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date)
        },
        "status": "confirmed only",
        "weekendConfirmedLeadCount": len(weekend_leads),
        "repeatInflatablesThisWeekend": repeat_this_weekend,
        "nextUseAfterWeekend": next_use,
        "notes": {
            "definiteTurnover": (
                "Inflatable is booked on multiple weekend dates and "
                "InflatableOffice reports only one unit owned."
            ),
            "possibleTurnover": (
                "Inflatable is booked on multiple weekend dates, but "
                "InflatableOffice reports multiple units or no owned quantity. "
                "A specific physical unit assignment cannot be proven from this data."
            )
        },
        "cache": "miss"
    }

    _summary_cache[cache_key] = {
        "expires": now + SUMMARY_CACHE_SECONDS,
        "data": result
    }

    return result


async def build_inflatable_schedule(search_text, start_date, end_date):
    """
    Search confirmed inflatable usage by name over a date range.
    Useful for: 'When is Melting Ice going out next?'
    """
    cache_key = (
        f"inflatableschedule:{normalize_name(search_text)}:"
        f"{start_date}:{end_date}"
    )
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    leads = await fetch_confirmed_leads(start_date, end_date)

    needle = normalize_name(search_text)
    matches = defaultdict(Counter)
    names = {}

    for lead in leads:
        event_date = lead_event_date(lead)
        if not event_date:
            continue

        for row in extract_rentals_from_lead(lead):
            name = row["name"]

            if not is_inflatable_category_name(name):
                continue

            if needle not in normalize_name(name):
                continue

            rid = row["rental_id"]
            names[rid] = name
            matches[rid][str(event_date)] += row["quantity"]

    results = []

    for rid, date_counts in matches.items():
        dates = sorted(date_counts.keys())

        results.append({
            "rentalId": rid,
            "name": names[rid],
            "nextConfirmedUse": dates[0] if dates else None,
            "bookings": [
                {
                    "date": date_key,
                    "quantity": date_counts[date_key]
                }
                for date_key in dates
            ]
        })

    results.sort(
        key=lambda x: (
            x["nextConfirmedUse"] or "9999-12-31",
            x["name"].lower()
        )
    )

    result = {
        "search": search_text,
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date)
        },
        "status": "confirmed only",
        "matches": results,
        "cache": "miss"
    }

    _summary_cache[cache_key] = {
        "expires": now + SUMMARY_CACHE_SECONDS,
        "data": result
    }

    return result


# ============================================================
# CORE REPORT BUILDER
# ============================================================

async def fetch_confirmed_leads(start_date, end_date):
    """
    One filtered lead-list request in the normal case.
    _body=true gives us status + selectedrides + rentalqty + embedded rentals.
    """
    date_filter = (
        f"{start_date.strftime('%Y-%m-%d')} - "
        f"{end_date.strftime('%Y-%m-%d')}"
    )

    params = {
        "_body": "true",
        "date": date_filter,
        "status[]": CONFIRMED_STATUS_ID,
    }

    rows = await io_get_pages(
        "leads/",
        params,
        max_pages=3
    )

    confirmed = []

    for lead in rows:
        if not isinstance(lead, dict):
            continue

        event_date = lead_event_date(lead)

        if event_date and not (start_date <= event_date <= end_date):
            continue

        if confirmed_from_record(lead) is True:
            confirmed.append(lead)

    return confirmed


async def build_loadout(start_date, end_date):
    cache_key = f"loadout:{start_date}:{end_date}"
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    leads = await fetch_confirmed_leads(start_date, end_date)

    category_totals = Counter()
    category_items = defaultdict(Counter)
    all_items = Counter()

    # Physical totals include package-expanded components.
    physical_totals = Counter()
    physical_items = defaultdict(Counter)

    # Keep the original booked items too.
    packages = Counter()
    unresolved_packages = Counter()
    services = Counter()

    # Daily summaries, no additional IO calls.
    daily = {}

    current = start_date
    while current <= end_date:
        daily[str(current)] = {
            "confirmedLeadCount": 0,
            "physicalTotals": {
                "chairs": 0,
                "tables": 0,
                "tents": 0,
                "inflatablesGames": 0,
            },
            "items": []
        }
        current += timedelta(days=1)

    per_day_items = defaultdict(Counter)
    per_day_physical = defaultdict(Counter)

    for lead in leads:
        event_date = lead_event_date(lead)
        date_key = str(event_date) if event_date else None

        if date_key in daily:
            daily[date_key]["confirmedLeadCount"] += 1

        for row in extract_rentals_from_lead(lead):
            name = row["name"]
            qty = row["quantity"]

            category = add_item_to_counters(
                name,
                qty,
                category_totals,
                category_items,
                all_items
            )

            if date_key:
                per_day_items[date_key][name] += qty

            if category == "services":
                services[name] += qty
                continue

            if category == "packages":
                packages[name] += qty

                expanded = expand_package(name, qty)

                if not expanded:
                    unresolved_packages[name] += qty
                    continue

                for component_name, component_qty in expanded.items():
                    comp_category = classify_item(component_name)

                    physical_totals[comp_category] += component_qty
                    physical_items[comp_category][component_name] += component_qty

                    if date_key:
                        per_day_physical[date_key][comp_category] += component_qty

                continue

            # Non-package physical items count directly.
            if category not in {"other"}:
                physical_totals[category] += qty
                physical_items[category][name] += qty

                if date_key:
                    per_day_physical[date_key][category] += qty

    # Build daily output.
    for date_key in daily:
        daily[date_key]["physicalTotals"] = {
            "chairs": per_day_physical[date_key].get("chairs", 0),
            "tables": per_day_physical[date_key].get("tables", 0),
            "tents": per_day_physical[date_key].get("tents", 0),
            "inflatablesGames": per_day_physical[date_key].get("inflatables_games", 0),
            "concessions": per_day_physical[date_key].get("concessions", 0),
            "miniGolf": per_day_physical[date_key].get("mini_golf", 0),
            "foam": per_day_physical[date_key].get("foam", 0),
            "bubbles": per_day_physical[date_key].get("bubbles", 0),
        }

        daily[date_key]["items"] = [
            {"name": name, "quantity": qty}
            for name, qty in sorted(
                per_day_items[date_key].items(),
                key=lambda pair: (-pair[1], pair[0].lower())
            )
        ]

    # Warehouse-focused categories.
    warehouse_groups = {}

    warehouse_order = (
        "chairs",
        "tables",
        "tents",
        "inflatables_games",
        "concessions",
        "mini_golf",
        "foam",
        "bubbles",
        "photo_booth",
        "karaoke",
        "audio",
    )

    for category in warehouse_order:
        items = physical_items.get(category, Counter())

        warehouse_groups[category] = {
            "label": CATEGORY_LABELS.get(category, category),
            "total": physical_totals.get(category, 0),
            "items": [
                {"name": name, "quantity": qty}
                for name, qty in sorted(
                    items.items(),
                    key=lambda pair: (-pair[1], pair[0].lower())
                )
            ]
        }

    result = {
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date)
        },
        "status": "confirmed only",
        "confirmedLeadCount": len(leads),

        # This is the operational "what do we actually load?" summary.
        "warehouseTotals": {
            "chairs": physical_totals.get("chairs", 0),
            "tables": physical_totals.get("tables", 0),
            "tents": physical_totals.get("tents", 0),
            "inflatablesGames": physical_totals.get("inflatables_games", 0),
            "concessions": physical_totals.get("concessions", 0),
            "miniGolf": physical_totals.get("mini_golf", 0),
            "foam": physical_totals.get("foam", 0),
            "bubbles": physical_totals.get("bubbles", 0),
            "photoBooth": physical_totals.get("photo_booth", 0),
            "karaoke": physical_totals.get("karaoke", 0),
            "audio": physical_totals.get("audio", 0),
        },

        "warehouseGroups": warehouse_groups,

        # Packages remain visible so we can audit what was expanded.
        "packages": {
            "booked": [
                {"name": name, "quantity": qty}
                for name, qty in sorted(
                    packages.items(),
                    key=lambda pair: (-pair[1], pair[0].lower())
                )
            ],
            "unresolved": [
                {
                    "name": name,
                    "quantity": qty,
                    "note": "Package component mapping has not been configured yet."
                }
                for name, qty in sorted(
                    unresolved_packages.items(),
                    key=lambda pair: (-pair[1], pair[0].lower())
                )
            ]
        },

        # Service lines are deliberately excluded from physical warehouse counts.
        "servicesExcludedFromLoadout": [
            {"name": name, "quantity": qty}
            for name, qty in sorted(
                services.items(),
                key=lambda pair: (-pair[1], pair[0].lower())
            )
        ],

        # Anything here needs classification/mapping attention.
        "reviewItems": [
            {"name": name, "quantity": qty}
            for name, qty in sorted(
                category_items.get("other", Counter()).items(),
                key=lambda pair: (-pair[1], pair[0].lower())
            )
        ],

        "byDay": daily,

        # Raw booked item summary for auditing.
        "allBookedItems": [
            {"name": name, "quantity": qty}
            for name, qty in sorted(
                all_items.items(),
                key=lambda pair: (-pair[1], pair[0].lower())
            )
        ],

        "cache": "miss"
    }

    _summary_cache[cache_key] = {
        "expires": now + SUMMARY_CACHE_SECONDS,
        "data": result
    }

    return result


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "Callahan InflatableOffice Bridge",
        "status": "ok",
        "mode": "read-only",
        "version": "3.1.0"
    }


@app.get("/health")
async def health(_: bool = Depends(check_token)):
    data = await io_get("leads/", {
        "limit": 1,
        "_body": "false",
        "status[]": CONFIRMED_STATUS_ID
    })

    return {
        "bridge": "ok",
        "inflatableOffice": "ok",
        "confirmedStatusId": CONFIRMED_STATUS_ID,
        "sampleReceived": bool(extract_items(data))
    }


@app.get("/leads")
async def leads(
    _: bool = Depends(check_token),
    filter: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
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

    if date:
        params["date"] = date

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


@app.get("/public/weekend-loadout")
async def public_weekend_loadout():
    """
    Callahan weekend = Friday through Sunday.
    """
    today = datetime.now().date()
    days_until_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)

    return await build_loadout(friday, sunday)


@app.get("/public/day-loadout")
async def public_day_loadout(
    date: str = Query(..., description="YYYY-MM-DD")
):
    requested = parse_requested_date(date)
    return await build_loadout(requested, requested)


@app.get("/public/range-loadout")
async def public_range_loadout(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    start_date = parse_requested_date(start)
    end_date = parse_requested_date(end)

    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be on or after start date"
        )

    if (end_date - start_date).days > 31:
        raise HTTPException(
            status_code=400,
            detail="Public range is limited to 31 days"
        )

    return await build_loadout(start_date, end_date)


@app.get("/public/weekend-cleaning")
async def public_weekend_cleaning(
    lookahead_days: int = Query(
        default=60,
        ge=7,
        le=180,
        description="How many days after the weekend to search for next use"
    )
):
    """
    Friday-Sunday turnover/cleaning report plus next confirmed use.
    """
    today = datetime.now().date()
    days_until_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)

    return await build_cleaning_plan(
        friday,
        sunday,
        lookahead_days=lookahead_days
    )


@app.get("/public/inflatable-next-use")
async def public_inflatable_next_use(
    name: str = Query(..., min_length=2, description="Full or partial inflatable name"),
    days: int = Query(default=90, ge=1, le=365)
):
    """
    Search upcoming confirmed uses of a specific inflatable name.
    Example: ?name=Melting%20Ice&days=90
    """
    start_date = datetime.now().date()
    end_date = start_date + timedelta(days=days)

    return await build_inflatable_schedule(
        name,
        start_date,
        end_date
    )


@app.post("/admin/clear-cache")
async def clear_cache(_: bool = Depends(check_token)):
    _summary_cache.clear()
    return {"status": "cache cleared"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )
