"""
Homelab structured logging module — drop this directory into any Python project.

Usage:
    from homelab_logging import setup_logging, get_logger
    from homelab_logging.config import LoggingConfig

    setup_logging(LoggingConfig(project="cardvision", service="api"))
    logger = get_logger(__name__)
    logger.info("started", version="1.0")
"""
import logging
import structlog

from .config import LoggingConfig
from .context import correlation_id_var, session_id_var
from .middleware import CorrelationMiddleware

__all__ = ["setup_logging", "get_logger", "CorrelationMiddleware", "LoggingConfig"]

_config: LoggingConfig | None = None


def _inject_request_context(logger, method_name, event_dict):
    """structlog processor: pull correlation_id and session_id from contextvars."""
    corr = correlation_id_var.get("")
    sess = session_id_var.get("")
    if corr:
        event_dict["correlation_id"] = corr
    if sess:
        event_dict["session_id"] = sess
    return event_dict


def setup_logging(config: LoggingConfig | None = None) -> None:
    global _config
    _config = config or LoggingConfig()

    shared_processors: list = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
        _inject_request_context,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if _config.render_json
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_config.log_level.upper())


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if _config is None:
        raise RuntimeError("Call setup_logging() before get_logger()")
    return structlog.get_logger(name).bind(
        host_id=_config.host_id,
        project=_config.project,
        service=_config.service,
    )
