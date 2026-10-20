"""Console logging for the app's own loggers (everything under "app").

Uvicorn keeps its own access and error loggers. Third-party loggers stay at
WARNING so request bodies and prompts from the SDKs never reach the log."""

import logging
import logging.config
from contextvars import ContextVar

# A short tag such as "repo 3" or "msg 12", set per ingest task or per turn so
# concurrent runs can be told apart in the log.
log_context: ContextVar[str] = ContextVar("log_context", default="")


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tag = log_context.get()
        record.ctx = f" [{tag}]" if tag else ""
        return True


def setup_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": _ContextFilter}},
            "formatters": {
                "console": {
                    "format": "%(asctime)s.%(msecs)03d %(levelname)-7s "
                    "%(name)s%(ctx)s  %(message)s",
                    "datefmt": "%H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "console",
                    "filters": ["context"],
                }
            },
            "loggers": {
                "app": {
                    "handlers": ["console"],
                    "level": level.upper(),
                    "propagate": False,
                }
            },
        }
    )


def preview(text: str, limit: int = 80) -> str:
    """A one-line, shortened copy of user text for log lines."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
