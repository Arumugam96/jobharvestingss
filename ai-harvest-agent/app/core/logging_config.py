"""Central logging setup — quiet console + full-detail rotating debug file.

Without this, structlog runs completely unconfigured: every ``.info()``/``.debug()``
call is printed unconditionally (structlog does not filter by level on its own),
so high-frequency polling routes (``GET /harvest-status/{id}``, ``GET /health``)
flood the terminal within seconds during a long-running harvest and any real
signal (e.g. a harvest-report email failure) scrolls out of view.

This installs:
  - console handler — renders at ``settings.log_level`` (default INFO), so
    routine polling noise (logged at DEBUG, see LoggingMiddleware) stays out.
  - rotating file handler (data/logs/app.log) — always DEBUG, so the complete
    step-by-step trace of any request/email flow is preserved on disk even
    when the console only shows the highlights.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "data" / "logs"
_LOG_FILE = _LOG_DIR / "app.log"

_configured = False

# Paths the frontend polls every few seconds. LoggingMiddleware (our own
# structlog request_started/request_finished) already drops these to DEBUG —
# but uvicorn ships its *own* separate access logger ("uvicorn.access") with
# its own handler, entirely outside that middleware, and it was still
# printing one "INFO: <ip> - "GET /harvest-status/... HTTP/1.1" 200 OK" line
# per poll. This filters those specific successful polls out of uvicorn's
# access log too, while leaving every other route — and any non-200 response
# on these same paths — visible.
_NOISY_ACCESS_PATHS = ("/harvest-status", "/health", "/run-history")


class _QuietPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if " 200 " not in msg:
            return True
        return not any(path in msg for path in _NOISY_ACCESS_PATHS)


def configure_logging(level: str = "INFO") -> None:
    """Idempotent — safe to call once at app startup."""
    global _configured
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level.upper())
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        )
    )

    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=10_000_000, backupCount=5, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
        )
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = [console_handler, file_handler]

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        cache_logger_on_first_use=True,
    )

    logging.getLogger("uvicorn.access").addFilter(_QuietPollingFilter())
