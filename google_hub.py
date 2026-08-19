import base64
import hashlib
import hmac
import html
import os
import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

router = APIRouter(prefix="/google", tags=["Google AI Hub"])
security = HTTPBearer()

BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "").strip()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://callahanentertainment.onrender.com/google/oauth/callback",
).strip()
GOOGLE_OAUTH_STATE_SECRET = os.getenv(
    "GOOGLE_OAUTH_STATE_SECRET",
    BRIDGE_TOKEN,
).strip()

GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "").replace("properties/", "").strip()
SEARCH_CONSOLE_SITE_URL = os.getenv("SEARCH_CONSOLE_SITE_URL", "").strip()

GOOGLE_ADS_CUSTOMER_ID = os.getenv(
    "GOOGLE_ADS_CUSTOMER_ID", ""
).replace("-", "").strip()
GOOGLE_ADS_LOGIN_CUSTOMER_ID = os.getenv(
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""
).replace("-", "").strip()
GOOGLE_ADS_DEVELOPER_TOKEN = os.getenv(
    "GOOGLE_ADS_DEVELOPER_TOKEN", ""
).strip()
GOOGLE_ADS_API_VERSION = os.getenv(
    "GOOGLE_ADS_API_VERSION", "v25"
).strip().lstrip("/")

GOOGLE_BUSINESS_ACCOUNT_ID = os.getenv(
    "GOOGLE_BUSINESS_ACCOUNT_ID", ""
).strip()
GOOGLE_BUSINESS_LOCATION_ID = os.getenv(
    "GOOGLE_BUSINESS_LOCATION_ID", ""
).strip()

_access_token_cache = {"token": None, "expires": 0.0}


def _check_bridge_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    if not BRIDGE_TOKEN:
        raise HTTPException(status_code=500, detail="BRIDGE_TOKEN is not configured")
    if credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


def _require_oauth_client():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be configured in Render",
        )


def _verify_state(state: str):
    if not GOOGLE_OAUTH_STATE_SECRET:
        raise HTTPException(status_code=500, detail="OAuth state secret is not configured")
    try:
        stamp_text, supplied = state.split(".", 1)
        stamp = int(stamp_text)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    if abs(time.time() - stamp) > 900:
        raise HTTPException(status_code=400, detail="OAuth state expired; start authorization again")

    expected_sig = hmac.new(
        GOOGLE_OAUTH_STATE_SECRET.encode(),
        stamp_text.encode(),
        hashlib.sha256,
    ).digest()
    expected = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")


async def _exchange_refresh_token() -> str:
    _require_oauth_client()

    if not GOOGLE_REFRESH_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not fully connected. Visit /google/oauth/start, "
                "authorize Google, then save GOOGLE_REFRESH_TOKEN in Render."
            ),
        )

    now = time.time()
    if _access_token_cache["token"] and now < _access_token_cache["expires"] - 60:
        return _access_token_cache["token"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GOOGLE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )

    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("error_description", "")
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Google token refresh failed (HTTP {response.status_code})"
            + (f": {detail}" if detail else ""),
        )

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Google did not return an access token")

    _access_token_cache["token"] = access_token
    _access_token_cache["expires"] = now + int(payload.get("expires_in", 3600))
    return access_token


async def _google_request(method: str, url: str, *, json=None, params=None, headers=None):
    access_token = await _exchange_refresh_token()
    merged_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if headers:
        merged_headers.update(headers)

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.request(
            method,
            url,
            json=json,
            params=params,
            headers=merged_headers,
        )

    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error", {})
                if isinstance(error, dict):
                    detail = error.get("message", "")
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Google API returned HTTP {response.status_code}"
            + (f": {detail}" if detail else ""),
        )

    return response.json() if response.content else {}


def _default_dates(days: int):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=max(days - 1, 0))
    return str(start), str(end)


def _period_dates(days: int, offset_periods: int = 0):
    current_end = date.today() - timedelta(days=1)
    end = current_end - timedelta(days=days * offset_periods)
    start = end - timedelta(days=max(days - 1, 0))
    return str(start), str(end)


