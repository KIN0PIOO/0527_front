import os
import re
import json
import streamlit as st
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from utils.db import (
    get_mig_status_summary,
    get_sql_status_summary,
    get_tuning_status_summary,
    get_recent_fails,
    get_mig_jobs,
    get_mig_logs,
    reset_mig_job,
    reset_sql_job,
    reset_sql_tuning,
)
from utils.agent_control import get_status as get_agent_status, start as start_agent

_ROOT         = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")
_CHATS_DIR    = _ROOT / "runtime" / "chats"
_COMMAND_FILE = _ROOT / "runtime" / "chat_command.json"

# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
<style>
/* 대화 목록 버튼 */
div[data-testid="stVerticalBlock"] button.chat-item {
    text-align: left; width: 100%;
}
/* 상태 카드 */
.stat-card {
    background: #f8f9fa; border: 1px solid #e9ecef;
    border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;
}
.stat-card-title {
    font-size: 12px; font-weight: 700; letter-spacing: 1px;
    text-transform: uppercase; color: #6c757d; margin-bottom: 10px;
}
.stat-row {
    display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 4px;
}
.stat-label { font-size: 13px; color: #495057; }
.stat-val   { font-size: 14px; font-weight: 700; color: #212529; }
.badge-pass { color: #10b981; }
.badge-fail { color: #ef4444; }
.badge-etc  { color: #6c757d; }
/* 구분선 */
.divider { border-top: 1px solid #e9ecef; margin: 8px 0; }
</style>
"""

# ── 채팅 파일 관리 ─────────────────────────────────────────────────────────────
def _list_chats() -> list[dict]:
    if not _CHATS_DIR.exists():
        return []
    chats = []
    for f in sorted(_CHATS_DIR.glob("*.json"), reverse=True):
        try:
            chats.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return chats

def _load_chat(chat_id: str) -> dict | None:
    path = _CHATS_DIR / f"{chat_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

def _save_chat(chat: dict):
    _CHATS_DIR.mkdir(parents=True, exist_ok=True)
    (_CHATS_DIR / f"{chat['id']}.json").write_text(
        json.dumps(chat, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def _delete_chat(chat_id: str):
    (_CHATS_DIR / f"{chat_id}.json").unlink(missing_ok=True)

def _new_chat() -> dict:
    return {
        "id":       datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
        "title":    "새 대화",
        "messages": [],
    }

# ── MAP_ID 감지 + DB 로그 조회 ────────────────────────────────────────────────
def _extract_map_ids(text: str) -> list[int]:
    """메시지에서 MAP_ID 숫자를 추출."""
    patterns = [
        r"map[\s_-]?id[\s:=]?\s*(\d+)",
        r"(\d+)\s*번",
        r"#(\d+)",
    ]
    found = set()
    for p in patterns:
        for m in re.findall(p, text, re.IGNORECASE):
            found.add(int(m))
    return list(found)

def _fetch_map_context(map_ids: list[int]) -> str:
    """MAP_ID별 상세 정보 + 로그를 텍스트로 반환."""
    if not map_ids:
        return ""
    lines = ["", "[조회된 MAP_ID 상세 정보]"]
    try:
        all_jobs = {int(j["MAP_ID"]): j for j in get_mig_jobs()}
        for mid in map_ids:
            lines.append(f"\n▶ MAP_ID {mid}")
            job = all_jobs.get(mid)
            if not job:
                lines.append("  - 해당 MAP_ID 없음")
                continue
            lines.append(f"  - 소스→타겟: {job.get('FR_TABLE')} → {job.get('TO_TABLE')}")
            lines.append(f"  - 상태: {job.get('STATUS') or 'NULL'}")
            lines.append(f"  - 재시도: {job.get('RETRY_COUNT')}회, 소요: {job.get('ELAPSED_SECONDS')}초")
            if job.get("MIG_SQL"):
                lines.append(f"  - MIG_SQL: {str(job['MIG_SQL'])[:200]}")
            if job.get("VERIFY_SQL"):
                lines.append(f"  - VERIFY_SQL: {str(job['VERIFY_SQL'])[:200]}")

            logs = get_mig_logs(mid)
            if logs:
                lines.append(f"  - 실행 로그 ({len(logs)}건):")
                for lg in logs[-10:]:  # 최근 10개만
                    lines.append(
                        f"    [{lg.get('LOG_LEVEL','?')}][{lg.get('STEP_NAME','?')}] "
                        f"{str(lg.get('MESSAGE',''))[:150]}"
                    )
            else:
                lines.append("  - 로그 없음")
    except Exception as e:
        lines.append(f"  (조회 오류: {e})")
    return "\n".join(lines)

# ── 수퍼바이저 명령 전달 ──────────────────────────────────────────────────────
def _write_supervisor_command(instruction: str, one_shot: bool = False) -> None:
    _COMMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    _COMMAND_FILE.write_text(
        json.dumps({
            "command": instruction,
            "one_shot": one_shot,
            "requested_at": datetime.now().isoformat(),
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _ensure_agent_running(one_shot: bool) -> str:
    """에이전트가 꺼져있으면 자동 시작. one_shot 여부에 따라 안내 문구 반환."""
    if not get_agent_status()["running"]:
        start_agent()
        if one_shot:
            return "\n에이전트를 자동 시작했습니다. 해당 작업만 처리 후 자동 종료됩니다."
        return "\n에이전트를 자동 시작했습니다."
    return ""

_SUPERVISOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "reset_and_run_mig_job",
            "description": (
                "특정 map_id의 마이그레이션 작업을 재실행합니다. "
                "DB에서 USE_YN=Y, STATUS=NULL, RETRY_COUNT=0으로 초기화한 뒤 "
                "수퍼바이저에게 해당 map_id를 즉시 실행하도록 지시합니다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "map_id": {"type": "integer", "description": "재실행할 MAP_ID"},
                },
                "required": ["map_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_and_run_sql_job",
            "description": (
                "특정 row_id의 SQL 변환 작업을 재실행합니다. "
                "DB에서 STATUS=READY로 초기화한 뒤 수퍼바이저에게 즉시 실행하도록 지시합니다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "row_id": {"type": "string", "description": "재실행할 SQL job의 ROW_ID"},
                },
                "required": ["row_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_and_run_sql_tuning",
            "description": (
                "특정 row_id의 SQL 튜닝을 재실행합니다. "
                "DB에서 TUNED_SQL/TUNED_TEST를 초기화한 뒤 수퍼바이저에게 즉시 실행하도록 지시합니다. "
                "STATUS=PASS인 row_id에만 적용됩니다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "row_id": {"type": "string", "description": "재실행할 tuning job의 ROW_ID"},
                },
                "required": ["row_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_supervisor_command",
            "description": (
                "수퍼바이저에게 실행 우선순위나 모드를 지시합니다. "
                "특정 에이전트만 실행하거나 순서를 바꾸고 싶을 때 사용하세요. "
                "예: 'mig만 돌려줘', 'SQL 먼저', 'tuning 건너뛰어'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "수퍼바이저 LLM에게 전달할 구체적인 한국어 지시문",
                    },
                    "summary": {
                        "type": "string",
                        "description": "사용자에게 보여줄 간단한 확인 메시지",
                    },
                },
                "required": ["instruction", "summary"],
            },
        },
    },
]

# ── LLM ───────────────────────────────────────────────────────────────────────
def _system_prompt() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "당신은 Oracle 데이터 마이그레이션 파이프라인의 운영 어시스턴트입니다.",
        "사용자의 질문에 한국어로 친절하고 간결하게 답변하세요.",
        "숫자나 상태를 물어보면 아래 실시간 DB 데이터를 기반으로 정확히 답변하세요.",
        "",
        f"[현재 시각: {now}]",
        "",
        "[에이전트별 현황]",
    ]
    for label, fn in [
        ("Mig Agent",    get_mig_status_summary),
        ("SQL Agent",    get_sql_status_summary),
        ("Tuning Agent", get_tuning_status_summary),
    ]:
        try:
            s = fn()
            detail = ", ".join(f"{k} {v}건" for k, v in s.items())
            lines.append(f"- {label}: {detail} (합계 {sum(s.values())}건)")
        except Exception:
            lines.append(f"- {label}: 조회 실패")

    try:
        fails = get_recent_fails(5)
        lines.append("")
        if fails:
            lines.append("[최근 실패 작업]")
            for r in fails:
                lines.append(f"- MAP_ID {r['MAP_ID']}: {r['FR_TABLE']} → {r['TO_TABLE']}")
        else:
            lines.append("[최근 실패 작업]: 없음")
    except Exception:
        pass

    lines += [
        "",
        "[수퍼바이저 제어 도구 사용 기준]",
        "- map_id=X 재실행 요청 → reset_and_run_mig_job(map_id=X)",
        "- SQL row_id=X 재실행 요청 → reset_and_run_sql_job(row_id=X)",
        "- SQL 튜닝 row_id=X 재실행 요청 → reset_and_run_sql_tuning(row_id=X)",
        "- 특정 에이전트만/먼저 실행 요청 ('mig만', 'sql 먼저' 등) → send_supervisor_command",
    ]

    return "\n".join(lines)

def _call_llm(chat_messages: list[dict]) -> str:
    api_key  = os.getenv("OPEN_API_KEY") or os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL",  "")
    model    = os.getenv("LLM_MODEL", "gpt-4o-mini")
    client   = OpenAI(api_key=api_key, base_url=base_url)

    last_user = next(
        (m["content"] for m in reversed(chat_messages) if m["role"] == "user"), ""
    )
    map_ids = _extract_map_ids(last_user)
    extra   = _fetch_map_context(map_ids) if map_ids else ""
    system  = _system_prompt() + extra

    full_messages = [{"role": "system", "content": system}] + chat_messages

    try:
        resp = client.chat.completions.create(
            model=model, messages=full_messages, temperature=0.7, max_tokens=1500,
            tools=_SUPERVISOR_TOOLS, tool_choice="auto",
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            call = msg.tool_calls[0]
            args = json.loads(call.function.arguments)
            name = call.function.name

            if name == "reset_and_run_mig_job":
                map_id = args["map_id"]
                ok = reset_mig_job(map_id)
                if not ok:
                    return f"⚠️ map_id={map_id}를 찾을 수 없거나 초기화에 실패했습니다."
                _write_supervisor_command(
                    f"map_id={map_id}의 마이그레이션 작업이 재실행 대기 상태로 초기화되었습니다. "
                    f"poll_jobs 후 run_data_migration([{map_id}])를 호출하세요.",
                    one_shot=True,
                )
                hint = _ensure_agent_running(one_shot=True)
                return f"✅ map_id={map_id} 초기화 완료. 해당 작업만 실행 후 자동 종료됩니다.{hint}"

            if name == "reset_and_run_sql_job":
                row_id = args["row_id"]
                ok = reset_sql_job(row_id)
                if not ok:
                    return f"⚠️ row_id={row_id}를 찾을 수 없거나 초기화에 실패했습니다."
                _write_supervisor_command(
                    f"row_id={row_id}의 SQL 변환 작업이 READY 상태로 초기화되었습니다. "
                    f"poll_jobs 후 run_sql_conversion(['{row_id}'])를 호출하세요.",
                    one_shot=True,
                )
                hint = _ensure_agent_running(one_shot=True)
                return f"✅ row_id={row_id} SQL 변환 초기화 완료. 해당 작업만 실행 후 자동 종료됩니다.{hint}"

            if name == "reset_and_run_sql_tuning":
                row_id = args["row_id"]
                ok = reset_sql_tuning(row_id)
                if not ok:
                    return f"⚠️ row_id={row_id}를 찾을 수 없거나 STATUS=PASS가 아니어서 초기화할 수 없습니다."
                _write_supervisor_command(
                    f"row_id={row_id}의 SQL 튜닝이 초기화되었습니다. "
                    f"poll_jobs 후 run_sql_tuning(['{row_id}'])를 호출하세요.",
                    one_shot=True,
                )
                hint = _ensure_agent_running(one_shot=True)
                return f"✅ row_id={row_id} SQL 튜닝 초기화 완료. 해당 작업만 실행 후 자동 종료됩니다.{hint}"

            if name == "send_supervisor_command":
                _write_supervisor_command(args["instruction"], one_shot=False)
                hint = _ensure_agent_running(one_shot=False)
                return f"✅ {args['summary']}\n\n수퍼바이저가 다음 사이클에 반영합니다.{hint}"

        return (msg.content or "").strip()

    except Exception:
        # tool use 미지원 모델 fallback
        resp = client.chat.completions.create(
            model=model, messages=full_messages, temperature=0.7, max_tokens=1500,
        )
        return resp.choices[0].message.content.strip()

# ── 오른쪽 상태 패널 ───────────────────────────────────────────────────────────
_ICON = {"PASS": "✅", "FAIL": "❌", "RUNNING": "🔄", "READY": "🔵",
         "SKIP": "⏭️", "NULL": "⚫", "PENDING": "🟣"}
_CLR  = {"PASS": "badge-pass", "FAIL": "badge-fail"}

def _status_card(title: str, summary: dict):
    if not summary:
        st.markdown(f"""
        <div class="stat-card">
          <div class="stat-card-title">{title}</div>
          <span style="color:#9ca3af;font-size:13px">데이터 없음</span>
        </div>""", unsafe_allow_html=True)
        return
    total = sum(summary.values())
    rows  = ""
    for k, v in sorted(summary.items(),
                       key=lambda x: ["PASS","FAIL","RUNNING","READY","SKIP","PENDING","NULL"].index(x[0])
                       if x[0] in ["PASS","FAIL","RUNNING","READY","SKIP","PENDING","NULL"] else 99):
        icon  = _ICON.get(k, "◻️")
        cls   = _CLR.get(k, "badge-etc")
        rows += f"""<div class="stat-row">
            <span class="stat-label">{icon} {k}</span>
            <span class="stat-val {cls}">{v}</span>
        </div>"""
    st.markdown(f"""
    <div class="stat-card">
      <div class="stat-card-title">{title} &nbsp;<span style="font-weight:400;color:#adb5bd">총 {total}건</span></div>
      {rows}
    </div>""", unsafe_allow_html=True)

# ── 메인 렌더 ─────────────────────────────────────────────────────────────────
def render():
    st.markdown(CSS, unsafe_allow_html=True)

    # ── 세션 초기화 ──────────────────────────────────────────────────────────
    if "current_chat" not in st.session_state:
        st.session_state.current_chat = _new_chat()
    if "chat_refresh" not in st.session_state:
        st.session_state.chat_refresh = False

    chat = st.session_state.current_chat

    # ── 3패널 레이아웃 ────────────────────────────────────────────────────────
    left, center, right = st.columns([1.6, 4, 1.6], gap="medium")

    # ════════════════════════════════════════════════════════════
    # 왼쪽: 대화 목록
    # ════════════════════════════════════════════════════════════
    with left:
        st.markdown("#### 💬 대화 목록")
        if st.button("✏️ 새 대화", use_container_width=True, type="primary"):
            st.session_state.current_chat = _new_chat()
            st.rerun()

        st.markdown("---")
        chats = _list_chats()
        if not chats:
            st.caption("대화 기록 없음")
        for c in chats:
            label = c.get("title", "대화")[:18]
            is_current = c["id"] == chat["id"]
            col_title, col_del = st.columns([5, 1])
            with col_title:
                if st.button(
                    f"{'▶ ' if is_current else ''}{label}",
                    key=f"chat_{c['id']}",
                    use_container_width=True,
                    type="primary" if is_current else "secondary",
                ):
                    loaded = _load_chat(c["id"])
                    if loaded:
                        st.session_state.current_chat = loaded
                        st.rerun()
            with col_del:
                if st.button("🗑", key=f"del_{c['id']}", help="삭제"):
                    _delete_chat(c["id"])
                    if is_current:
                        st.session_state.current_chat = _new_chat()
                    st.rerun()

    # ════════════════════════════════════════════════════════════
    # 가운데: 채팅
    # ════════════════════════════════════════════════════════════
    with center:
        st.markdown(f"#### 🤖 Migration 어시스턴트")
        st.caption("파이프라인 상태, 실패 원인, 작업 현황 등 무엇이든 물어보세요.")

        # 메시지 표시
        msg_container = st.container(height=660)
        with msg_container:
            if not chat["messages"]:
                st.markdown("""
                <div style="text-align:center;color:#9ca3af;padding:80px 0 40px 0">
                    <div style="font-size:40px">💬</div>
                    <div style="font-size:15px;margin-top:12px">아래에서 질문을 입력해보세요</div>
                    <div style="font-size:12px;margin-top:8px;color:#d1d5db">
                        예: "현재 실패한 작업은 몇 개야?" · "PASS된 이관 테이블 목록 보여줘"
                    </div>
                </div>
                """, unsafe_allow_html=True)
            for msg in chat["messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

        # 입력
        user_input = st.chat_input("메시지를 입력하세요...", key="chat_input")

    # ════════════════════════════════════════════════════════════
    # 오른쪽: 에이전트 상태
    # ════════════════════════════════════════════════════════════
    with right:
        rc, rr = st.columns([3, 1])
        with rc:
            st.markdown("#### 📊 현황")
        with rr:
            if st.button("🔄", help="새로고침"):
                st.rerun()

        try:
            _status_card("📦 Mig",    get_mig_status_summary())
        except Exception as e:
            st.error(str(e))
        try:
            _status_card("🔄 SQL",    get_sql_status_summary())
        except Exception as e:
            st.error(str(e))
        try:
            _status_card("⚡ Tuning", get_tuning_status_summary())
        except Exception as e:
            st.error(str(e))

    # ── 메시지 처리 (컬럼 밖에서) ─────────────────────────────────────────────
    if user_input and user_input.strip():
        # 유저 메시지 추가
        chat["messages"].append({"role": "user", "content": user_input.strip()})
        if chat["title"] == "새 대화":
            chat["title"] = user_input.strip()[:24]

        # LLM 호출
        with center:
            with st.chat_message("assistant"):
                placeholder = st.empty()
                placeholder.markdown("⏳ 답변 생성 중...")
                try:
                    answer = _call_llm(chat["messages"])
                except Exception as e:
                    answer = f"⚠️ LLM 호출 실패: {e}"
                placeholder.markdown(answer)

        chat["messages"].append({"role": "assistant", "content": answer})
        _save_chat(chat)
        st.session_state.current_chat = chat
        st.rerun()
