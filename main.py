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
QUOTE_STATUS_ID = os.getenv("QUOTE_STATUS_ID", "").strip()
CONTRACTED_STATUS_ID = os.getenv("CONTRACTED_STATUS_ID", "").strip()
COMPLETE_STATUS_ID = os.getenv("COMPLETE_STATUS_ID", "").strip()

STATUS_IDS = {
    "confirmed": CONFIRMED_STATUS_ID,
    "quote": QUOTE_STATUS_ID,
    "contracted": CONTRACTED_STATUS_ID,
    "complete": COMPLETE_STATUS_ID,
}

try:
    LARGE_EVENT_THRESHOLD = float(
        os.getenv("LARGE_EVENT_THRESHOLD", "1000")
    )
except ValueError:
    LARGE_EVENT_THRESHOLD = 1000.0

app = FastAPI(
    title="Callahan InflatableOffice Bridge",
    version="3.5.0",
    servers=[{"url": "https://callahanentertainment.onrender.com"}],
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


def normalize_status_name(value):
    text = str(value or "").strip().lower()
    aliases = {
        "quotes": "quote",
        "quoted": "quote",
        "contract": "contracted",
        "contracts": "contracted",
        "completed": "complete",
        "completion": "complete",
        "confirmed": "confirmed",
    }
    return aliases.get(text, text)


def requested_statuses(value):
    parts = [normalize_status_name(x) for x in str(value or "").split(",")]
    parts = [x for x in parts if x]
    allowed = {"confirmed", "quote", "contracted", "complete"}
    invalid = [x for x in parts if x not in allowed]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported status: " + ", ".join(invalid) +
                ". Use confirmed, quote, contracted, or complete."
            )
        )
    return list(dict.fromkeys(parts or ["confirmed"]))


def status_matches_record(lead, requested):
    if not isinstance(lead, dict):
        return False

    requested = normalize_status_name(requested)
    status = lead.get("status")
    status_id = str(lead.get("statusid", "") or lead.get("status_id", "")).strip()
    configured_id = STATUS_IDS.get(requested, "")

    if configured_id and status_id:
        return status_id == configured_id

    if isinstance(status, dict):
        name = normalize_status_name(status.get("name", ""))
        flag_map = {
            "confirmed": "confirmed",
            "quote": "newquote",
            "contracted": "contract",
            "complete": "complete",
        }
        flag = str(status.get(flag_map.get(requested, ""), "")).strip().lower()
        if flag in {"1", "true", "yes"}:
            return True
        if name == requested:
            return True
        if requested == "contracted" and name == "contract":
            return True
        if requested == "quote" and name in {"quoted", "new quote"}:
            return True
        if requested == "complete" and name == "completed":
            return True

    name = normalize_status_name(lead.get("statusname", "") or lead.get("status_name", ""))
    return name == requested


def lead_status_name(lead):
    status = lead.get("status") if isinstance(lead, dict) else None
    if isinstance(status, dict) and status.get("name"):
        return str(status.get("name"))
    return str(lead.get("statusname", "") or lead.get("status_name", "")) if isinstance(lead, dict) else ""


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


