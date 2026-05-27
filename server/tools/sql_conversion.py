from langchain_core.tools import tool
import time
from typing import List

from server.tools.context import callbacks, record_agent_run, sql_registry, is_stop_requested


@tool
def run_sql_conversion(row_ids: List[str]) -> str:
    """SQL 변환 작업을 실행합니다.
    row_ids: 처리할 row_id 목록. poll_jobs() 결과의 sql_jobs에서 선택합니다.
    URGENT 상태 항목을 우선 포함하고, 목록은 최대 20개를 권장합니다."""
    results = []
    logger = callbacks.get("logger")

    for row_id in row_ids:
        if is_stop_requested():
            results.append("중지 요청됨")
            break
        job = sql_registry.get(str(row_id))
        if job is None:
            results.append(f"row_id={row_id}: 현재 사이클에서 찾을 수 없음")
            continue
        started = time.perf_counter()
        try:
            callbacks["sql_inc"](row_id)
            callbacks["sql_proc"](job)
            record_agent_run("SQL_MIGRATION", time.perf_counter() - started, "SUCCESS")
            if logger:
                logger.info(f"[SqlConversionTool] row_id={row_id} 완료")
            results.append(f"row_id={row_id}: 완료")
        except Exception as exc:
            record_agent_run("SQL_MIGRATION", time.perf_counter() - started, "FAIL")
            if logger:
                logger.error(f"[SqlConversionTool] row_id={row_id} 오류: {exc}")
            results.append(f"row_id={row_id}: 실패 — {exc}")

    return "SqlConversion 결과: " + " | ".join(results) if results else "처리할 작업 없음"
