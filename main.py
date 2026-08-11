from datetime import datetime, timedelta
from collections import Counter


def _find_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "results", "data", "leads"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _parse_date(value):
    if not value:
        return None

    text = str(value).strip()

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _lead_date(lead):
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

    leads = _find_list(data)
    totals = Counter()

    matching_leads = 0

    for lead in leads:
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
