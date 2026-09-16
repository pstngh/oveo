from __future__ import annotations

import logging
import re
import traceback
import uuid
from pathlib import Path

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]")


def _label(value: str, *, limit: int = 120) -> str:
    return _SAFE_LABEL.sub("?", value)[:limit]


def log_unexpected(logger: logging.Logger, error: BaseException, *, area: str) -> str:
    """Log only an opaque ID, exception class, and code locations.

    Exception messages and traceback source lines are deliberately excluded because
    provider, request, and database errors can retain private user content.
    """

    error_id = uuid.uuid4().hex
    locations = "|".join(
        f"{_label(Path(frame.filename).name)}:{frame.lineno}:{_label(frame.name)}"
        for frame in traceback.extract_tb(error.__traceback__, limit=8)
    )
    logger.error(
        "unexpected_error error_id=%s area=%s exception_class=%s locations=%s",
        error_id,
        _label(area),
        _label(type(error).__name__),
        locations or "unavailable",
    )
    return error_id
