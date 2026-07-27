# gateway/endpoints/openai_compat.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from gateway.core.router import Router

router = APIRouter()
router_instance = Router()

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_json = await request.json()
    messages = req_json.get("messages", [])
    
    try:
        result = await router_instance.route_request(messages)
        return JSONResponse(content=result)
    except Exception as e:
        return JSONResponse(
            content={"error": {"message": str(e), "type": "gateway_error"}},
            status_code=503
        )