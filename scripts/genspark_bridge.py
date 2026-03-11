#!/usr/bin/env python3
"""
AADS Bridge — CEO 대화 결정사항 감지 + ceo_directives 자동 저장
생성: 2026-03-04 T-021
확장: 2026-03-05 T-037 — CEO↔웹 Claude 일반 대화 저장 + 매니저 DIRECTIVE 블록 저장

사용:
  # stdin 읽기
  echo "확정: Phase 3 진행" | python3 bridge.py

  # 텍스트 직접 입력
  python3 bridge.py --text "OK, Phase 3 진행해"

  # 파일 처리
  python3 bridge.py --file conversation.txt

  # API 연결 테스트
  python3 bridge.py --test

  # 라이브러리로 import
  from bridge import process_message
  result = process_message("확정: 진행해")

동작:
  - "확정", "승인", "진행해", "OK" 등 키워드 감지
  - 감지 시 ceo_directives 카테고리에 자동 저장
  - T-037: 모든 대화를 go100_user_memory에 카테고리 분류 후 저장
  - T-037 B-3: >>>DIRECTIVE_START 블록 감지 시 mgr_directive로 저장
  - JSON 결과 반환
"""

import argparse
import glob as _glob
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.request
import urllib.error
import asyncio
try:
    import aiohttp as _aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
from datetime import datetime, timedelta, timezone

# ─── T-100: 완료보고 메시지 필터링 ───────────────────────────────────────────
SKIP_PATTERNS = [
    "작업 완료",
    "push 완료",
    "에러 종료",
    "BRIDGE_RESULT",
    "현재 작업 현황",
    "다음 지시서 작성 전 필수 확인",
    "맥락유지 필수문서",
    "HANDOVER.md 반드시 갱신",
    "지시서 작성규칙",
    "auto_trigger 자동 실행",
]

# T-100: 자기 발송 마커
BRIDGE_SENT_MARKER = "[BRIDGE-SENT]"

# T-100: 중복 처리 방지
_processed_ids: set = set()
_processed_ids_order: list = []
_MAX_PROCESSED_IDS = 1000

# ─── 설정 ───────────────────────────────────────────────────────────────────
CONTEXT_API = os.getenv(
    "CONTEXT_API", "https://aads.newtalk.kr/api/v1/context/system"
)
MEMORY_API = os.getenv(
    "MEMORY_API", "https://aads.newtalk.kr/api/v1/memory/log"
)
AADS_ROOT = os.getenv("AADS_ROOT", "/root/aads")
ENV_FILE = os.path.join(AADS_ROOT, "aads-server", ".env")

# CEO 결정사항 감지 키워드 (대소문자 무시)
DECISION_KEYWORDS = [
    "확정", "승인", "진행해", "진행하자", "진행 해",
    "ok", "오케이", "결정", "채택", "선택", "동의",
    "맞아", "그렇게 해", "그렇게해",
    "approved", "confirmed", "go ahead", "proceed",
    "lets go", "let's go",
]

# ─── T-037 A-1: 대화 분류 카테고리 7종 ──────────────────────────────────
CATEGORY_KEYWORDS = {
    "ceo_directive":   {"keywords": ["확정", "승인", "ok", "결정", "적용", "반영"],           "importance": 9.0},
    "architecture":    {"keywords": ["아키텍처", "설계", "구조", "파이프라인", "에이전트"],   "importance": 8.0},
    "cost_analysis":   {"keywords": ["비용", "예산", "가격", "절감", "과금", "수익"],         "importance": 7.5},
    "task_planning":   {"keywords": ["작업", "T-0", "배포", "실행", "구현", "지시서"],        "importance": 7.0},
    "troubleshooting": {"keywords": ["에러", "오류", "502", "장애", "실패", "버그"],          "importance": 7.0},
    "research":        {"keywords": ["연구", "조사", "분석", "비교", "검토"],                 "importance": 6.0},
    "general":         {"keywords": [],                                                       "importance": 5.0},
}

