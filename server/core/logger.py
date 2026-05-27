"""공유 로거 — 모든 에이전트가 동일한 logger 인스턴스를 사용한다."""

import logging
import sys
from pathlib import Path

_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "runtime" / "agent.log"


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("migration_agent")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s")

        # Windows 환경 UTF-8 인코딩 보정
        try:
            import io
            sys.stdout = io.TextIOWrapper(
                sys.stdout.detach(), encoding="utf-8", line_buffering=True
            )
        except Exception:
            pass

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


logger = _setup_logger()
