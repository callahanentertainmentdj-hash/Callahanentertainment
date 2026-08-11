# Callahan InflatableOffice Bridge

Small read-only FastAPI bridge between ChatGPT/other clients and InflatableOffice API v6.

## Security
- Rotate the InflatableOffice API key that was pasted into chat before deployment.
- Put the new key only in the host's environment-variable/secret settings.
- Do not commit `.env` to GitHub.
- Keep the InflatableOffice API token read-only for Phase 1.
- Every bridge data endpoint requires `Authorization: Bearer <BRIDGE_TOKEN>`.

## Local run
1. Copy `.env.example` to `.env` and fill in both secrets.
2. `pip install -r requirements.txt`
3. `uvicorn main:app --reload`
4. Open `http://127.0.0.1:8000/docs`

## Render deployment
1. Create a private GitHub repository and upload these files.
2. In Render choose **New > Blueprint** and connect the repo. `render.yaml` configures the service.
3. Set `INFLATABLE_OFFICE_API_KEY` to the newly rotated IO token.
4. Render auto-generates `BRIDGE_TOKEN`; copy it from the service Environment page for your client configuration.
5. Deploy.
6. Test `/health` with the Authorization header.

Example:
```bash
curl https://YOUR-SERVICE.onrender.com/health \
  -H "Authorization: Bearer YOUR_BRIDGE_TOKEN"
```

## Endpoints
- `GET /health`
- `GET /leads?limit=25&body=true`
- `GET /leads/{lead_id}?body=true`
- `GET /rentals`
- `GET /workers`
- `GET /vehicles`
- `GET /categories`
- `GET /locations`

This bridge makes GET requests only. It contains no POST/PATCH/DELETE route for InflatableOffice.
