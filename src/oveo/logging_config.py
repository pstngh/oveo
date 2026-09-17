from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep the active log private, including after a rollover."""

    def _open(self) -> TextIO:  # type: ignore[override]
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


class _UtcFormatter(logging.Formatter):
    @staticmethod
    def converter(timestamp: float | None) -> time.struct_time:
        return time.gmtime(timestamp)


def install_error_file_handler(
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    """Persist ERROR records emitted by Oveo's content-safe diagnostic loggers."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.ERROR)
    handler.setFormatter(
        _UtcFormatter(
            fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logging.getLogger("oveo").addHandler(handler)
    return handler


def remove_error_file_handler(handler: logging.Handler) -> None:
    logging.getLogger("oveo").removeHandler(handler)
    handler.close()