def cleaning_priority(last_use_date, next_use_date):
    """
    Priority rules:
      URGENT  = same-day / next-day turnaround
      HIGH    = next use within 3 days
      SOON    = next use within 7 days
      NORMAL  = next use within 14 days
      LOW     = next use more than 14 days away
      NO RUSH = no confirmed use in the lookahead window
    """
    if not last_use_date:
        return {
            "priority": "NO RUSH",
            "daysUntilNextUse": None,
            "reason": "No last-use date available."
        }

    if not next_use_date:
        return {
            "priority": "NO RUSH",
            "daysUntilNextUse": None,
            "reason": "No confirmed future use in the lookahead window."
        }

    try:
        last_date = datetime.strptime(last_use_date, "%Y-%m-%d").date()
        next_date = datetime.strptime(next_use_date, "%Y-%m-%d").date()
    except ValueError:
        return {
            "priority": "NO RUSH",
            "daysUntilNextUse": None,
            "reason": "Unable to calculate dates."
        }

    gap = (next_date - last_date).days

    if gap <= 1:
        priority = "URGENT"
        reason = "Same-day or next-day turnaround."
    elif gap <= 3:
        priority = "HIGH"
        reason = f"Next confirmed use is in {gap} days."
    elif gap <= 7:
        priority = "SOON"
        reason = f"Next confirmed use is in {gap} days."
    elif gap <= 14:
        priority = "NORMAL"
        reason = f"Next confirmed use is in {gap} days."
    else:
        priority = "LOW"
        reason = f"Next confirmed use is in {gap} days."

    return {
        "priority": priority,
        "daysUntilNextUse": gap,
        "reason": reason
    }


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
                "priority": "URGENT",
                "priorityReason": "Booked on multiple days within the same weekend.",
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

        last_weekend_use = max(dates) if dates else None
        priority_info = cleaning_priority(
            last_weekend_use,
            next_date
        )

        next_use.append({
            "rentalId": rid,
            "name": record["name"],
            "lastWeekendUse": last_weekend_use,
            "nextConfirmedUse": next_date,
            "nextQuantity": next_qty,
            "lookaheadDays": lookahead_days,
            "cleaningPriority": priority_info["priority"],
            "daysUntilNextUse": priority_info["daysUntilNextUse"],
            "priorityReason": priority_info["reason"],
        })

    repeat_this_weekend.sort(
        key=lambda x: (
            0 if x["turnover"] == "definite" else 1,
            x["name"].lower()
        )
    )

    priority_order = {
        "URGENT": 0,
        "HIGH": 1,
        "SOON": 2,
        "NORMAL": 3,
        "LOW": 4,
        "NO RUSH": 5,
    }

    next_use.sort(
        key=lambda x: (
            priority_order.get(x["cleaningPriority"], 99),
            x["nextConfirmedUse"] is None,
            x["nextConfirmedUse"] or "9999-12-31",
            x["name"].lower()
        )
    )

    priority_counts = Counter(
        item["cleaningPriority"]
        for item in next_use
    )

    result = {
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date)
        },
        "status": "confirmed only",
        "weekendConfirmedLeadCount": len(weekend_leads),
        "cleaningPrioritySummary": {
            "URGENT": priority_counts.get("URGENT", 0) + len(repeat_this_weekend),
            "HIGH": priority_counts.get("HIGH", 0),
            "SOON": priority_counts.get("SOON", 0),
            "NORMAL": priority_counts.get("NORMAL", 0),
            "LOW": priority_counts.get("LOW", 0),
            "NO RUSH": priority_counts.get("NO RUSH", 0),
        },
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
            ),
            "priorityLegend": {
                "URGENT": "Same-weekend turnover or next use within 1 day.",
                "HIGH": "Next confirmed use within 3 days.",
                "SOON": "Next confirmed use within 7 days.",
                "NORMAL": "Next confirmed use within 14 days.",
                "LOW": "Next confirmed use more than 14 days away.",
                "NO RUSH": "No confirmed future use in the selected lookahead window."
            }
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

async def fetch_leads_by_status(start_date, end_date, statuses):
    """Fetch leads by one or more business statuses.

    If a status ID is configured, IO filters server-side. Otherwise the bridge
    fetches the bounded date range once with _body=true and matches the embedded
    status name/flags. This lets Quote/Contracted/Complete work before their
    account-specific IDs are configured.
    """
    statuses = requested_statuses(",".join(statuses) if isinstance(statuses, (list, tuple)) else statuses)
    date_filter = f"{start_date:%Y-%m-%d} - {end_date:%Y-%m-%d}"

    configured = [STATUS_IDS.get(x, "") for x in statuses]
    can_filter_server_side = all(configured)

    rows = []
    if can_filter_server_side:
        # Separate calls are deliberate: IO's repeated status[] handling varies
        # by account/API gateway, while each call remains small and predictable.
        seen = set()
        for status_name in statuses:
            params = {
                "_body": "true",
                "date": date_filter,
                "status[]": STATUS_IDS[status_name],
            }
            for lead in await io_get_pages("leads/", params, max_pages=6):
                lead_id = str(lead.get("id", "")) if isinstance(lead, dict) else ""
                key = lead_id or id(lead)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(lead)
    else:
        rows = await io_get_pages(
            "leads/",
            {"_body": "true", "date": date_filter},
            max_pages=6,
        )

    matched = []
    for lead in rows:
        if not isinstance(lead, dict):
            continue
        event_date = lead_event_date(lead)
        if event_date and not (start_date <= event_date <= end_date):
            continue
        if any(status_matches_record(lead, x) for x in statuses):
            matched.append(lead)

    return matched


async def fetch_confirmed_leads(start_date, end_date):
    return await fetch_leads_by_status(start_date, end_date, ["confirmed"])


def rental_catalog_id(rental):
    """Normalize the different identifier fields returned by IO rentals."""
    for key in ("id", "rentalid", "rental_id", "rideid", "ride_id"):
        value = rental.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def rental_is_active(rental):
    for key in ("active", "enabled", "isactive", "is_active"):
        if key in rental:
            return str(rental[key]).strip().lower() not in {"0", "false", "no", "off"}
    for key in ("inactive", "disabled", "deleted", "archived"):
        if key in rental:
            return str(rental[key]).strip().lower() in {"0", "false", "no", "off", ""}
    return True


def inventory_category_matches(item, requested):
    if not requested:
        return True
    needle = normalize_name(requested).replace("-", " ").replace("_", " ")
    candidates = (
        item["category"].replace("_", " "),
        item["categoryLabel"],
        item.get("sourceCategory") or "",
    )
    return any(needle in normalize_name(candidate) for candidate in candidates)


