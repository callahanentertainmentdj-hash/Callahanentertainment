# Callahan Entertainment AI Hub

FastAPI bridge for Callahan Entertainment. It keeps the existing read-only InflatableOffice operational endpoints and adds protected Google marketing endpoints for Search Console, GA4, Google Ads, and Google Business Profile reviews.

## Security
- Keep all API keys, OAuth secrets, refresh tokens, and the bridge token in Render environment variables only.
- Do not commit a real `.env` file.
- Protected endpoints require `Authorization: Bearer <BRIDGE_TOKEN>`.
- InflatableOffice access remains read-only.

## Render
Render starts the application with:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

The public API documentation is available at `/docs` after deployment.

## InflatableOffice endpoints
Important operational endpoints include:
- `GET /health`
- `GET /leads`
- `GET /leads/{lead_id}`
- `GET /rentals`
- `GET /inventory?category=inflatables&history_days=90&future_days=90`
- `GET /inventory/idle?category=concessions&min_idle_days=30`
- `GET /inventory/categories?history_days=90&future_days=90`
- `GET /inventory/item?name=Melting%20Ice&history_days=180&future_days=180`
- `GET /status-events`
- `GET /status-summary`
- `GET /weekend-collections`
- `GET /collections-range`
- `GET /staffing`
- `GET /staffing-range`
- `GET /weekend-operations`
- `GET /public/weekend-loadout`
- `GET /public/day-loadout`
- `GET /public/range-loadout`
- `GET /public/weekend-cleaning`
- `GET /public/inflatable-next-use`
- `GET /public/schedule`
- `GET /public/schedule-range`

## Google connection
The OAuth starter is:
- `GET /google/oauth/start`

The OAuth callback is:
- `GET /google/oauth/callback`

After the refresh token is stored in Render, verify the connection with:
- `GET /google/status`

## Search Console
- `GET /google/search-console/sites`
- `GET /google/search-console/performance?days=28&dimensions=query`
- `GET /google/search-console/performance?days=28&dimensions=page`
- `GET /google/search-console/performance?days=28&dimensions=query,device`
- `GET /google/search-console/summary?days=28`

The performance endpoint supports these dimensions: `query`, `page`, `country`, `device`, `date`, and `searchAppearance`.

## Google Analytics 4
- `GET /google/analytics/overview?days=28`
- `GET /google/analytics/report?days=28&dimensions=sessionDefaultChannelGroup&metrics=sessions,totalUsers,newUsers,keyEvents`

The custom report endpoint passes supported GA4 dimension and metric names through to the Google Analytics Data API, making it possible to build more specific reports without changing Python code.

## Google Ads
- `GET /google/ads/customers`
- `GET /google/ads/campaigns?days=28`
- `GET /google/ads/search-terms?days=28`
- `GET /google/ads/keywords?days=28`

The Google Ads API version is controlled with `GOOGLE_ADS_API_VERSION` in Render rather than being permanently hard-coded into every endpoint.

## Google Business Profile
- `GET /google/business/accounts`
- `GET /google/business/locations`
- `GET /google/reviews`

## Combined marketing query
- `GET /google/marketing-summary?days=28`

This returns Search Console, GA4, Google Ads, and review data in a single protected request. Each source reports its own error so one unavailable Google product does not prevent the other sources from returning.

## Render environment variables
InflatableOffice/status:
- `INFLATABLE_OFFICE_API_KEY`
- `BRIDGE_TOKEN`
- `IO_BASE_URL`
- `CONFIRMED_STATUS_ID`
- `QUOTE_STATUS_ID`
- `CONTRACTED_STATUS_ID`
- `COMPLETE_STATUS_ID`

Google OAuth:
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_REDIRECT_URI`
- `GOOGLE_OAUTH_STATE_SECRET`

Google services:
- `GA4_PROPERTY_ID`
- `SEARCH_CONSOLE_SITE_URL`
- `GOOGLE_ADS_CUSTOMER_ID`
- `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
- `GOOGLE_ADS_DEVELOPER_TOKEN`
- `GOOGLE_ADS_API_VERSION`
- `GOOGLE_BUSINESS_ACCOUNT_ID`
- `GOOGLE_BUSINESS_LOCATION_ID`

Do not put live values into GitHub. Configure them in Render.
