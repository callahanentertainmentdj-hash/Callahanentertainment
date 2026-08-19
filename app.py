from main import app
from google_hub import router as google_router

# Keep the existing InflatableOffice bridge intact and layer Google endpoints on top.
app.include_router(google_router)
app.title = "Callahan Entertainment AI Hub"
app.version = "3.6.0"
