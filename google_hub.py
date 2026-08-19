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
GOOGLE_OAUTH_STATE_SECRET = os.getenv("GOOGLE_OAUTH_STATE_SECRET", BRIDGE_TOKEN).strip()

GA4_PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "").replace("properties/", "").strip()
SEARCH_CONSOLE_SITE_URL = os.getenv("SEARCH_CONSOLE_SITE_URL", "").strip()
GOOGLE_ADS_CUSTOMER_ID = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "").strip()
GOOGLE_ADS_LOGIN_CUSTOMER_ID = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").replace("-", "").strip()
GOOGLE_ADS_DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
GOOGLE_ADS_API_VERSION = os.getenv("GOOGLE_ADS_API_VERSION", "v25").strip()
GOOGLE_BUSINESS_ACCOUNT_ID = os.getenv("GOOGLE_BUSINESS_ACCOUNT_ID", "").strip()
GOOGLE_BUSINESS_LOCATION_ID = os.getenv("GOOGLE_BUSINESS_LOCATION_ID", "").strip()

_access_token_cache = {"token": None, "expires": 0.0}


def _check_bridge_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
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
        raise HTTPException(status_code=400, detail="OAuth state expired; start again")
    expected_sig = hmac.new(
        GOOGLE_OAUTH_STATE_SECRET.encode(), stamp_text.encode(), hashlib.sha256
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
                "Google OAuth is not fully connected. Visit /google/oauth/start, authorize Google, "
                "then save the returned refresh token in Render as GOOGLE_REFRESH_TOKEN."
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
        raise HTTPException(status_code=502, detail=f"Google token refresh failed (HTTP {response.status_code})")
    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Google did not return an access token")
    _access_token_cache["token"] = access_token
    _access_token_cache["expires"] = now + int(payload.get("expires_in", 3600))
    return access_token


async def _google_request(method: str, url: str, *, json=None, params=None, headers=None):
    access_token = await _exchange_refresh_token()
    merged_headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.request(method, url, json=json, params=params, headers=merged_headers)
    if response.status_code >= 400:
        message = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error", {})
                message = error.get("message", "") if isinstance(error, dict) else str(error)
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Google API returned HTTP {response.status_code}" + (f": {message}" if message else ""),
        )
    return response.json() if response.content else {}


def _default_dates(days: int):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=max(days - 1, 0))
    return str(start), str(end)


def _csv(value: str):
    return [part.strip() for part in value.split(",") if part.strip()]


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
    url = f"https://googleads.googleapis.com/{GOOGLE_ADS_API_VERSION}/customers/{cid}/googleAds:search"
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(url, headers=headers, json={"query": query, "pageSize": 1000})
    if response.status_code >= 400:
        message = ""
        try:
            body = response.json()
            message = body.get("error", {}).get("message", "")
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Google Ads returned HTTP {response.status_code}" + (f": {message}" if message else ""),
        )
    return response.json()


