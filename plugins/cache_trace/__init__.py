"""Cache-trace observability plugin (opt-in, metadata-only).

Consumes the existing per-API lifecycle hooks (``pre_api_request``,
``post_api_request``, ``api_request_error``) and appends allowlisted,
content-free records to a bounded rotated JSONL file under this profile's
``logs/cache-trace/``. Never receives or stores prompt text, tool arguments,
response content, credentials or exception messages; never mutates the
request; fails open (any I/O problem is logged and dropped, the API call is
unaffected). See ``README.md``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:  # noqa: ANN001 - framework-provided context
    from plugins.cache_trace.collector import (
        on_api_request_error, on_post_api_request, on_pre_api_request,
    )

    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        logger.warning("cache_trace: plugin context has no register_hook; not registered")
        return
    register_hook("pre_api_request", on_pre_api_request)
    register_hook("post_api_request", on_post_api_request)
    register_hook("api_request_error", on_api_request_error)
    logger.info("cache_trace: registered pre/post/error API-request hooks (metadata-only)")
