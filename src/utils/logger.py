"""Nexus AI — Structured logging with rotating file output."""
from __future__ import annotations
import logging, sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
import structlog

def setup_logging(level: str = "INFO") -> None:
    root = Path(__file__).parent.parent.parent
    logs_dir = root / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # File handler: JSON logs, 10MB max, 5 backups
    file_handler = RotatingFileHandler(
        logs_dir / "nexus.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)

    # Agent-specific debug file
    agent_handler = RotatingFileHandler(
        logs_dir / "agents.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
    )
    agent_handler.setLevel(logging.DEBUG)

    # Audit file (security events only)
    audit_handler = RotatingFileHandler(
        logs_dir / "security.log", maxBytes=5*1024*1024, backupCount=10, encoding="utf-8"
    )
    audit_handler.setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(message)s")
    console.setFormatter(fmt)
    file_handler.setFormatter(fmt)
    agent_handler.setFormatter(fmt)
    audit_handler.setFormatter(fmt)

    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)

    # Agent logger
    agent_logger = logging.getLogger("agent")
    agent_logger.addHandler(agent_handler)

    # Audit/security logger
    audit_logger = logging.getLogger("audit")
    audit_logger.addHandler(audit_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