def _split_csv(value: str):
    return [part.strip() for part in value.split(",") if part.strip()]


def _pct_change(current, previous):
    current = float(current or 0)
    previous = float(previous or 0)
    if previous == 0:
        return None if current else 0.0
    return round(((current - previous) / previous) * 100, 2)


def _safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _safe_int(value):
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _sc_rows(payload):
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    return rows if isinstance(rows, list) else []


def _sc_totals(payload):
    rows = _sc_rows(payload)
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    row = rows[0]
    return {
        "clicks": _safe_int(row.get("clicks")),
        "impressions": _safe_int(row.get("impressions")),
        "ctr": round(_safe_float(row.get("ctr")), 6),
        "position": round(_safe_float(row.get("position")), 2),
    }


def _ga_metric_map(payload):
    if not isinstance(payload, dict):
        return {}
    headers = payload.get("metricHeaders", [])
    rows = payload.get("rows", [])
    if not rows:
        return {}
    values = rows[0].get("metricValues", [])
    result = {}
    for idx, header in enumerate(headers):
        name = header.get("name")
        raw = values[idx].get("value") if idx < len(values) else "0"
        result[name] = _safe_float(raw)
    return result


def _ga_channel_rows(payload):
    if not isinstance(payload, dict):
        return []
    metric_names = [h.get("name") for h in payload.get("metricHeaders", [])]
    result = []
    for row in payload.get("rows", []) or []:
        dims = row.get("dimensionValues", [])
        vals = row.get("metricValues", [])
        item = {"channel": dims[0].get("value", "") if dims else ""}
        for idx, name in enumerate(metric_names):
            raw = vals[idx].get("value") if idx < len(vals) else "0"
            item[name] = _safe_float(raw)
        result.append(item)
    return result


def _sc_url(site: str):
    return (
        "https://www.googleapis.com/webmasters/v3/sites/"
        + quote(site, safe="")
        + "/searchAnalytics/query"
    )


async def _sc_report(site: str, start: str, end: str, dimensions=None, row_limit=100):
    payload = {"startDate": start, "endDate": end, "rowLimit": row_limit}
    if dimensions:
        payload["dimensions"] = dimensions
    return await _google_request("POST", _sc_url(site), json=payload)


async def _ga_report(prop: str, start: str, end: str, dimensions=None, metrics=None, limit=100):
    payload = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "metrics": [{"name": m} for m in (metrics or [])],
        "limit": str(limit),
    }
    if dimensions:
        payload["dimensions"] = [{"name": d} for d in dimensions]
    return await _google_request(
        "POST",
        f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
        json=payload,
    )


def _ads_headers(access_token: str):
    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        raise HTTPException(status_code=503, detail="GOOGLE_ADS_DEVELOPER_TOKEN is not configured")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if GOOGLE_ADS_LOGIN_CUSTOMER_ID:
        headers["login-customer-id"] = GOOGLE_ADS_LOGIN_CUSTOMER_ID
    return headers


async def _ads_search(query: str, customer_id: Optional[str] = None):
    cid = (customer_id or GOOGLE_ADS_CUSTOMER_ID).replace("-", "").strip()
    if not cid:
        raise HTTPException(status_code=503, detail="GOOGLE_ADS_CUSTOMER_ID is not configured")

    access_token = await _exchange_refresh_token()
    headers = _ads_headers(access_token)
    url = (
        f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}"
        f"/customers/{cid}/googleAds:search"
    )

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(url, headers=headers, json={"query": query, "pageSize": 1000})

    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Google Ads returned HTTP {response.status_code}"
            + (f": {detail}" if detail else ""),
        )
    return response.json()


