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
