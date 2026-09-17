from __future__ import annotations

import logging
import stat
from pathlib import Path

from oveo.logging_config import install_error_file_handler, remove_error_file_handler


def test_error_file_is_private_rotating_and_utc_timestamped(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "oveo-errors.log"
    handler = install_error_file_handler(path, max_bytes=180, backup_count=2)
    logger = logging.getLogger("oveo.test")
    try:
        for index in range(8):
            logger.error("synthetic_error code=test_failure sequence=%s", index)
    finally:
        remove_error_file_handler(handler)

    log_files = sorted(path.parent.glob("oveo-errors.log*"))
    assert path in log_files
    assert 1 < len(log_files) <= 3
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in log_files)
    diagnostics = "".join(item.read_text(encoding="utf-8") for item in log_files)
    assert "ERROR oveo.test synthetic_error code=test_failure" in diagnostics
    assert "Z ERROR" in diagnostics