@router.get("/oauth/callback", response_class=HTMLResponse, include_in_schema=False)
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
        return HTMLResponse(f"<h2>Google connection failed</h2><p>HTTP {response.status_code}</p>", status_code=502)
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
    dimensions: str = Query(default="query", description="Comma-separated: query,page,country,device,date,searchAppearance"),
    row_limit: int = Query(default=100, ge=1, le=25000),
    site_url: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    site = (site_url or SEARCH_CONSOLE_SITE_URL).strip()
    if not site:
        raise HTTPException(status_code=503, detail="SEARCH_CONSOLE_SITE_URL is not configured")
    dims = _csv(dimensions)
    allowed = {"query", "page", "country", "device", "date", "searchAppearance"}
    if not dims or any(d not in allowed for d in dims):
        raise HTTPException(status_code=400, detail=f"dimensions must use: {', '.join(sorted(allowed))}")
    start, end = _default_dates(days)
    url = "https://www.googleapis.com/webmasters/v3/sites/" + quote(site, safe="") + "/searchAnalytics/query"
    return await _google_request(
        "POST",
        url,
        json={"startDate": start, "endDate": end, "dimensions": dims, "rowLimit": row_limit},
    )


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
    url = "https://www.googleapis.com/webmasters/v3/sites/" + quote(site, safe="") + "/searchAnalytics/query"
    total = await _google_request("POST", url, json={"startDate": start, "endDate": end, "rowLimit": 1})
    queries = await _google_request("POST", url, json={"startDate": start, "endDate": end, "dimensions": ["query"], "rowLimit": 25})
    pages = await _google_request("POST", url, json={"startDate": start, "endDate": end, "dimensions": ["page"], "rowLimit": 25})
    return {"dateRange": {"start": start, "end": end}, "totals": total, "topQueries": queries, "topPages": pages}


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
    dims = [{"name": value} for value in _csv(dimensions)]
    mets = [{"name": value} for value in _csv(metrics)]
    if not mets:
        raise HTTPException(status_code=400, detail="At least one metric is required")
    payload = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "metrics": mets,
        "limit": str(limit),
    }
    if dims:
        payload["dimensions"] = dims
    return await _google_request(
        "POST",
        f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
        json=payload,
    )


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
    base = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
    totals = await _google_request(
        "POST", base,
        json={
            "dateRanges": [{"startDate": start, "endDate": end}],
            "metrics": [
                {"name": "sessions"}, {"name": "totalUsers"}, {"name": "newUsers"},
                {"name": "engagedSessions"}, {"name": "keyEvents"}, {"name": "totalRevenue"},
            ],
        },
    )
    channels = await _google_request(
        "POST", base,
        json={
            "dateRanges": [{"startDate": start, "endDate": end}],
            "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
            "limit": "50",
        },
    )
    pages = await _google_request(
        "POST", base,
        json={
            "dateRanges": [{"startDate": start, "endDate": end}],
            "dimensions": [{"name": "pagePath"}],
            "metrics": [{"name": "screenPageViews"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
            "limit": "50",
        },
    )
    return {"dateRange": {"start": start, "end": end}, "totals": totals, "channels": channels, "pages": pages}


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
          metrics.all_conversions
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.cost_micros DESC
    """
    return await _ads_search(query, customer_id)


@router.get("/ads/search-terms")
async def ads_search_terms(
    days: int = Query(default=28, ge=1, le=365),
    customer_id: Optional[str] = Query(default=None),
    _: bool = Depends(_check_bridge_token),
):
    start, end = _default_dates(days)
    query = f"""
        SELECT
          search_term_view.search_term,
          campaign.name,
          ad_group.name,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM search_term_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.clicks DESC
    """
    return await _ads_search(query, customer_id)


@router.get("/ads/keywords")
async def ads_keywords(
    days: int = Query(default=28, ge=1, le=365),
    customer_id: Optional[str] = Query(default=None),
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
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM keyword_view
        WHERE segments.date BETWEEN '{start}' AND '{end}'
        ORDER BY metrics.clicks DESC
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
        raise HTTPException(status_code=503, detail="GOOGLE_BUSINESS_ACCOUNT_ID and GOOGLE_BUSINESS_LOCATION_ID must be configured")
    return await _google_request(
        "GET",
        f"https://mybusiness.googleapis.com/v4/accounts/{aid}/locations/{lid}/reviews",
        params={"pageSize": page_size, "orderBy": "updateTime desc"},
    )


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
        url = "https://www.googleapis.com/webmasters/v3/sites/" + quote(SEARCH_CONSOLE_SITE_URL, safe="") + "/searchAnalytics/query"
        await capture(
            "searchConsole",
            _google_request("POST", url, json={"startDate": start, "endDate": end, "dimensions": ["query"], "rowLimit": 25}),
        )

    if GA4_PROPERTY_ID:
        await capture(
            "analytics",
            _google_request(
                "POST",
                f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runReport",
                json={
                    "dateRanges": [{"startDate": start, "endDate": end}],
                    "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                    "metrics": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
                    "limit": "100",
                },
            ),
        )

    if GOOGLE_ADS_CUSTOMER_ID and GOOGLE_ADS_DEVELOPER_TOKEN:
        query = f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.average_cpc, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
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