@router.get("/oauth/callback", response_class=HTMLResponse)
async def google_oauth_callback(code: str, state: str):
    _require_oauth_client()
    _verify_state(state)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    if response.status_code >= 400:
        return HTMLResponse(
            f"<h2>Google connection failed</h2><p>HTTP {response.status_code}</p>",
            status_code=502,
        )

    payload = response.json()
    refresh_token = payload.get("refresh_token", "")
    if not refresh_token:
        return HTMLResponse(
            "<h2>Authorization succeeded, but Google did not return a refresh token.</h2>"
            "<p>Remove the app from Google permissions and run /google/oauth/start again.</p>"
        )

    safe_token = html.escape(refresh_token)
    return HTMLResponse(
        "<h2>Callahan AI Hub connected to Google</h2>"
        "<p>Copy this into Render as <b>GOOGLE_REFRESH_TOKEN</b>. Treat it like a password.</p>"
        f"<textarea style='width:95%;height:120px'>{safe_token}</textarea>"
        "<p>Save it in Render, redeploy, then test /google/status.</p>"
    )


@router.get("/status")
async def google_status(_: bool = Depends(_check_bridge_token)):
    result = {
        "oauthClientConfigured": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        "refreshTokenConfigured": bool(GOOGLE_REFRESH_TOKEN),
        "searchConsoleSiteConfigured": bool(SEARCH_CONSOLE_SITE_URL),
        "analyticsPropertyConfigured": bool(GA4_PROPERTY_ID),
        "adsCustomerConfigured": bool(GOOGLE_ADS_CUSTOMER_ID),
        "adsDeveloperTokenConfigured": bool(GOOGLE_ADS_DEVELOPER_TOKEN),
        "adsApiVersion": GOOGLE_ADS_API_VERSION,
        "businessAccountConfigured": bool(GOOGLE_BUSINESS_ACCOUNT_ID),
        "businessLocationConfigured": bool(GOOGLE_BUSINESS_LOCATION_ID),
    }
    try:
        if GOOGLE_REFRESH_TOKEN:
            await _exchange_refresh_token()
            result["googleOauth"] = "ok"
        else:
            result["googleOauth"] = "not connected"
    except HTTPException as exc:
        result["googleOauth"] = "error"
        result["oauthError"] = exc.detail
    return result


@router.get("/search-console/sites")
async def search_console_sites(_: bool = Depends(_check_bridge_token)):
    return await _google_request("GET", "https://www.googleapis.com/webmasters/v3/sites")


