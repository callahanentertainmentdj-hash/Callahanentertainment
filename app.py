from main import app
from google_oauth_start import router as google_oauth_start_router
from google_hub import router as google_router

# Register the clean OAuth start route first so it wins over the older token-protected path.
app.include_router(google_oauth_start_router)

# Keep the existing InflatableOffice bridge intact and layer Google endpoints on top.
app.include_router(google_router)
app.title = "Callahan Entertainment AI Hub"
app.version = "3.6.1"
