from main import app
from google_oauth_start import router as google_oauth_start_router
from google_hub import router as google_router

# Public OAuth starter. The callback and protected Google data routes live in google_hub.py.
app.include_router(google_oauth_start_router)

# Keep the existing InflatableOffice bridge intact and layer Google marketing endpoints on top.
app.include_router(google_router)
app.title = "Callahan Entertainment AI Hub"
app.version = "3.8.0"
