import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request

from invincible.core.router import Router
from invincible.core.session_store import SessionStore
from invincible.endpoints.mcp import require_mcp_auth
from invincible.endpoints.mcp import router as mcp_router
from invincible.endpoints.openai_compat import router as openai_router

load_dotenv()

logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.router = Router(config_path=os.getenv("INVINCIBLE_CONFIG_PATH"))
    app.state.sessions = SessionStore(db_path=os.getenv("INVINCIBLE_DB_PATH"))
    await app.state.sessions.init()
    yield
    await app.state.router.close()
    await app.state.sessions.close()

app = FastAPI(title="Invincible", lifespan=lifespan)

async def require_auth(request: Request):
    gateway_key = os.getenv("GATEWAY_API_KEY")
    if not gateway_key:
        return
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Missing authentication token",
                    "type": "auth_error",
                }
            },
        )
    token = auth.removeprefix("Bearer ")
    if token != gateway_key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Invalid authentication token",
                    "type": "auth_error",
                }
            },
        )

app.include_router(openai_router, dependencies=[Depends(require_auth)])
app.include_router(mcp_router, dependencies=[Depends(require_mcp_auth)])

@app.get("/")
def health_check():
    return {"status": "healthy"}