async def build_inventory_activity(history_days=90, future_days=90):
    """Join the complete rental catalog with bounded historical/future events."""
    cache_key = f"inventoryactivity:{history_days}:{future_days}"
    now = time.time()
    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    today = datetime.now().date()
    history_start = today - timedelta(days=history_days)
    future_end = today + timedelta(days=future_days)

    catalog = await io_get_pages("rentals", {"_body": "true"}, max_pages=10)
    # Completed events establish actual historical use; confirmed/contracted
    # events cover accounts where past bookings retain their original status.
    leads = await fetch_leads_by_status(
        history_start, future_end, ["complete", "confirmed", "contracted"]
    )

    usage = defaultdict(list)
    names_from_events = {}
    for lead in leads:
        event_date = lead_event_date(lead)
        if not event_date:
            continue
        for row in extract_rentals_from_lead(lead):
            rental_id = str(row["rental_id"])
            names_from_events[rental_id] = row["name"]
            usage[rental_id].append({
                "date": str(event_date),
                "quantity": row["quantity"],
                "status": lead_status_name(lead),
                "eventId": lead.get("id"),
            })

    items = []
    catalog_ids = set()
    for rental in catalog:
        if not isinstance(rental, dict):
            continue
        rental_id = rental_catalog_id(rental)
        if not rental_id:
            continue
        catalog_ids.add(rental_id)
        name = rental_name_from_object(rental, rental_id)
        if name == f"Rental {rental_id}":
            name = names_from_events.get(rental_id, name)
        category = classify_item(name)
        source_category = rental.get("category") or rental.get("categoryname") or rental.get("category_name")
        if isinstance(source_category, dict):
            source_category = source_category.get("name") or source_category.get("title")
        bookings = sorted(usage.get(rental_id, []), key=lambda booking: booking["date"])
        past = [booking for booking in bookings if booking["date"] < str(today)]
        upcoming = [booking for booking in bookings if booking["date"] >= str(today)]
        last_date = past[-1]["date"] if past else None
        next_date = upcoming[0]["date"] if upcoming else None
        items.append({
            "rentalId": rental_id,
            "name": name,
            "category": category,
            "categoryLabel": CATEGORY_LABELS.get(category, category),
            "sourceCategory": source_category,
            "ownedQuantity": rental_owned_quantity(rental),
            "active": rental_is_active(rental),
            "lastRentalDate": last_date,
            "nextRentalDate": next_date,
            "daysSinceLastRental": (today - datetime.strptime(last_date, "%Y-%m-%d").date()).days if last_date else None,
            "daysUntilNextRental": (datetime.strptime(next_date, "%Y-%m-%d").date() - today).days if next_date else None,
            "pastBookingCount": len(past),
            "upcomingBookingCount": len(upcoming),
            "pastBookings": past,
            "upcomingBookings": upcoming,
        })

    items.sort(key=lambda item: (item["categoryLabel"].lower(), item["name"].lower()))
    result = {
        "asOf": str(today),
        "historyStart": str(history_start),
        "futureEnd": str(future_end),
        "inventoryCount": len(items),
        "activeInventoryCount": sum(1 for item in items if item["active"]),
        "unbookedInventoryCount": sum(1 for item in items if item["active"] and not item["upcomingBookingCount"]),
        "eventItemsMissingFromCatalog": sorted(set(usage) - catalog_ids),
        "items": items,
        "cache": "miss",
    }
    _summary_cache[cache_key] = {"expires": now + SUMMARY_CACHE_SECONDS, "data": result}
    return result


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
# GENERAL CONFIRMED SCHEDULE SEARCH
# ============================================================

def lead_customer_name(lead):
    if not isinstance(lead, dict):
        return ""

    organization = str(
        lead.get("eventorganization", "")
        or ""
    ).strip()

    cust = lead.get("cust", {})
    first = ""
    last = ""

    if isinstance(cust, dict):
        first = str(cust.get("firstname", "") or "").strip()
        last = str(cust.get("lastname", "") or "").strip()

    full_name = " ".join(
        part for part in (first, last) if part
    ).strip()

    return organization or full_name


def lead_address(lead):
    if not isinstance(lead, dict):
        return ""

    street = str(lead.get("eventstreet", "") or "").strip()
    city = str(lead.get("eventcity", "") or "").strip()
    state = str(lead.get("eventstate", "") or "").strip()
    zip_code = str(lead.get("eventzip", "") or "").strip()

    # Normalize IO string "null".
    values = []
    for value in (street, city, state, zip_code):
        if value and value.lower() != "null":
            values.append(value)

    if not values:
        return ""

    if street and street.lower() != "null":
        city_state_zip = " ".join(
            x for x in (city, state, zip_code)
            if x and x.lower() != "null"
        ).strip()
        return f"{street}, {city_state_zip}" if city_state_zip else street

    return " ".join(values)


