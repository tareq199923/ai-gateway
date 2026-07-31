from dotenv import load_dotenv
load_dotenv()

# gateway/main.py
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException
from gateway.endpoints.openai_compat import router as openai_router
from gateway.core.router import Router
from gateway.core.session_store import SessionStore

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.router = Router()
    app.state.sessions = SessionStore()
    await app.state.sessions.init()
    yield
    await app.state.router.close()
    await app.state.sessions.close()

app = FastAPI(title="AI Continuity Gateway", lifespan=lifespan)

async def require_auth(request: Request):
    gateway_key = os.getenv("GATEWAY_API_KEY")
    if not gateway_key:
        return
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing authentication token", "type": "auth_error"}},
        )
    token = auth.removeprefix("Bearer ")
    if token != gateway_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid authentication token", "type": "auth_error"}},
        )

app.include_router(openai_router, dependencies=[Depends(require_auth)])

@app.get("/")
def health_check():
    return {"status": "healthy"}