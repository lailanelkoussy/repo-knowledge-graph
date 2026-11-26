"""
Optional Langfuse tracing.

Tracing is enabled only when the `langfuse` package is installed and LANGFUSE_PUBLIC_KEY /
LANGFUSE_SECRET_KEY are set; otherwise every helper is a no-op, so the MCP server starts
without any observability configuration.
"""
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_client = None
_observe = None
_initialised = False


def _init() -> None:
    global _client, _observe, _initialised
    if _initialised:
        return
    _initialised = True
    if not (os.getenv('LANGFUSE_PUBLIC_KEY') and os.getenv('LANGFUSE_SECRET_KEY')):
        return
    try:
        from langfuse import get_client, observe
    except ImportError:
        logger.info('LANGFUSE_* variables set but langfuse is not installed: tracing disabled')
        return
    try:
        _client = get_client()
        _observe = observe
        logger.info('Langfuse tracing enabled')
    except Exception as e:  # never prevent the server from starting
        logger.warning(f'Langfuse initialisation failed, tracing disabled: {e}')


def tracing_enabled() -> bool:
    _init()
    return _client is not None


def observe_tool(name: str) -> Callable:
    """Decorator tracing an async MCP tool as a Langfuse 'tool' observation (no-op when disabled)."""
    def decorator(fn: Callable) -> Callable:
        _init()
        if _observe is None:
            return fn
        return _observe(name=name, as_type='tool')(fn)
    return decorator


def annotate_current(metadata: dict) -> None:
    """Attach metadata (arguments, response size, error type...) to the current observation."""
    if not tracing_enabled() or _client is None:
        return
    try:
        _client.update_current_span(metadata=metadata)
    except Exception:
        pass


def flush() -> None:
    if tracing_enabled() and _client is not None:
        try:
            _client.flush()
        except Exception:
            pass


def response_size(result: Any) -> Optional[int]:
    try:
        import json
        return len(json.dumps(result, default=str))
    except Exception:
        return None
