import os
import httpx
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI(
    title="Callahan InflatableOffice Bridge",
    version="1.1.0"
)

IO_API_KEY = os.getenv("INFLATABLE_OFFICE_API_KEY")
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")
IO_BASE = "https://rental.software/api6"

security = HTTPBearer()


def check_token(
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    if not BRIDGE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="BRIDGE_TOKEN is not configured"
        )

    if credentials.credentials != BRIDGE_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )

    return True


async def io_get(path: str, params: dict | None = None):
    if not IO_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="INFLATABLE_OFFICE_API_KEY is not configured"
        )

    params = params or {}
    params["apiKey"] = IO_API_KEY

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{IO_BASE}/{path.lstrip('/')}",
            params=params
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text
        )

    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


@app.get("/")
def root():
    return {
        "service": "Callahan InflatableOffice Bridge",
        "status": "running"
    }


@app.get("/health")
async def health(_: bool = Depends(check_token)):
    return {
        "status": "ok",
        "bridge": "running",
        "inflatable_office_key_configured": bool(IO_API_KEY)
    }


@app.get("/rentals")
async def rentals(_: bool = Depends(check_token)):
    return await io_get("rentals/")


@app.get("/workers")
async def workers(_: bool = Depends(check_token)):
    return await io_get("workers/")


@app.get("/vehicles")
async def vehicles(_: bool = Depends(check_token)):
    return await io_get("vehicles/")


@app.get("/categories")
async def categories(_: bool = Depends(check_token)):
    return await io_get("categories/")


@app.get("/locations")
async def locations(_: bool = Depends(check_token)):
    return await io_get("locations/")


@app.get("/leads")
async def leads(_: bool = Depends(check_token)):
    return await io_get(
        "leads/",
        {"_body": "true"}
    )


@app.get("/leads/{lead_id}")
async def lead_detail(
    lead_id: str,
    _: bool = Depends(check_token)
):
    return await io_get(
        f"leads/{lead_id}/",
        {"_body": "true"}
    )