def lead_start_datetime(lead):
    value = lead.get("eventstarttime") if isinstance(lead, dict) else None

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except Exception:
        return None


def lead_end_datetime(lead):
    value = lead.get("eventendtime") if isinstance(lead, dict) else None

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except Exception:
        return None


def format_clock(dt):
    if not dt:
        return None

    # %-I isn't portable to Windows, so format then strip.
    return dt.strftime("%I:%M %p").lstrip("0")


async def build_schedule_search(
    search_text,
    start_date,
    end_date,
):
    """
    Search confirmed bookings by rental/item name substring.
    Returns event-level schedule rows with date/time, qty and address.
    """
    cache_key = (
        f"schedule:{normalize_name(search_text)}:"
        f"{start_date}:{end_date}"
    )
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    leads = await fetch_confirmed_leads(
        start_date,
        end_date
    )

    needle = normalize_name(search_text)
    rows = []

    for lead in leads:
        start_dt = lead_start_datetime(lead)
        end_dt = lead_end_datetime(lead)

        for rental_row in extract_rentals_from_lead(lead):
            rental_name = rental_row["name"]

            if needle not in normalize_name(rental_name):
                continue

            rows.append({
                "leadId": str(lead.get("id", "")),
                "date": (
                    str(start_dt.date())
                    if start_dt
                    else str(lead_event_date(lead) or "")
                ),
                "startTime": format_clock(start_dt),
                "endTime": format_clock(end_dt),
                "rentalName": rental_name,
                "quantity": rental_row["quantity"],
                "customer": lead_customer_name(lead),
                "address": lead_address(lead),
                "deliveryType": str(
                    lead.get("deliverytype", "") or ""
                ).strip(),
            })

    rows.sort(
        key=lambda x: (
            x["date"],
            x["startTime"] or "",
            x["rentalName"].lower(),
        )
    )

    result = {
        "search": search_text,
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date),
        },
        "status": "confirmed only",
        "count": len(rows),
        "bookings": rows,
        "cache": "miss",
    }

    _summary_cache[cache_key] = {
        "expires": now + SUMMARY_CACHE_SECONDS,
        "data": result,
    }

    return result


# ============================================================
# COLLECTIONS + STAFFING HELPERS
# ============================================================

ATTENDED_SERVICE_TERMS = (
    "foam party w/ music",
    "foam party with music",
    "interactive bubble party",
    "bubble party",
    "jurassic adventure",
    "photo booth",
    "photobooth",
)

NON_ATTENDED_FOAM_TERMS = (
    "foam party machine rental only",
    "foam machine rental only",
)


def money(value):
    try:
        return round(float(value or 0), 2)
    except Exception:
        return 0.0


def lead_total(lead):
    if not isinstance(lead, dict):
        return 0.0

    # IO provides both total and subtotal; total is the correct first choice.
    return money(
        lead.get("total")
        if lead.get("total") not in (None, "")
        else lead.get("subtotal")
    )


def lead_amount_paid(lead):
    if not isinstance(lead, dict):
        return 0.0

    # Prefer the fully-calculated totalamountpaid field when available.
    value = lead.get("totalamountpaid")

    if value not in (None, ""):
        return money(value)

    return money(lead.get("amountpaid"))


def lead_balance_due(lead):
    if not isinstance(lead, dict):
        return 0.0

    # IO exposes balancedue directly in _body=true lead responses.
    value = lead.get("balancedue")

    if value not in (None, ""):
        return max(money(value), 0.0)

    # Safe fallback only if balancedue is missing.
    return max(
        round(lead_total(lead) - lead_amount_paid(lead), 2),
        0.0
    )


def lead_fee_rows(lead):
    """
    Normalize IO's parallel fee arrays into readable rows.
    """
    if not isinstance(lead, dict):
        return []

    names = lead.get("feename", [])
    prices = lead.get("feeprice", [])
    types = lead.get("feetype", [])

    if not isinstance(names, list):
        return []

    if not isinstance(prices, list):
        prices = []

    if not isinstance(types, list):
        types = []

    rows = []

    for i, name in enumerate(names):
        if not name:
            continue

        price = money(prices[i]) if i < len(prices) else 0.0
        fee_type = str(types[i] or "") if i < len(types) else ""

        rows.append({
            "name": str(name),
            "amount": price,
            "type": fee_type,
        })

    return rows


def staffing_charge_info(lead):
    """
    Detect explicit PAID staff/attendant charges from IO fee lines.

    Important Callahan rule:
    - Zero-dollar Staff Costs lines are ignored.
    - Timed Delivery/Venue is NOT an attendant/staffing requirement,
      even though InflatableOffice may classify its fee type as "staff".
    """
    hits = []

    excluded_fee_names = {
        "timed delivery/venue",
        "timed delivery venue",
    }

    for fee in lead_fee_rows(lead):
        n = normalize_name(fee["name"])
        t = normalize_name(fee["type"])
        amount = money(fee.get("amount"))

        if n in excluded_fee_names:
            continue

        if (
            amount > 0
            and (
                "staff" in n
                or "attendant" in n
                or t == "staff"
            )
        ):
            hits.append(fee)

    return hits


