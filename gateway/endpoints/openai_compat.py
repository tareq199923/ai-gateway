# gateway/endpoints/openai_compat.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from gateway.core.router import Router, UpstreamClientError

router = APIRouter()
router_instance = Router()

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_json = await request.json()
    if req_json.get("stream"):
        return JSONResponse(
            content={"error": {"message": "Streaming is not currently supported by this gateway.", "type": "invalid_request_error"}},
            status_code=400
        )
    messages = req_json.get("messages", [])
    
    try:
        result = await router_instance.route_request(messages)
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