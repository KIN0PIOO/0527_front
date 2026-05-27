from langchain_core.tools import tool
import time
from typing import List

from server.tools.context import callbacks, mig_registry, record_agent_run, is_stop_requested


@tool
def run_data_migration(map_ids: List[int]) -> str:
    """데이터 이관 작업을 실행합니다.
    map_ids: 처리할 map_id 목록. poll_jobs() 결과의 migration_jobs에서 선택합니다.
    retry_count >= 3 인 job은 목록에서 제외하세요."""
    results = []
    logger = callbacks.get("logger")

    for map_id in map_ids:
        if is_stop_requested():
            results.append("중지 요청됨")
            break
        job = mig_registry.get(map_id)
        if job is None:
            results.append(f"map_id={map_id}: 현재 사이클에서 찾을 수 없음")
            continue
        started = time.perf_counter()
        try:
            callbacks["mig_inc"](map_id)
            callbacks["mig_proc"](job)
            record_agent_run("DB_MIGRATION", time.perf_counter() - started, "SUCCESS")
            if logger:
                logger.info(f"[DataMigrationTool] map_id={map_id} 완료")
            results.append(f"map_id={map_id}: 완료")
        except Exception as exc:
            record_agent_run("DB_MIGRATION", time.perf_counter() - started, "FAIL")
            if logger:
                logger.error(f"[DataMigrationTool] map_id={map_id} 오류: {exc}")
            results.append(f"map_id={map_id}: 실패 — {exc}")

    return "DataMigration 결과: " + " | ".join(results) if results else "처리할 작업 없음"