def attended_rental_reasons(lead):
    """
    Rental names that automatically require an attendant.
    Machine-only foam is explicitly excluded unless a staff fee is present.
    """
    reasons = []

    for row in extract_rentals_from_lead(lead):
        name = row["name"]
        normalized = normalize_name(name)

        if any(term in normalized for term in NON_ATTENDED_FOAM_TERMS):
            continue

        if any(term in normalized for term in ATTENDED_SERVICE_TERMS):
            reasons.append({
                "type": "attended_service",
                "rentalName": name,
                "quantity": row["quantity"],
            })

    return reasons


def lead_event_item_count(lead):
    return sum(
        row["quantity"]
        for row in extract_rentals_from_lead(lead)
    )


def staffing_assessment(lead):
    staff_fees = staffing_charge_info(lead)
    attended = attended_rental_reasons(lead)

    reasons = []

    if staff_fees:
        reasons.append({
            "type": "staffing_charge",
            "fees": staff_fees,
            "totalStaffingCharge": round(
                sum(f["amount"] for f in staff_fees),
                2
            )
        })

    reasons.extend(attended)

    total = lead_total(lead)
    item_count = lead_event_item_count(lead)
    large_event = total >= LARGE_EVENT_THRESHOLD

    # Large events are flagged for review, but do not automatically
    # become "staff required" without one of the user's explicit rules.
    return {
        "staffRequired": bool(staff_fees or attended),
        "staffingReasons": reasons,
        "staffingCharge": round(
            sum(f["amount"] for f in staff_fees),
            2
        ),
        "largeEventReview": large_event,
        "largeEventThreshold": LARGE_EVENT_THRESHOLD,
        "eventTotal": total,
        "bookedItemQuantity": item_count,
    }


def compact_event_row(lead):
    start_dt = lead_start_datetime(lead)
    end_dt = lead_end_datetime(lead)

    rentals = [
        {
            "name": row["name"],
            "quantity": row["quantity"]
        }
        for row in extract_rentals_from_lead(lead)
    ]

    return {
        "leadId": str(lead.get("id", "")),
        "date": (
            str(start_dt.date())
            if start_dt
            else str(lead_event_date(lead) or "")
        ),
        "startTime": format_clock(start_dt),
        "endTime": format_clock(end_dt),
        "customer": lead_customer_name(lead),
        "address": lead_address(lead),
        "deliveryType": str(
            lead.get("deliverytype", "") or ""
        ).strip(),
        "rentals": rentals,
    }


async def build_collections_report(start_date, end_date):
    cache_key = f"collections:{start_date}:{end_date}"
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    leads = await fetch_confirmed_leads(start_date, end_date)

    events = []
    total_contracts = 0.0
    total_paid = 0.0
    total_balance = 0.0

    for lead in leads:
        total = lead_total(lead)
        paid = lead_amount_paid(lead)
        balance = lead_balance_due(lead)

        total_contracts += total
        total_paid += paid
        total_balance += balance

        row = compact_event_row(lead)
        row.update({
            "eventTotal": round(total, 2),
            "amountPaid": round(paid, 2),
            "balanceDue": round(balance, 2),
            "paidInFull": balance <= 0.005,
        })
        events.append(row)

    events.sort(
        key=lambda x: (
            x["date"],
            x["startTime"] or "",
            x["customer"].lower(),
        )
    )

    outstanding = [
        event
        for event in events
        if event["balanceDue"] > 0.005
    ]

    result = {
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date)
        },
        "status": "confirmed only",
        "confirmedEventCount": len(leads),
        "financialSummary": {
            "contractedRevenue": round(total_contracts, 2),
            "alreadyPaid": round(total_paid, 2),
            "outstandingToCollect": round(total_balance, 2),
            "eventsWithBalanceDue": len(outstanding),
            "eventsPaidInFull": len(events) - len(outstanding),
        },
        "outstandingEvents": outstanding,
        "allEvents": events,
        "cache": "miss",
    }

    _summary_cache[cache_key] = {
        "expires": now + SUMMARY_CACHE_SECONDS,
        "data": result,
    }

    return result


