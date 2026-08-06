# invincible/compat/anthropic.py
"""Pure translation helpers for the Anthropic Messages API.

Converts between the Anthropic wire format and Invincible's internal message
model - nothing more. This module must not import FastAPI or the Router; the
endpoint wires the two together.

Internal message model (shared with the OpenAI compatibility layer):

    [{"role": "system" | "user" | "assistant", "content": str}, …]
"""
import json
import logging
import uuid
from collections.abc import (  # noqa: F401  (AsyncGenerator re-exported for type hints)
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
)

from invincible.compat.common import (
    build_message,
    build_usage,
    estimate_token_sum,
)

logger = logging.getLogger(__name__)

# HTTP status -> Anthropic error type. Anything unmapped becomes api_error.
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    503: "overloaded_error",
}

# OpenAI finish_reason -> Anthropic stop_reason.
FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}

DEFAULT_STOP_REASON = "end_turn"


def translate_finish_reason(finish_reason: str | None) -> str:
    """Map an OpenAI finish reason to the Anthropic stop_reason vocabulary.

    Unknown or missing reasons map to ``end_turn`` so a stream always closes
    with a valid stop reason. New mappings (``pause_turn``, …) can be added
    here without touching endpoint code.
    """
    if finish_reason is None:
        return DEFAULT_STOP_REASON
    return FINISH_REASON_MAP.get(finish_reason, DEFAULT_STOP_REASON)


def flatten_content_blocks(content, role: str) -> str:
    """Flatten an Anthropic ``content`` value into plain text.

    Handles both shapes Anthropic clients send: a plain string, or a list of
    content blocks (``text``, ``tool_use``, ``tool_result``, …).

    Text blocks are concatenated; ``tool_result`` blocks contribute their
    text; ``tool_use`` blocks become a compact placeholder tag so tool
    context survives the round trip without pretending to execute tools.
    Unsupported block types are skipped. Tool-related blocks are never
    silently discarded - they degrade to text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if text:
                parts.append(str(text))
        elif block_type == "tool_result":
            result = block.get("content", "")
            parts.append(flatten_content_blocks(result, role))
        elif block_type == "tool_use":
            name = block.get("name") or "tool"
            parts.append(f"[tool_use: {name}]")
    return "".join(parts)


def anthropic_to_internal(messages: list, system=None) -> list:
    """Translate an Anthropic Messages request into internal messages.

    ``system`` may be a string or a list of text blocks; it becomes a
    leading ``system`` message (the Router always keeps system messages).
    ``messages[]`` may also contain ``role == "system"`` entries (which
    Claude Code sends); they are converted into internal ``system`` messages
    exactly like the top-level ``system`` field, contributing their
    flattened text. Content is flattened with
    :func:`flatten_content_blocks`. Messages that flatten to nothing are
    skipped; a request with no usable text raises ``ValueError`` so the
    endpoint can answer with an Anthropic ``invalid_request_error``.
    """
    internal: list = []

    if system is not None:
        system_text = flatten_content_blocks(system, "system")
        if system_text:
            internal.append(build_message("system", system_text))

    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Each message must be an object")
        role = message.get("role")
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"Unsupported message role: {role!r}")
        content = flatten_content_blocks(message.get("content", ""), role)
        if not content:
            continue
        internal.append(build_message(role, content))

    if not internal:
        raise ValueError("Request contains no usable text content")
    return internal


def _message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def build_message_skeleton(message_id: str, model: str, input_tokens: int) -> dict:
    """The Anthropic ``message`` skeleton used by ``message_start`` events."""
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": build_usage(input_tokens, 0),
    }


def internal_to_anthropic(
    openai_body: dict, requested_model: str | None, input_tokens: int
) -> dict:
    """Translate an internal (OpenAI-shaped) Router response into an
    Anthropic Messages response.

    The ``model`` field echoes the client's model hint; it never influences
    routing and never requires the provider to expose Claude model names.
    ``usage`` token counts are estimates (the Router's own heuristic) since
    upstream responses may omit usage entirely.
    """
    choices = openai_body.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message") or {}
    content = message.get("content") or ""

    output_tokens = estimate_token_sum([build_message("assistant", content)])
    model = requested_model or openai_body.get("model") or "invincible"

    return {
        "id": _message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": translate_finish_reason(first_choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": build_usage(input_tokens, output_tokens),
    }


def build_error(status_code: int, message: str) -> tuple[int, dict]:
    """Build an Anthropic-compatible error response.

    Returns ``(http_status, body)`` where the body is always:

        {"type": "error", "error": {"type": <mapped>, "message": <msg>}}

    The message is the caller's (sanitized) text; upstream provider error
    bodies are never forwarded verbatim.
    """
    error_type = ERROR_TYPE_BY_STATUS.get(status_code, "api_error")
    return status_code, {
        "type": "error",
        "error": {"type": error_type, "message": message},
    }


def sse_frame(event: str, data: dict) -> str:
    """Render one Anthropic SSE event (``event:`` + ``data:`` lines)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _delta_piece(chunk: dict) -> str:
    """The text delta carried by one OpenAI stream chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("delta") or {}).get("content") or ""


def _delta_finish(chunk: dict) -> str | None:
    """The finish_reason carried by one OpenAI stream chunk."""
    choices = chunk.get("choices") or []
    if not choices:
        return None
    return choices[0].get("finish_reason")


async def _complete(
    on_complete: Callable[[str], Awaitable[None]] | None, text: str
) -> None:
    if on_complete is not None:
        await on_complete(text)


async def build_stream_events(
    first: dict | None,
    tail: AsyncIterator[dict],
    requested_model: str | None,
    input_tokens: int,
    on_complete: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncGenerator[str, None]:
    """Wrap the Router's OpenAI stream into Anthropic SSE events.

    Yields pre-formatted frames in the canonical Anthropic order:

        message_start → content_block_start → content_block_delta*
        → content_block_stop → message_delta → message_stop

    ``on_complete`` (if given) is awaited exactly once with the accumulated
    reply text - on success *and* on a mid-stream failure - so the caller
    can persist the session once. A mid-stream upstream failure emits a
    well-formed ``error`` event and stops; the stream never emits malformed
    SSE and always closes.
    """
    message_id = _message_id()
    model = requested_model or "invincible"
    reply_text = ""
    finish_reason = None

    yield sse_frame(
        "message_start",
        {
            "type": "message_start",
            "message": build_message_skeleton(message_id, model, input_tokens),
        },
    )
    yield sse_frame(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )

    try:
        if first is not None:
            piece = _delta_piece(first)
            if piece:
                reply_text += piece
                yield sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
        async for chunk in tail:
            piece = _delta_piece(chunk)
            finish_reason = _delta_finish(chunk)
            if piece:
                reply_text += piece
                yield sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
    except Exception as e:
        logger.warning("Anthropic stream terminated after an upstream error: %s", e)
        yield sse_frame(
            "error",
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Stream terminated unexpectedly",
                },
            },
        )
        await _complete(on_complete, reply_text)
        return

    yield sse_frame(
        "content_block_stop",
        {"type": "content_block_stop", "index": 0},
    )
    yield sse_frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": translate_finish_reason(finish_reason),
                "stop_sequence": None,
            },
            "usage": build_usage(
                input_tokens,
                estimate_token_sum([build_message("assistant", reply_text)]),
            ),
        },
    )
    yield sse_frame("message_stop", {"type": "message_stop"})
    await _complete(on_complete, reply_text)
