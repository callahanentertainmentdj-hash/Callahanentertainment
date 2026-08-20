from main import app
from google_oauth_start import router as google_oauth_start_router
from google_hub import router as google_router

# Public OAuth starter. The callback and protected Google data routes live in google_hub.py.
app.include_router(google_oauth_start_router)

# Keep the existing InflatableOffice bridge intact and layer Google marketing endpoints on top.
app.include_router(google_router)
app.title = "Callahan Entertainment AI Hub"
app.version = "3.8.0"

# GPT Actions requires an explicit absolute server URL in the OpenAPI schema.
app.servers = [{"url": "https://callahanentertainment.onrender.com"}]
app.openapi_schema = None

@app.get("/openapi-operations.json", include_in_schema=False)
async def operations_openapi_schema():
    """GPT Actions schema limited to InflatableOffice operations and inventory."""
    schema = dict(app.openapi())
    schema["servers"] = [{"url": "https://callahanentertainment.onrender.com"}]
    schema["paths"] = {
        path: operations
        for path, operations in schema.get("paths", {}).items()
        if path != "/" and not path.startswith(("/google/", "/admin/"))
    }
    schema["info"] = {
        **schema.get("info", {}),
        "title": "Callahan Entertainment Operations and Inventory",
    }
    return schema

@app.get("/openapi-google.json", include_in_schema=False)
async def google_openapi_schema():
    """GPT Actions schema limited to protected Google marketing operations."""
    schema = dict(app.openapi())
    schema["servers"] = [{"url": "https://callahanentertainment.onrender.com"}]
    schema["paths"] = {
        path: operations
        for path, operations in schema.get("paths", {}).items()
        if path.startswith("/google/") and not path.startswith("/google/oauth/")
    }
    schema["info"] = {
        **schema.get("info", {}),
        "title": "Callahan Entertainment Google Marketing",
    }
    return schema

@app.get("/openapi-combined.json", include_in_schema=False)
async def combined_openapi_schema():
    """One GPT Actions schema combining operations and Google marketing."""
    selected_paths = {
        "/health",
        "/rentals",
        "/inventory",
        "/inventory/idle",
        "/inventory/categories",
        "/inventory/item",
        "/status-events",
        "/status-summary",
        "/weekend-collections",
        "/collections-range",
        "/staffing",
        "/staffing-range",
        "/weekend-operations",
        "/public/weekend-loadout",
        "/public/day-loadout",
        "/public/range-loadout",
        "/public/weekend-cleaning",
        "/public/inflatable-next-use",
        "/public/schedule",
        "/google/status",
        "/google/search-console/performance",
        "/google/search-console/summary",
        "/google/search-console/opportunities",
        "/google/analytics/report",
        "/google/analytics/overview",
        "/google/ads/campaigns",
        "/google/ads/search-terms",
        "/google/ads/keywords",
        "/google/reviews",
        "/google/marketing-summary",
    }
    schema = dict(app.openapi())
    schema["servers"] = [{"url": "https://callahanentertainment.onrender.com"}]
    schema["paths"] = {
        path: operations
        for path, operations in schema.get("paths", {}).items()
        if path in selected_paths
    }
    schema["info"] = {
        **schema.get("info", {}),
        "title": "Callahan Entertainment Operations and Google Marketing",
    }
    return schema