async def build_staffing_report(start_date, end_date):
    cache_key = f"staffing:{start_date}:{end_date}"
    now = time.time()

    cached = _summary_cache.get(cache_key)
    if cached and now < cached["expires"]:
        result = dict(cached["data"])
        result["cache"] = "hit"
        return result

    leads = await fetch_confirmed_leads(start_date, end_date)

    required = []
    large_review = []

    for lead in leads:
        assessment = staffing_assessment(lead)
        row = compact_event_row(lead)
        row.update(assessment)

        if assessment["staffRequired"]:
            required.append(row)

        if assessment["largeEventReview"]:
            large_review.append(row)

    required.sort(
        key=lambda x: (
            x["date"],
            x["startTime"] or "",
            x["customer"].lower(),
        )
    )

    large_review.sort(
        key=lambda x: (
            x["date"],
            -(x["eventTotal"] or 0),
        )
    )

    # Basic overlap detection for attended events.
    overlaps = []

    for i, first in enumerate(required):
        first_start = None
        first_end = None

        try:
            first_start = datetime.strptime(
                f'{first["date"]} {first["startTime"]}',
                "%Y-%m-%d %I:%M %p"
            ) if first["startTime"] else None

            first_end = datetime.strptime(
                f'{first["date"]} {first["endTime"]}',
                "%Y-%m-%d %I:%M %p"
            ) if first["endTime"] else None
        except Exception:
            pass

        if not first_start or not first_end:
            continue

        for second in required[i + 1:]:
            if second["date"] != first["date"]:
                continue

            try:
                second_start = datetime.strptime(
                    f'{second["date"]} {second["startTime"]}',
                    "%Y-%m-%d %I:%M %p"
                ) if second["startTime"] else None

                second_end = datetime.strptime(
                    f'{second["date"]} {second["endTime"]}',
                    "%Y-%m-%d %I:%M %p"
                ) if second["endTime"] else None
            except Exception:
                second_start = None
                second_end = None

            if not second_start or not second_end:
                continue

            if first_start < second_end and second_start < first_end:
                overlaps.append({
                    "date": first["date"],
                    "event1": {
                        "leadId": first["leadId"],
                        "customer": first["customer"],
                        "time": f'{first["startTime"]} - {first["endTime"]}',
                        "address": first["address"],
                    },
                    "event2": {
                        "leadId": second["leadId"],
                        "customer": second["customer"],
                        "time": f'{second["startTime"]} - {second["endTime"]}',
                        "address": second["address"],
                    },
                    "warning": "Attended events overlap and may require separate staff."
                })

    result = {
        "dateRange": {
            "start": str(start_date),
            "end": str(end_date)
        },
        "status": "confirmed only",
        "confirmedEventCount": len(leads),
        "staffRequiredCount": len(required),
        "staffRequiredEvents": required,
        "largeEventReviewCount": len(large_review),
        "largeEventsForReview": large_review,
        "overlapWarnings": overlaps,
        "rules": {
            "automaticStaffingTriggers": [
                "Any staffing or attendant charge",
                "Foam Party W/ Music",
                "Bubble Party",
                "Jurassic Adventure",
                "Photo Booth",
            ],
            "machineOnlyFoam": (
                "Foam machine rental only does not automatically require "
                "an attendant unless a staffing charge is present."
            ),
            "largeEventReviewThreshold": LARGE_EVENT_THRESHOLD,
        },
        "cache": "miss",
    }

    _summary_cache[cache_key] = {
        "expires": now + SUMMARY_CACHE_SECONDS,
        "data": result,
    }

    return result
async def build_status_events(status_text, start_date, end_date, q=None):
    statuses = requested_statuses(status_text)
    leads = await fetch_leads_by_status(start_date, end_date, statuses)
    needle = normalize_name(q) if q else ""
    events = []

    for lead in leads:
        rentals = extract_rentals_from_lead(lead)
        item_names = [r["name"] for r in rentals]
        haystack = normalize_name(" ".join([
            lead_customer_name(lead),
            lead_address(lead),
            str(lead.get("eventname", "") or ""),
            " ".join(item_names),
        ]))
        if needle and needle not in haystack:
            continue

        cust = lead.get("cust") if isinstance(lead.get("cust"), dict) else {}
        events.append({
            "leadId": str(lead.get("id", "")),
            "status": lead_status_name(lead),
            "eventDate": str(lead_event_date(lead) or ""),
            "startTime": format_clock(lead_start_datetime(lead)),
            "endTime": format_clock(lead_end_datetime(lead)),
            "customer": lead_customer_name(lead),
            "email": str(cust.get("email", "") or lead.get("email", "") or ""),
            "phone": str(cust.get("cellphone", "") or cust.get("homephone", "") or lead.get("cellphone", "") or ""),
            "address": lead_address(lead),
            "eventName": str(lead.get("eventname", "") or ""),
            "eventTotal": round(lead_total(lead), 2),
            "amountPaid": round(lead_amount_paid(lead), 2),
            "balanceDue": round(lead_balance_due(lead), 2),
            "items": rentals,
            "createdTime": str(lead.get("createtime", "") or ""),
            "modifiedTime": str(lead.get("modifiedtime", "") or ""),
        })

    events.sort(key=lambda x: (x["eventDate"] or "9999-12-31", x["customer"].lower()))
    return {
        "statuses": statuses,
        "dateRange": {"start": str(start_date), "end": str(end_date)},
        "search": q or "",
        "count": len(events),
        "events": events,
    }


