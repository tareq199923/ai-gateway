# invincible/endpoints/openai_compat.py
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from invincible.core.router import UpstreamClientError


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    stream: bool | None = None

router = APIRouter()


def models_from_providers(providers: list) -> list[dict]:
    """Map the router's loaded providers to OpenAI /v1/models entries.

    The router validates providers at startup, so every entry normally has
    a ``model_id``; the isinstance/get guard is cheap defense in depth.
    Runtime provider order is preserved.
    """
    return [
        {"id": p["model_id"], "object": "model", "owned_by": "invincible"}
        for p in providers
        if isinstance(p, dict) and p.get("model_id")
    ]


@router.get("/v1/models")
async def list_models(request: Request):
    router = getattr(request.app.state, "router", None)
    if router is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "message": "Router not initialized",
                    "type": "config_error",
                }
            },
        )
    return {"object": "list", "data": models_from_providers(router.providers)}

@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    if body.stream:
        return JSONResponse(
            content={
                "error": {
                    "message": (
                        "Streaming is not currently supported by "
                        "this server."
                    ),
                    "type": "invalid_request_error",
                }
            },
            status_code=400
        )

    session_id = request.headers.get("X-Session-Id", "default")
    store = request.app.state.sessions

    history = await store.load(session_id)
    full_messages = history + body.messages

    try:
        result = await request.app.state.router.route_request(full_messages)
        choices = result.get("choices") or []
        if choices and "message" in choices[0]:
            await store.save(session_id, full_messages + [choices[0]["message"]])
        return JSONResponse(content=result)
    except UpstreamClientError as e:
        return JSONResponse(
            content=e.body,
            status_code=e.status_code
        )
    except Exception as e:
        return JSONResponse(
            content={"error": {"message": str(e), "type": "gateway_error"}},
            status_code=503
        )
