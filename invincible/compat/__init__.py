# invincible/compat/__init__.py
"""Protocol compatibility layers.

Each protocol (Anthropic, future ones…) exposes a set of *pure* translation
helpers that convert between the client's wire format and Invincible's
internal message model. These modules must not depend on FastAPI or the
Router - they only translate data.
"""
