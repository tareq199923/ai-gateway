# invincible/endpoints/anthropic_compat.py
"""Anthropic Messages API compatibility endpoint (POST /v1/messages).

Translates Anthropic requests into Invincible's internal message model,
hands them to the existing Router, and translates responses back to
Anthropic format. The Router is never modified and never becomes aware that
the client spoke Anthropic; sessions are shared with the OpenAI endpoint
because both protocols persist the same internal message format.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from invincible.compat.anthropic import (
    anthropic_to_internal,
    build_error,
    build_stream_events,
    internal_to_anthropic,
)
from invincible.compat.common import build_message, estimate_token_sum
from invincible.core.router import AllProvidersFailedError, UpstreamClientError
from invincible.models.anthropic import AnthropicMessagesRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _error_message(status_code: int, message: str) -> JSONResponse:
    status, body = build_error(status_code, message)
    return JSONResponse(content=body, status_code=status)


async def _persist(store, session_id, full_messages, content: str):
    try:
        await store.save(
            session_id, full_messages + [build_message("assistant", content)]
        )
    except Exception:
        logger.exception("Failed to persist session history for %s", session_id)


@router.post("/v1/messages")
async def anthropic_messages(request: Request, body: AnthropicMessagesRequest):
    session_id = request.headers.get("X-Session-Id", "default")
    store = request.app.state.sessions

    try:
        internal_messages = anthropic_to_internal(body.messages, body.system)
    except ValueError as e:
        return _error_message(400, str(e))

    history = await store.load(session_id)
    full_messages = history + internal_messages
    input_tokens = estimate_token_sum(full_messages)

    if body.stream:
        try:
            first, tail = await request.app.state.router.stream_open(full_messages)
        except UpstreamClientError as e:
            return _error_message(e.status_code, "Upstream request failed")
        except AllProvidersFailedError:
            return _error_message(503, "All providers failed or are in cooldown.")

        async def save_complete(accumulated: str):
            await _persist(store, session_id, full_messages, accumulated)

        return StreamingResponse(
            build_stream_events(
                first, tail, body.model, input_tokens, on_complete=save_complete
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        result = await request.app.state.router.route_request(full_messages)
    except UpstreamClientError as e:
        return _error_message(e.status_code, "Upstream request failed")
    except AllProvidersFailedError:
        return _error_message(503, "All providers failed or are in cooldown.")
    except Exception:
        logger.exception("Unexpected error during Anthropic completion")
        return _error_message(500, "Internal server error")

    choices = result.get("choices") or []
    if choices and "message" in choices[0]:
        content = choices[0]["message"].get("content") or ""
        await _persist(store, session_id, full_messages, content)

    anthropic_response = internal_to_anthropic(result, body.model, input_tokens)
    return JSONResponse(content=anthropic_response)