# ─── API 키 로드 ──────────────────────────────────────────────────────────
def _load_monitor_key() -> str:
    key = os.getenv("AADS_MONITOR_KEY", "")
    if key:
        return key
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("AADS_MONITOR_KEY="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""

MONITOR_KEY = _load_monitor_key()
WATCHDOG_API = os.getenv("AADS_API_URL", "https://aads.newtalk.kr/api/v1") + "/watchdog/errors"


# ─── T-038: Watchdog 에러 보고 ────────────────────────────────────────────
def report_error_to_watchdog(error_type: str, source: str, server: str,
                              message: str, stack_trace: str = "") -> None:
    """에러를 Watchdog API에 자동 보고. 실패해도 무시 (무한루프 방지)."""
    try:
        payload = json.dumps({
            "error_type": error_type,
            "source": source,
            "server": server,
            "message": message[:1000],
            "stack_trace": stack_trace[:2000] if stack_trace else None,
            "context": {},
        }).encode()
        req = urllib.request.Request(
            WATCHDOG_API,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Monitor-Key": MONITOR_KEY,
                "User-Agent": "curl/7.64.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ─── KST 타임스탬프 (Python 3.6 호환) ───────────────────────────────────
def _now_kst() -> str:
    try:
        kst = timezone(timedelta(hours=9))
        return datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

def _now_iso_kst() -> str:
    try:
        kst = timezone(timedelta(hours=9))
        return datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

# ─── Context API 쓰기 ─────────────────────────────────────────────────────
def write_to_api(category: str, key: str, value: dict) -> dict:
    """Context API에 데이터 저장 (POST /context/system)"""
    payload = json.dumps({
        "category": category,
        "key": key,
        "value": value
    }).encode()
    req = urllib.request.Request(
        CONTEXT_API,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Monitor-Key": MONITOR_KEY,
            "User-Agent": "curl/7.64.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"status": "error", "code": e.code, "detail": body}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ─── Context API 읽기 ─────────────────────────────────────────────────────
def read_from_api(category: str = "", key: str = "") -> dict:
    """Context API에서 데이터 조회 (GET /context/system)"""
    url = CONTEXT_API
    if category:
        url += "/" + category
    if key:
        url += "/" + key
    req = urllib.request.Request(url, headers={"X-Monitor-Key": MONITOR_KEY, "User-Agent": "curl/7.64.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ─── T-037: Memory API 쓰기 (go100_user_memory POST /memory/log) ─────────
def _write_to_memory_api(payload: dict) -> dict:
    """go100_user_memory POST /memory/log 호출"""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        MEMORY_API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Monitor-Key": MONITOR_KEY,
            "User-Agent": "curl/7.64.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        return {"status": "error", "code": e.code, "detail": body_text}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

# ─── T-037 A-1: 대화 분류 함수 ───────────────────────────────────────────
def classify_aads_conversation(text: str):
    """텍스트를 카테고리로 분류하고 (category, importance) 반환"""
    text_lower = text.lower()
    for category, info in CATEGORY_KEYWORDS.items():
        if category == "general":
            continue
        for kw in info["keywords"]:
            if kw.lower() in text_lower:
                return category, info["importance"]
    return "general", CATEGORY_KEYWORDS["general"]["importance"]

# ─── T-037 A-2: 보조 함수 ────────────────────────────────────────────────
def extract_decisions(text: str) -> list:
    """승인/확정/결정/적용 포함 문장 리스트 반환"""
    markers = ["승인", "확정", "결정", "적용"]
    return [ln.strip() for ln in text.splitlines() if ln.strip() and any(m in ln for m in markers)]

def extract_action_items(text: str) -> list:
    """T-0 패턴, 진행/배포/실행 포함 문장 리스트 반환"""
    markers = ["진행", "배포", "실행"]
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and (re.search(r'T-\d+', ln) or any(m in ln for m in markers))]

def generate_summary(ceo_input: str, ai_response: str) -> str:
    """각 첫 150자 추출하여 요약 생성"""
    return "CEO: " + ceo_input[:150].strip() + " | AI: " + ai_response[:150].strip()

# ─── T-037 A-3: 저장 함수 ────────────────────────────────────────────────
def save_aads_conversation(ceo_input: str, ai_response: str = "", session_id: str = "") -> dict:
    """
    CEO↔Genspark 대화를 system_memory에 저장 (POST /context/system).
    /conversations 페이지에서 conversation:* 카테고리로 조회됨.
    """
    try:
        kst = timezone(timedelta(hours=9))
        if not session_id:
            session_id = "web-claude-" + datetime.now(kst).strftime("%Y%m%d")
        combined = ceo_input + " " + ai_response
        category, importance = classify_aads_conversation(combined)

        # 프로젝트 분류
        proj = "aads"  # 기본값
        sid_lower = session_id.lower()
        for p in ("kis", "go100", "sf", "sales", "ntv2", "newtalk", "nas"):
            if p in sid_lower:
                proj = p
                break

        epoch = int(time.time())
        snapshot = f"[USER]\n{ceo_input[:2000]}\n---MSG_SEP---\n[ASSISTANT]\n{ai_response[:2000]}"
        return write_to_api(
            category=f"conversation:{proj}",
            key=f"chat_{epoch}",
            value={
                "snapshot": snapshot,
                "char_count": len(ceo_input) + len(ai_response),
                "logged_at": _now_iso_kst(),
                "source": "genspark_bridge",
                "project": proj.upper(),
                "session_id": session_id,
                "category": category,
                "summary": generate_summary(ceo_input, ai_response),
                "decisions": extract_decisions(combined),
                "action_items": extract_action_items(combined),
            },
        )
    except Exception as e:
        logging.warning("save_aads_conversation failed: %s", e)
        return {"status": "error", "detail": str(e)}

# ─── T-037 B-3: DIRECTIVE 블록 저장 함수 ─────────────────────────────────
def save_directive_block(block_text: str, source: str = "bridge") -> dict:
    """
    >>>DIRECTIVE_START ~ >>>DIRECTIVE_END 블록을 매니저 지시서로 memory/log 저장.
    실패 시 로그만 남기고 흐름 중단하지 않음.
    """
    try:
        epoch = int(time.time())
        task_id_match = re.search(r'Task ID\s*:\s*(\S+)', block_text)
        task_id = task_id_match.group(1) if task_id_match else "unknown_" + str(epoch)
        payload = {
            "user_id": 2,
            "memory_type": "mgr_directive_" + task_id,
            "content": {
                "agent_id": "AADS_PROJECT_MGR",
                "event_type": "directive_block",
                "details": {
                    "task_id": task_id,
                    "directive_text": block_text[:2000],
                    "source": source,
                },
                "logged_at": _now_iso_kst(),
            },
            "importance": 8.5,
            "expires_at": None,
        }
        return _write_to_memory_api(payload)
    except Exception as e:
        logging.warning("save_directive_block failed: %s", e)
        return {"status": "error", "detail": str(e)}

# ─── T-102: 문서 감지 패턴 ───────────────────────────────────────────────
DOCUMENT_PATTERNS = {
    "plan":     ["기획서", "설계서", "프로토타입", "UI-PROTO", "디자인 설계"],
    "tech":     ["기술 스택", "아키텍처", "컴포넌트 구조", "API 맵", "엔드포인트"],
    "research": ["연구 보고", "분석 보고", "비용 분석", "비교 분석", "최적화 연구"],
    "status":   ["종합 상황 보고", "지휘통제소", "진행 상황 보고"],
    "directive": ["DIRECTIVE_START", ">>>DIRECTIVE"],
}

DOCUMENTS_API = os.getenv("AADS_API_URL", "https://aads.newtalk.kr/api/v1") + "/documents"


def classify_document(text: str):
    """텍스트에서 문서 유형 감지. 없으면 None 반환."""
    for doc_type, keywords in DOCUMENT_PATTERNS.items():
        if any(kw in text for kw in keywords):
            return doc_type
    return None


def _extract_doc_title(text: str, doc_type: str) -> str:
    """텍스트 첫 줄 또는 첫 헤더에서 제목 추출."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:80]
    return doc_type.upper() + " " + _now_kst()


def save_as_document(text: str, doc_type: str, title: str = "", source_session: str = "bridge") -> dict:
    """
    T-102: 문서성 컨텐츠를 POST /api/v1/documents 로 저장.
    실패해도 로그만 남기고 흐름 중단하지 않음.
    """
    try:
        if not title:
            title = _extract_doc_title(text, doc_type)
        payload = json.dumps({
            "type": doc_type,
            "title": title,
            "content": text,
            "tags": [doc_type],
            "source_session": source_session,
        }).encode()
        req = urllib.request.Request(
            DOCUMENTS_API,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Monitor-Key": MONITOR_KEY,
                "User-Agent": "curl/7.64.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        logging.warning("save_as_document HTTP error %s: %s", e.code, body)
        return {"status": "error", "code": e.code, "detail": body}
    except Exception as e:
        logging.warning("save_as_document failed: %s", e)
        return {"status": "error", "detail": str(e)}


# ─── T-106: 지시서 파일명 생성 (우선순위 포함) ───────────────────────────
def _generate_filename(content: str, project: str = "AADS") -> str:
    """
    지시서 파일명 생성 — 우선순위 감지 후 파일명에 반영.
    예: AADS_20260306_120317_P0_BRIDGE.md (P0-CRITICAL인 경우)
        AADS_20260306_120317_P1_BRIDGE.md (P1-HIGH인 경우)
        AADS_20260306_120317_P2_BRIDGE.md (기본값)
    """
    timestamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S")
    priority = "P2"  # 기본값
    if "P0-CRITICAL" in content:
        priority = "P0"
    elif "P1-HIGH" in content:
        priority = "P1"
    return f"{project}_{timestamp}_{priority}_BRIDGE.md"


# ─── 결정사항 감지 ────────────────────────────────────────────────────────
def detect_decision(text: str) -> bool:
    """텍스트에서 CEO 결정 키워드 감지"""
    text_lower = text.lower()
    for kw in DECISION_KEYWORDS:
        if kw.lower() in text_lower:
            return True
    return False

def get_matched_keywords(text: str) -> list:
    """매칭된 키워드 목록 반환"""
    text_lower = text.lower()
    return [kw for kw in DECISION_KEYWORDS if kw.lower() in text_lower]

def extract_decision_summary(text: str) -> str:
    """결정사항 요약 추출 (비어있지 않은 첫 3줄, 최대 200자)"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    summary = " | ".join(lines[:3])
    return summary[:200] if len(summary) > 200 else summary

# ─── 메인 처리 함수 ───────────────────────────────────────────────────────
def process_message(message: str, source: str = "bridge",
                    ai_response: str = "", session_id: str = "") -> dict:
    """
    CEO 메시지 처리:
      1) B-3: >>>DIRECTIVE_START 블록 감지 → memory/log mgr_directive 저장
      2) 기존 DIRECTIVE 키워드 감지 → ceo_directives 카테고리에 저장 (100% 유지)
      3) A-4: save_aads_conversation 호출 (결정 감지 여부와 무관하게 항상 실행)

    반환:
      {
        "detected": bool,
        "summary": str | None,
        "saved": str | None,
        "result": dict | None,
        "conversation_saved": dict,
        "directive_blocks_saved": list
      }
    """
    # T-100: 자기 발송 마커 체크
    if BRIDGE_SENT_MARKER in message:
        return {"detected": False, "skipped": True, "reason": "bridge-sent marker"}

    # T-100: 완료보고 패턴 스킵
    for pattern in SKIP_PATTERNS:
        if pattern in message:
            return {"detected": False, "skipped": True, "reason": f"skip_pattern: {pattern}"}

    # T-100: 중복 처리 방지
    msg_hash = hashlib.md5(message.encode()).hexdigest()
    if msg_hash in _processed_ids:
        return {"detected": False, "skipped": True, "reason": "duplicate"}
    _processed_ids.add(msg_hash)
    _processed_ids_order.append(msg_hash)
    if len(_processed_ids_order) > _MAX_PROCESSED_IDS:
        old = _processed_ids_order.pop(0)
        _processed_ids.discard(old)

    # B-3: DIRECTIVE 블록 감지 및 저장 (T-100 강화: 규칙설명·템플릿·완료된 task 스킵)
    directive_blocks_saved = []
    done_dir = "/root/.genspark/directives/done"
    for match in re.finditer(r'>>>DIRECTIVE_START(.*?)(?:>>>DIRECTIVE_END|$)', message, re.DOTALL):
        block_text = match.group(0).strip()

        # T-100 수정4: "지시서 작성규칙" 또는 "작성규칙"이 같은 메시지에 있으면 규칙 설명 → 스킵
        if "지시서 작성규칙" in message or "작성규칙" in message:
            continue

        # T-100 수정4: Task ID가 실제 숫자(T-NNN)가 아니면 템플릿 → 스킵
        task_id_match_inner = re.search(r'Task ID\s*:\s*T-(\d+)', block_text)
        if not task_id_match_inner:
            continue

        # T-100 수정4: done/ 폴더에 이미 완료된 task면 스킵
        task_num = task_id_match_inner.group(1)
        done_files = (
            _glob.glob(f"{done_dir}/*T{task_num}*")
            + _glob.glob(f"{done_dir}/*T-{task_num}*")
        )
        if done_files:
            continue

        dr = save_directive_block(block_text, source=source)
        directive_blocks_saved.append(dr)

    # 기존 DIRECTIVE 키워드 저장 로직 (100% 유지)
    directive_result = None
    saved = None
    summary = None
    keywords = []
    if detect_decision(message):
        ts = _now_kst()
        epoch = int(time.time())
        summary = extract_decision_summary(message)
        keywords = get_matched_keywords(message)
        key = "ceo_decision_" + str(epoch)
        value = {
            "timestamp": ts,
            "source": source,
            "decision_summary": summary,
            "full_text": message[:1000],
            "keywords_matched": keywords,
        }
        directive_result = write_to_api("ceo_directives", key, value)
        if isinstance(directive_result, dict) and directive_result.get("status") == "ok":
            saved = directive_result.get("saved", "ceo_directives/" + key)

    # T-102: 문서 감지 → /api/v1/documents 저장 (기존 대화 저장과 별도 추가 실행)
    document_saved = None
    combined_text = message + (" " + ai_response if ai_response else "")
    doc_type = classify_document(combined_text)
    if doc_type:
        try:
            document_saved = save_as_document(
                combined_text,
                doc_type=doc_type,
                source_session=session_id or "bridge",
            )
        except Exception as e:
            logging.warning("T-102 save_as_document exception: %s", e)
            document_saved = {"status": "error", "detail": str(e)}

    # A-4: 일반 대화 전체 저장 (항상 실행)
    if not session_id:
        kst = timezone(timedelta(hours=9))
        session_id = "web-claude-" + datetime.now(kst).strftime("%Y%m%d")
    try:
        conv_result = save_aads_conversation(message, ai_response, session_id=session_id)
    except Exception as e:
        # T-038: bridge 에러 자동 보고
        import traceback
        report_error_to_watchdog(
            "bridge_error", "bridge.py", "68",
            f"save_aads_conversation failed: {e}",
            traceback.format_exc()
        )
        conv_result = {"status": "error", "detail": str(e)}

    return {
        "detected": bool(directive_result),
        "summary": summary,
        "keywords": keywords,
        "saved": saved,
        "result": directive_result,
        "conversation_saved": conv_result,
        "directive_blocks_saved": directive_blocks_saved,
        "document_saved": document_saved,
    }

# ─── AADS-109: 지시서 환경 호환성 사전 검증 ──────────────────────────────

class DirectiveValidator:
    """지시서 환경 호환성 사전 검증"""

    def __init__(self, aads_api_url: str):
        self.api_url = aads_api_url

    async def get_server_env(self, server: str) -> dict:
        """Context API에서 서버 환경 스냅샷 조회"""
        if not _AIOHTTP_AVAILABLE:
            return {}
        try:
            async with _aiohttp.ClientSession() as session:
                r = await session.get(
                    f"{self.api_url}/context/system",
                    params={"category": "server_environment", "key": f"env_{server}"},
                    timeout=_aiohttp.ClientTimeout(total=5)
                )
                if r.status == 200:
                    data = await r.json()
                    items = data.get("items", [])
                    if items:
                        return items[0].get("data", {})
        except Exception:
            pass
        return {}

    async def validate(self, directive_content: str, target_server: str) -> dict:
        """지시서 내용 vs 서버 환경 교차 검증"""
        env = await self.get_server_env(target_server)
        if not env:
            return {"valid": True, "warnings": ["⚠️ 서버 환경 스냅샷 없음 — 검증 불가"], "blockers": []}

        warnings = []
        blockers = []
        runtimes = env.get("runtimes", {})
        projects = env.get("projects", {})

        # 1) PHP 버전 체크
        if any(kw in directive_content for kw in ["composer require", "php artisan", "Laravel", "laravel"]):
            php_ver = runtimes.get("php", "not installed")
            if "not installed" in php_ver:
                blockers.append("🚫 PHP 미설치 — 지시서에 PHP 명령어 포함")
            elif "5." in php_ver or "7.0" in php_ver or "7.1" in php_ver:
                blockers.append(f"🚫 PHP {php_ver} — Laravel 11+ 에는 PHP 8.2+ 필요")

        # 2) Node 체크
        if any(kw in directive_content for kw in ["npm install", "npm run", "npx", "node "]):
            node_ver = runtimes.get("node", "not installed")
            if "not installed" in node_ver:
                blockers.append("🚫 Node.js 미설치 — 지시서에 npm/node 명령어 포함")

        # 3) Python 체크
        if any(kw in directive_content for kw in ["pip install", "python3 ", "pip3 "]):
            py_ver = runtimes.get("python3", "not installed")
            if "not installed" in py_ver:
                warnings.append("⚠️ Python3 미설치 — pip/python3 명령어 포함")

        # 4) Docker 체크
        if any(kw in directive_content for kw in ["docker compose", "docker-compose", "docker build"]):
            docker_ver = runtimes.get("docker", "not installed")
            if "not installed" in docker_ver:
                blockers.append("🚫 Docker 미설치 — 지시서에 Docker 명령어 포함")

        # 5) 경로 존재 확인
        cd_paths = re.findall(r'cd\s+(/[^\s;&&|]+)', directive_content)
        for path in set(cd_paths):
            found = False
            for proj_path, proj_info in projects.items():
                if path.startswith(proj_path) and proj_info.get("exists"):
                    found = True
                    break
            if not found and path not in ("/root", "/tmp", "/var/log"):
                blockers.append(f"🚫 경로 {path} — 서버에 존재하지 않음")

        # 6) DB 테이블 참조 확인
        schema_refs = re.findall(r"Schema::table\('(\w+)'", directive_content)
        alter_refs = re.findall(r"ALTER TABLE\s+(\w+)", directive_content, re.IGNORECASE)
        existing_tables = str(env.get("databases", {}))
        for table in set(schema_refs + alter_refs):
            if table not in existing_tables:
                warnings.append(f"⚠️ 테이블 '{table}' — DB에 없음 (신규 생성 확인 필요)")

        # 7) systemd 서비스 참조 확인
        service_refs = re.findall(r"systemctl\s+(?:restart|start|stop|enable)\s+(\S+)", directive_content)
        active_services = str(env.get("services", {}).get("systemd_active", ""))
        for svc in set(service_refs):
            if svc not in active_services:
                warnings.append(f"⚠️ 서비스 '{svc}' — 현재 active 목록에 없음")

        is_valid = len(blockers) == 0

        return {
            "valid": is_valid,
            "blockers": blockers,
            "warnings": warnings,
            "server": target_server,
            "env_collected_at": env.get("collected_at", "unknown"),
        }


# ─── AADS-109: 브릿지 투입 시 자동 검증 연동 ────────────────────────────

async def _process_directive_async(content: str, project: str) -> object:
    """지시서 투입 전 환경 호환성 사전 검증 후 저장 (async 내부 구현)"""
    aads_api_url = os.getenv("AADS_API_URL", "http://localhost:8000/api/v1")

    # 대상 서버 추출
    server_match = re.search(r'서버:\s*(\d+)', content)
    target_server = server_match.group(1) if server_match else "68"

    # 사전 검증
    validator = DirectiveValidator(aads_api_url)
    result = await validator.validate(content, target_server)

    if result["blockers"]:
        warning_content = f"""# ⚠️ 지시서 사전 검증 실패 — CEO 확인 필요

서버: {target_server}
환경 스냅샷: {result['env_collected_at']}

## 차단 사유 (blockers):
{''.join(f'- {b}' + chr(10) for b in result['blockers'])}

## 경고 (warnings):
{''.join(f'- {w}' + chr(10) for w in result['warnings'])}

## 원본 지시서:
{content[:500]}...

---
이 지시서를 실행하면 실패할 가능성이 높습니다.
서버 환경 업그레이드 후 재투입하거나, 지시서를 수정해주세요.
"""
        warning_path = f"/root/.genspark/directives/blocked/{project}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_BLOCKED.md"
        os.makedirs(os.path.dirname(warning_path), exist_ok=True)
        with open(warning_path, "w") as f:
            f.write(warning_content)
        return False

    if result["warnings"]:
        warning_header = "# ⚠️ 환경 경고 (자동 검증)\n"
        for w in result["warnings"]:
            warning_header += f"# {w}\n"
        warning_header += f"# 환경 스냅샷: {result['env_collected_at']}\n---\n\n"
        content = warning_header + content

    # 정상 투입 — pending 디렉터리에 저장
    pending_path = f"/root/.genspark/directives/pending/{project}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_BRIDGE.md"
    os.makedirs(os.path.dirname(pending_path), exist_ok=True)
    with open(pending_path, "w") as f:
        f.write(content)
    return pending_path


def process_directive(content: str, project: str = "AADS") -> object:
    """지시서 투입 전 환경 호환성 사전 검증 후 저장 (동기 래퍼, Python 3.6 호환)"""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_process_directive_async(content, project))


# ─── CLI ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="AADS Bridge — CEO 결정사항 감지 + Context API 자동 저장",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", "-f", help="처리할 대화 파일 경로")
    parser.add_argument("--text", "-t", help="처리할 텍스트 직접 입력")
    parser.add_argument("--source", "-s", default="cli", help="소스 레이블 (기본: cli)")
    parser.add_argument("--ai-response", default="", help="AI 응답 텍스트 (T-037 대화 저장용)")
    parser.add_argument("--session-id", default="", help="세션 ID (기본: web-claude-YYYYMMDD)")
    parser.add_argument(
        "--test", action="store_true", help="API 연결 테스트 및 설정 확인"
    )
    args = parser.parse_args()

    # ─── 연결 테스트 ───
    if args.test:
        print("=== AADS Bridge 설정 ===")
        print("Context API : " + CONTEXT_API)
        print("Memory API  : " + MEMORY_API)
        print("Monitor Key : " + ("설정됨 (" + MONITOR_KEY[:8] + "...)" if MONITOR_KEY else "미설정 ⚠️"))
        print("ENV 파일    : " + ENV_FILE + (" (존재)" if os.path.exists(ENV_FILE) else " (없음)"))
        print("")
        print("=== API 연결 테스트 ===")
        data = read_from_api("ceo_directives")
        if data.get("status") == "ok":
            count = data.get("count", len(data.get("data", [])))
            print("GET /context/system/ceo_directives : OK (" + str(count) + "개 항목)")
        else:
            print("GET 실패: " + str(data))
        print("")
        print("=== 감지 키워드 목록 ===")
        for kw in DECISION_KEYWORDS:
            print("  - " + kw)
        print("")
        print("=== 대화 분류 카테고리 ===")
        for cat, info in CATEGORY_KEYWORDS.items():
            print(f"  {cat} (importance={info['importance']}): {info['keywords']}")
        return

    # ─── 텍스트 읽기 ───
    if args.text:
        text = args.text
    elif args.file:
        try:
            with open(args.file) as f:
                text = f.read()
        except Exception as e:
            print(json.dumps({"status": "error", "detail": str(e)}, ensure_ascii=False))
            sys.exit(1)
    else:
        # stdin 읽기
        if sys.stdin.isatty():
            sys.stderr.write("텍스트를 입력하세요 (Ctrl+D로 종료):\n")
        text = sys.stdin.read()

    if not text.strip():
        print(json.dumps({"detected": False, "error": "빈 텍스트"}, ensure_ascii=False))
        return

    result = process_message(text, source=args.source,
                             ai_response=args.ai_response,
                             session_id=args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
