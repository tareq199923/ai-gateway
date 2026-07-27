# gateway/endpoints/openai_compat.py
from typing import Any
from pydantic import BaseModel
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from gateway.core.router import Router, UpstreamClientError

class ChatRequest(BaseModel):
    messages: list[dict[str, Any]]
    stream: bool | None = None

router = APIRouter()
router_instance = Router()

@router.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest):
    if body.stream:
        return JSONResponse(
            content={"error": {"message": "Streaming is not currently supported by this gateway.", "type": "invalid_request_error"}},
            status_code=400
        )
    
    try:
        result = await router_instance.route_request(body.messages)
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