async def build_status_summary(status_text, start_date, end_date):
    statuses = requested_statuses(status_text)
    leads = await fetch_leads_by_status(start_date, end_date, statuses)
    by_status = {}
    total_revenue = total_paid = total_balance = 0.0

    for lead in leads:
        name = lead_status_name(lead) or "Unknown"
        bucket = by_status.setdefault(name, {
            "count": 0, "eventTotal": 0.0, "amountPaid": 0.0, "balanceDue": 0.0
        })
        total = lead_total(lead)
        paid = lead_amount_paid(lead)
        balance = lead_balance_due(lead)
        bucket["count"] += 1
        bucket["eventTotal"] += total
        bucket["amountPaid"] += paid
        bucket["balanceDue"] += balance
        total_revenue += total
        total_paid += paid
        total_balance += balance

    for bucket in by_status.values():
        for key in ("eventTotal", "amountPaid", "balanceDue"):
            bucket[key] = round(bucket[key], 2)

    count = len(leads)
    return {
        "statuses": statuses,
        "dateRange": {"start": str(start_date), "end": str(end_date)},
        "count": count,
        "totals": {
            "eventTotal": round(total_revenue, 2),
            "amountPaid": round(total_paid, 2),
            "balanceDue": round(total_balance, 2),
            "averageEventValue": round(total_revenue / count, 2) if count else 0.0,
        },
        "byStatus": by_status,
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
async def root():
    return {
        "service": "Callahan InflatableOffice Bridge",
        "status": "ok",
        "mode": "read-only",
        "version": "3.5.0"
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
        "statusIds": {
            "confirmed": CONFIRMED_STATUS_ID,
            "quote": QUOTE_STATUS_ID or None,
            "contracted": CONTRACTED_STATUS_ID or None,
            "complete": COMPLETE_STATUS_ID or None,
        },
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


@app.get("/inventory")
async def inventory_overview(
    category: Optional[str] = Query(default=None, description="Category name, such as tents, concessions, or inflatables"),
    q: Optional[str] = Query(default=None, description="Full or partial inventory item name"),
    history_days: int = Query(default=90, ge=1, le=365),
    future_days: int = Query(default=90, ge=1, le=365),
    include_inactive: bool = Query(default=False),
    _: bool = Depends(check_token),
):
    """Complete inventory with previous rentals and upcoming reservations."""
    report = await build_inventory_activity(history_days, future_days)
    needle = normalize_name(q) if q else None
    items = [
        item for item in report["items"]
        if (include_inactive or item["active"])
        and inventory_category_matches(item, category)
        and (not needle or needle in normalize_name(item["name"]))
    ]
    return {**report, "filters": {"category": category, "q": q}, "count": len(items), "items": items}


@app.get("/inventory/idle")
async def inventory_idle(
    category: Optional[str] = Query(default=None),
    min_idle_days: int = Query(default=0, ge=0, le=365),
    history_days: int = Query(default=90, ge=1, le=365),
    future_days: int = Query(default=90, ge=1, le=365),
    _: bool = Depends(check_token),
):
    """Active equipment without any reservation inside the upcoming window."""
    report = await build_inventory_activity(history_days, future_days)
    items = [
        item for item in report["items"]
        if item["active"]
        and item["upcomingBookingCount"] == 0
        and inventory_category_matches(item, category)
        and (item["daysSinceLastRental"] is None or item["daysSinceLastRental"] >= min_idle_days)
    ]
    items.sort(key=lambda item: (item["daysSinceLastRental"] is not None, -(item["daysSinceLastRental"] or 0), item["name"].lower()))
    return {
        "asOf": report["asOf"], "historyStart": report["historyStart"],
        "futureEnd": report["futureEnd"], "category": category,
        "minIdleDays": min_idle_days, "count": len(items), "items": items,
        "cache": report["cache"],
    }


@app.get("/inventory/categories")
async def inventory_category_summary(
    history_days: int = Query(default=90, ge=1, le=365),
    future_days: int = Query(default=90, ge=1, le=365),
    _: bool = Depends(check_token),
):
    """Inventory, booking activity, and idle equipment grouped by category."""
    report = await build_inventory_activity(history_days, future_days)
    groups = {}
    for item in report["items"]:
        if not item["active"]:
            continue
        group = groups.setdefault(item["category"], {
            "category": item["category"], "label": item["categoryLabel"],
            "itemCount": 0, "ownedQuantity": 0, "pastBookingCount": 0,
            "upcomingBookingCount": 0, "unbookedItemCount": 0, "unbookedItems": [],
        })
        group["itemCount"] += 1
        group["ownedQuantity"] += item["ownedQuantity"] or 0
        group["pastBookingCount"] += item["pastBookingCount"]
        group["upcomingBookingCount"] += item["upcomingBookingCount"]
        if not item["upcomingBookingCount"]:
            group["unbookedItemCount"] += 1
            group["unbookedItems"].append(item["name"])
    categories = sorted(groups.values(), key=lambda group: group["label"].lower())
    return {
        "asOf": report["asOf"], "historyStart": report["historyStart"],
        "futureEnd": report["futureEnd"], "categoryCount": len(categories),
        "categories": categories, "cache": report["cache"],
    }


@app.get("/inventory/item")
async def inventory_item_history(
    name: str = Query(..., min_length=2, description="Full or partial equipment name"),
    history_days: int = Query(default=180, ge=1, le=365),
    future_days: int = Query(default=180, ge=1, le=365),
    _: bool = Depends(check_token),
):
    """Last use, next use, and all matching past/future equipment bookings."""
    report = await build_inventory_activity(history_days, future_days)
    needle = normalize_name(name)
    matches = [item for item in report["items"] if needle in normalize_name(item["name"])]
    return {
        "search": name, "asOf": report["asOf"], "historyStart": report["historyStart"],
        "futureEnd": report["futureEnd"], "count": len(matches), "items": matches,
        "cache": report["cache"],
    }


@app.get("/status-events")
async def status_events(
    status: str = Query(default="confirmed", description="confirmed, quote, contracted, complete; comma-separated allowed"),
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    q: Optional[str] = Query(default=None, description="Optional customer, address, event, or rental/item text"),
    _: bool = Depends(check_token),
):
    start_date = parse_requested_date(start)
    end_date = parse_requested_date(end)
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if (end_date - start_date).days > 730:
        raise HTTPException(status_code=400, detail="Status event range is limited to 730 days")
    return await build_status_events(status, start_date, end_date, q=q)


@app.get("/status-summary")
async def status_summary(
    status: str = Query(default="confirmed", description="confirmed, quote, contracted, complete; comma-separated allowed"),
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    _: bool = Depends(check_token),
):
    start_date = parse_requested_date(start)
    end_date = parse_requested_date(end)
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")
    if (end_date - start_date).days > 730:
        raise HTTPException(status_code=400, detail="Status summary range is limited to 730 days")
    return await build_status_summary(status, start_date, end_date)


@app.get("/weekend-collections")
async def weekend_collections(
    _: bool = Depends(check_token)
):
    """
    Protected financial report for Friday-Sunday.
    """
    today = datetime.now().date()
    days_until_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)

    return await build_collections_report(friday, sunday)


@app.get("/collections-range")
async def collections_range(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    _: bool = Depends(check_token),
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
            detail="Collections range is limited to 31 days"
        )

    return await build_collections_report(
        start_date,
        end_date
    )


@app.get("/staffing")
async def staffing(
    days: int = Query(default=30, ge=1, le=180),
    _: bool = Depends(check_token),
):
    start_date = datetime.now().date()
    end_date = start_date + timedelta(days=days)

    return await build_staffing_report(
        start_date,
        end_date
    )


@app.get("/staffing-range")
async def staffing_range(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    _: bool = Depends(check_token),
):
    start_date = parse_requested_date(start)
    end_date = parse_requested_date(end)

    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be on or after start date"
        )

    if (end_date - start_date).days > 180:
        raise HTTPException(
            status_code=400,
            detail="Staffing range is limited to 180 days"
        )

    return await build_staffing_report(
        start_date,
        end_date
    )


