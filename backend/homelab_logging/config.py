from dataclasses import dataclass, field
import os


@dataclass
class LoggingConfig:
    host_id: str = field(default_factory=lambda: os.getenv("HOST_ID", "unknown"))
    project: str = field(default_factory=lambda: os.getenv("LOG_PROJECT", "unknown"))
    service: str = field(default_factory=lambda: os.getenv("LOG_SERVICE", "unknown"))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    render_json: bool = field(
        default_factory=lambda: os.getenv("LOG_FORMAT", "json").lower() != "console"
    )