@router.get("/search-console/performance")
async def search_console_performance(
    days: int = Query(default=28, ge=1, le=486),
    dimensions: str = Query(
        default="query",
        description="Comma-separated: query,page,country,device,date,searchAppearance",
    ),
    row_limit: int = Query(default=100, ge=1, le=25000),
    site_url: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    site = (site_url or SEARCH_CONSOLE_SITE_URL).strip()
    if not site:
        raise HTTPException(status_code=503, detail="SEARCH_CONSOLE_SITE_URL is not configured")

    dims = _split_csv(dimensions)
    allowed = {"query", "page", "country", "device", "date", "searchAppearance"}
    if not dims or any(d not in allowed for d in dims):
        raise HTTPException(status_code=400, detail=f"dimensions must use: {', '.join(sorted(allowed))}")

    start, end = _default_dates(days)
    return await _sc_report(site, start, end, dims, row_limit)


@router.get("/search-console/summary")
async def search_console_summary(
    days: int = Query(default=28, ge=1, le=486),
    site_url: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    site = (site_url or SEARCH_CONSOLE_SITE_URL).strip()
    if not site:
        raise HTTPException(status_code=503, detail="SEARCH_CONSOLE_SITE_URL is not configured")

    start, end = _default_dates(days)
    return {
        "days": days,
        "site": site,
        "dateRange": {"start": start, "end": end},
        "topQueries": await _sc_report(site, start, end, ["query"], 50),
        "topPages": await _sc_report(site, start, end, ["page"], 50),
        "devices": await _sc_report(site, start, end, ["device"], 10),
        "countries": await _sc_report(site, start, end, ["country"], 25),
        "daily": await _sc_report(site, start, end, ["date"], 365),
    }


@router.get("/search-console/opportunities")
async def search_console_opportunities(
    days: int = Query(default=28, ge=7, le=486),
    site_url: Optional[str] = Query(default=None),
    min_impressions: int = Query(default=20, ge=1, le=1000000),
    min_position: float = Query(default=3.0, ge=1.0, le=100.0),
    max_position: float = Query(default=15.0, ge=1.0, le=100.0),
    limit: int = Query(default=25, ge=1, le=100),
    _: bool = Depends(_check_bridge_token),
):
    site = (site_url or SEARCH_CONSOLE_SITE_URL).strip()
    if not site:
        raise HTTPException(status_code=503, detail="SEARCH_CONSOLE_SITE_URL is not configured")
    if max_position < min_position:
        raise HTTPException(status_code=400, detail="max_position must be >= min_position")

    start, end = _default_dates(days)
    queries = await _sc_report(site, start, end, ["query"], 1000)
    pages = await _sc_report(site, start, end, ["page"], 1000)

    def score_row(row):
        impressions = _safe_int(row.get("impressions"))
        clicks = _safe_int(row.get("clicks"))
        ctr = _safe_float(row.get("ctr"))
        position = _safe_float(row.get("position"))
        opportunity_score = impressions * max(0.0, (max_position + 1) - position) * max(0.01, 0.12 - ctr)
        return {
            "key": (row.get("keys") or [""])[0],
            "clicks": clicks,
            "impressions": impressions,
            "ctr": round(ctr, 4),
            "position": round(position, 2),
            "opportunityScore": round(opportunity_score, 2),
        }

    quick_queries = []
    for row in _sc_rows(queries):
        position = _safe_float(row.get("position"))
        impressions = _safe_int(row.get("impressions"))
        if impressions >= min_impressions and min_position <= position <= max_position:
            quick_queries.append(score_row(row))
    quick_queries.sort(key=lambda x: x["opportunityScore"], reverse=True)

    page_opportunities = []
    for row in _sc_rows(pages):
        position = _safe_float(row.get("position"))
        impressions = _safe_int(row.get("impressions"))
        ctr = _safe_float(row.get("ctr"))
        if impressions >= min_impressions and position >= 4 and ctr <= 0.05:
            page_opportunities.append(score_row(row))
    page_opportunities.sort(key=lambda x: x["opportunityScore"], reverse=True)

    return {
        "site": site,
        "days": days,
        "dateRange": {"start": start, "end": end},
        "criteria": {
            "minImpressions": min_impressions,
            "queryPositionRange": [min_position, max_position],
            "pageCtrThreshold": 0.05,
        },
        "quickWinQueries": quick_queries[:limit],
        "pageOpportunities": page_opportunities[:limit],
    }


@router.get("/analytics/report")
async def analytics_report(
    days: int = Query(default=28, ge=1, le=365),
    dimensions: str = Query(default="sessionDefaultChannelGroup", description="Comma-separated GA4 dimensions"),
    metrics: str = Query(default="sessions,totalUsers,newUsers,keyEvents", description="Comma-separated GA4 metrics"),
    limit: int = Query(default=100, ge=1, le=10000),
    property_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    prop = (property_id or GA4_PROPERTY_ID).replace("properties/", "").strip()
    if not prop:
        raise HTTPException(status_code=503, detail="GA4_PROPERTY_ID is not configured")

    start, end = _default_dates(days)
    dims = _split_csv(dimensions)
    mets = _split_csv(metrics)
    if not mets:
        raise HTTPException(status_code=400, detail="At least one metric is required")

    return await _ga_report(prop, start, end, dims, mets, limit)


@router.get("/analytics/overview")
async def analytics_overview(
    days: int = Query(default=28, ge=1, le=365),
    property_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    prop = (property_id or GA4_PROPERTY_ID).replace("properties/", "").strip()
    if not prop:
        raise HTTPException(status_code=503, detail="GA4_PROPERTY_ID is not configured")

    start, end = _default_dates(days)
    totals = await _ga_report(
        prop, start, end,
        metrics=["sessions", "totalUsers", "newUsers", "engagedSessions", "screenPageViews", "keyEvents"],
        limit=1,
    )
    channels = await _ga_report(
        prop, start, end,
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "totalUsers", "newUsers", "keyEvents"],
        limit=50,
    )
    pages = await _ga_report(
        prop, start, end,
        dimensions=["pagePath"],
        metrics=["screenPageViews", "sessions", "totalUsers", "keyEvents"],
        limit=50,
    )

    return {
        "days": days,
        "dateRange": {"start": start, "end": end},
        "totals": totals,
        "channels": channels,
        "topPages": pages,
    }


@router.get("/ads/customers")
async def ads_customers(_: bool = Depends(_check_bridge_token)):
    access_token = await _exchange_refresh_token()
    headers = _ads_headers(access_token)

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.get(
            f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/customers:listAccessibleCustomers",
            headers=headers,
        )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Google Ads returned HTTP {response.status_code}")
    return response.json()


@router.get("/ads/campaigns")
async def ads_campaigns(
    days: int = Query(default=28, ge=1, le=365),
    customer_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    start, end = _default_dates(days)
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          metrics.impressions,
          metrics.clicks,
          metrics.ctr,
          metrics.average_cpc,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value,
          metrics.cost_per_conversion
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.cost_micros DESC
    """
    return await _ads_search(query, customer_id)


@router.get("/ads/search-terms")
async def ads_search_terms(
    days: int = Query(default=28, ge=1, le=365),
    customer_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    _: bool = Depends(_check_bridge_token),
):
    start, end = _default_dates(days)
    query = f"""
        SELECT
          campaign.name,
          ad_group.name,
          search_term_view.search_term,
          metrics.impressions,
          metrics.clicks,
          metrics.ctr,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM search_term_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.clicks DESC
        LIMIT {limit}
    """
    return await _ads_search(query, customer_id)


@router.get("/ads/keywords")
async def ads_keywords(
    days: int = Query(default=28, ge=1, le=365),
    customer_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    _: bool = Depends(_check_bridge_token),
):
    start, end = _default_dates(days)
    query = f"""
        SELECT
          campaign.name,
          ad_group.name,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.status,
          metrics.impressions,
          metrics.clicks,
          metrics.ctr,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM keyword_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.clicks DESC
        LIMIT {limit}
    """
    return await _ads_search(query, customer_id)


@router.get("/business/accounts")
async def business_accounts(_: bool = Depends(_check_bridge_token)):
    return await _google_request("GET", "https://mybusinessaccountmanagement.googleapis.com/v1/accounts")


@router.get("/business/locations")
async def business_locations(
    account_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    aid = (account_id or GOOGLE_BUSINESS_ACCOUNT_ID).replace("accounts/", "").strip()
    if not aid:
        raise HTTPException(status_code=503, detail="GOOGLE_BUSINESS_ACCOUNT_ID is not configured")

    return await _google_request(
        "GET",
        f"https://mybusiness.googleapis.com/v4/accounts/{aid}/locations",
        params={"pageSize": 100},
    )


@router.get("/reviews")
async def business_reviews(
    page_size: int = Query(default=50, ge=1, le=50),
    account_id: Optional[str] = Query(default=None),
    location_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    aid = (account_id or GOOGLE_BUSINESS_ACCOUNT_ID).replace("accounts/", "").strip()
    lid = (location_id or GOOGLE_BUSINESS_LOCATION_ID).replace("locations/", "").strip()
    if not aid or not lid:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_BUSINESS_ACCOUNT_ID and GOOGLE_BUSINESS_LOCATION_ID must be configured",
        )

    return await _google_request(
        "GET",
        f"https://mybusiness.googleapis.com/v4/accounts/{aid}/locations/{lid}/reviews",
        params={"pageSize": page_size, "orderBy": "updateTime desc"},
    )


@router.get("/marketing/compare")
async def marketing_compare(
    days: int = Query(default=28, ge=7, le=180),
    site_url: Optional[str] = Query(default=None),
    property_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    site = (site_url or SEARCH_CONSOLE_SITE_URL).strip()
    prop = (property_id or GA4_PROPERTY_ID).replace("properties/", "").strip()

    if not site:
        raise HTTPException(status_code=503, detail="SEARCH_CONSOLE_SITE_URL is not configured")
    if not prop:
        raise HTTPException(status_code=503, detail="GA4_PROPERTY_ID is not configured")

    current_start, current_end = _period_dates(days, 0)
    previous_start, previous_end = _period_dates(days, 1)

    sc_current = _sc_totals(await _sc_report(site, current_start, current_end, None, 1))
    sc_previous = _sc_totals(await _sc_report(site, previous_start, previous_end, None, 1))

    ga_metrics = ["sessions", "totalUsers", "newUsers", "keyEvents", "screenPageViews"]
    ga_current = _ga_metric_map(await _ga_report(prop, current_start, current_end, metrics=ga_metrics, limit=1))
    ga_previous = _ga_metric_map(await _ga_report(prop, previous_start, previous_end, metrics=ga_metrics, limit=1))

    def comparison(current, previous):
        keys = sorted(set(current) | set(previous))
        return {
            key: {
                "current": round(_safe_float(current.get(key)), 4),
                "previous": round(_safe_float(previous.get(key)), 4),
                "changePct": _pct_change(current.get(key), previous.get(key)),
            }
            for key in keys
        }

    return {
        "days": days,
        "currentPeriod": {"start": current_start, "end": current_end},
        "previousPeriod": {"start": previous_start, "end": previous_end},
        "searchConsole": comparison(sc_current, sc_previous),
        "analytics": comparison(ga_current, ga_previous),
    }


@router.get("/marketing/overview")
async def marketing_overview(
    days: int = Query(default=28, ge=7, le=180),
    site_url: Optional[str] = Query(default=None),
    property_id: Optional[str] = Query(default=None),
    opportunity_limit: int = Query(default=10, ge=1, le=50),
    _: bool = Depends(_check_bridge_token),
):
    site = (site_url or SEARCH_CONSOLE_SITE_URL).strip()
    prop = (property_id or GA4_PROPERTY_ID).replace("properties/", "").strip()

    if not site:
        raise HTTPException(status_code=503, detail="SEARCH_CONSOLE_SITE_URL is not configured")
    if not prop:
        raise HTTPException(status_code=503, detail="GA4_PROPERTY_ID is not configured")

    start, end = _default_dates(days)

    sc_total_payload = await _sc_report(site, start, end, None, 1)
    sc_queries_payload = await _sc_report(site, start, end, ["query"], 250)
    sc_pages_payload = await _sc_report(site, start, end, ["page"], 100)

    ga_totals_payload = await _ga_report(
        prop, start, end,
        metrics=["sessions", "totalUsers", "newUsers", "screenPageViews", "engagedSessions", "keyEvents"],
        limit=1,
    )
    ga_channels_payload = await _ga_report(
        prop, start, end,
        dimensions=["sessionDefaultChannelGroup"],
        metrics=["sessions", "totalUsers", "newUsers", "keyEvents"],
        limit=50,
    )

    sc_totals = _sc_totals(sc_total_payload)
    ga_totals = _ga_metric_map(ga_totals_payload)
    channels = _ga_channel_rows(ga_channels_payload)

    organic = next((row for row in channels if row.get("channel") == "Organic Search"), {})
    paid_search = next((row for row in channels if row.get("channel") == "Paid Search"), {})
    paid_social = next((row for row in channels if row.get("channel") == "Paid Social"), {})

    query_rows = []
    for row in _sc_rows(sc_queries_payload):
        key = (row.get("keys") or [""])[0]
        query_rows.append({
            "query": key,
            "clicks": _safe_int(row.get("clicks")),
            "impressions": _safe_int(row.get("impressions")),
            "ctr": round(_safe_float(row.get("ctr")), 4),
            "position": round(_safe_float(row.get("position")), 2),
        })

    quick_wins = [row for row in query_rows if row["impressions"] >= 20 and 3 <= row["position"] <= 12]
    quick_wins.sort(
        key=lambda x: x["impressions"] * max(0.01, 0.12 - x["ctr"]) * (13 - x["position"]),
        reverse=True,
    )

    page_rows = []
    for row in _sc_rows(sc_pages_payload):
        page_rows.append({
            "page": (row.get("keys") or [""])[0],
            "clicks": _safe_int(row.get("clicks")),
            "impressions": _safe_int(row.get("impressions")),
            "ctr": round(_safe_float(row.get("ctr")), 4),
            "position": round(_safe_float(row.get("position")), 2),
        })

    page_opportunities = [row for row in page_rows if row["impressions"] >= 100 and row["ctr"] <= 0.03]
    page_opportunities.sort(key=lambda x: x["impressions"], reverse=True)

    ads = None
    ads_status = "not configured"
    if GOOGLE_ADS_CUSTOMER_ID and GOOGLE_ADS_DEVELOPER_TOKEN:
        ads_status = "configured; API access may still be pending"
        try:
            ads_query = f"""
                SELECT
                  campaign.name,
                  campaign.status,
                  metrics.impressions,
                  metrics.clicks,
                  metrics.ctr,
                  metrics.cost_micros,
                  metrics.conversions,
                  metrics.conversions_value
                FROM campaign
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                ORDER BY metrics.cost_micros DESC
            """
            ads = await _ads_search(ads_query)
            ads_status = "ok"
        except HTTPException as exc:
            ads_status = str(exc.detail)

    return {
        "days": days,
        "dateRange": {"start": start, "end": end},
        "searchConsole": sc_totals,
        "analytics": {key: round(value, 4) for key, value in ga_totals.items()},
        "channelHighlights": {
            "organicSearch": organic,
            "paidSearch": paid_search,
            "paidSocial": paid_social,
        },
        "seoQuickWins": quick_wins[:opportunity_limit],
        "pageOpportunities": page_opportunities[:opportunity_limit],
        "adsStatus": ads_status,
        "ads": ads,
        "notes": [
            "Search Console clicks and GA4 Organic Search sessions are different measurements and should not be expected to match exactly.",
            "Booking attribution is not claimed here because the current InflatableOffice bridge does not yet prove a reliable source/UTM relationship for each booking.",
        ],
    }


@router.get("/marketing-summary")
async def marketing_summary(
    days: int = Query(default=28, ge=1, le=365),
    _: bool = Depends(_check_bridge_token),
):
    output = {
        "days": days,
        "searchConsole": None,
        "analytics": None,
        "ads": None,
        "reviews": None,
        "errors": {},
    }

    async def capture(name, coro):
        try:
            output[name] = await coro
        except HTTPException as exc:
            output["errors"][name] = exc.detail

    start, end = _default_dates(days)

    if SEARCH_CONSOLE_SITE_URL:
        await capture("searchConsole", _sc_report(SEARCH_CONSOLE_SITE_URL, start, end, ["query"], 50))

    if GA4_PROPERTY_ID:
        await capture(
            "analytics",
            _ga_report(
                GA4_PROPERTY_ID,
                start,
                end,
                dimensions=["sessionDefaultChannelGroup"],
                metrics=["sessions", "totalUsers", "newUsers", "keyEvents"],
                limit=100,
            ),
        )

    if GOOGLE_ADS_CUSTOMER_ID and GOOGLE_ADS_DEVELOPER_TOKEN:
        query = f"""
            SELECT
              campaign.id,
              campaign.name,
              campaign.status,
              metrics.impressions,
              metrics.clicks,
              metrics.ctr,
              metrics.cost_micros,
              metrics.conversions,
              metrics.conversions_value
            FROM campaign
            WHERE segments.date BETWEEN '{start}' AND '{end}'
            ORDER BY metrics.cost_micros DESC
        """
        await capture("ads", _ads_search(query))

    if GOOGLE_BUSINESS_ACCOUNT_ID and GOOGLE_BUSINESS_LOCATION_ID:
        aid = GOOGLE_BUSINESS_ACCOUNT_ID.replace("accounts/", "")
        lid = GOOGLE_BUSINESS_LOCATION_ID.replace("locations/", "")
        await capture(
            "reviews",
            _google_request(
                "GET",
                f"https://mybusiness.googleapis.com/v4/accounts/{aid}/locations/{lid}/reviews",
                params={"pageSize": 20, "orderBy": "updateTime desc"},
            ),
        )

    return output