@app.get("/weekend-operations")
async def weekend_operations(
    _: bool = Depends(check_token)
):
    """
    Protected combined weekend financial + staffing report.
    """
    today = datetime.now().date()
    days_until_friday = (4 - today.weekday()) % 7
    friday = today + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)

    collections = await build_collections_report(
        friday,
        sunday
    )
    staffing = await build_staffing_report(
        friday,
        sunday
    )

    return {
        "dateRange": {
            "start": str(friday),
            "end": str(sunday)
        },
        "collections": collections,
        "staffing": staffing,
    }


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


@app.get("/public/schedule")
async def public_schedule(
    q: str = Query(
        ...,
        min_length=2,
        description="Rental/item search text, e.g. Foam, Tent, Melting Ice"
    ),
    days: int = Query(
        default=14,
        ge=1,
        le=180,
        description="Number of days from today"
    )
):
    """
    Confirmed schedule search from today forward.
    Example:
      /public/schedule?q=Foam&days=14
    """
    start_date = datetime.now().date()
    end_date = start_date + timedelta(days=days)

    return await build_schedule_search(
        q,
        start_date,
        end_date
    )


@app.get("/public/schedule-range")
async def public_schedule_range(
    q: str = Query(
        ...,
        min_length=2,
        description="Rental/item search text"
    ),
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    """
    Confirmed schedule search over an explicit date range.
    """
    start_date = parse_requested_date(start)
    end_date = parse_requested_date(end)

    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be on or after start date"
        )

    if (end_date - start_date).days > 180:
        raise HTTPException(
            status_code=400,
            detail="Schedule range is limited to 180 days"
        )

    return await build_schedule_search(
        q,
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
