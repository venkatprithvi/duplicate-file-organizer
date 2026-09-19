from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .app_paths import app_data_dir

_LOGGER_NAME = "duplicate_file_organizer"

def log_path() -> Path:
    p = app_data_dir() / "logs" / "duplicate_file_organizer.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def get_logger(detailed: bool = True) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not getattr(logger, "_dfo_configured", False):
        handler = RotatingFileHandler(log_path(), maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S%z"))
        logger.addHandler(handler)
        logger._dfo_configured = True
    logger._dfo_detailed = detailed
    return logger

def configure_detailed(detailed: bool) -> logging.Logger:
    logger = get_logger(detailed)
    logger._dfo_detailed = detailed
    return logger

def info(logger, message: str):
    if getattr(logger, "_dfo_detailed", True): logger.info(message)

def essential(logger, level: int, message: str):
    logger.log(level, message)
