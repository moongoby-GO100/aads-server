#!/usr/bin/env python3
"""정본문서 목표(ac431290) M1~M9 업무계층(9 Epic -> 27 Story -> 54 Task) 등록기.

- 기본은 --dry-run: 명세 90건을 만들고 자체 검증만 한다(네트워크 없음).
- --apply: 공식 API(POST /goals/{id}/work-items, GET /goals/{id}/tree)만 사용한다.
  인증은 환경변수 AADS_BEARER_TOKEN(사용자 JWT)으로만 받는다. 서비스 키·raw SQL 은 쓰지 않는다.
- 부모가 성공한 뒤에만 하위를 만들고, 409(idempotency_conflict)는 덮어쓰지 않고 기록 후 해당 가지를 중단한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

GOAL_ID = "ac431290-66be-4d00-8bbb-9bf162917240"
GOAL_SHORT = GOAL_ID[:8]
DEFAULT_ACTOR_SESSION = "b749ff17-43d3-4b87-b67f-482b1748b82b"
DESIGN_DOC = "aads-canonical-milestone-hierarchy-audit (document_id c20d5e6a-cd46-4eab-97c8-9d6f4bc76b80, rev2 content_hash 229c722e…1e363)"

# (number, milestone_id, title, [(story_key, title, role, [(task_key, title, criterion)])])
PLAN = [
    (1, "866aa027-b162-44e3-b1e1-3241daa21276", "문서 정본 저장·개정·승인 API", [
        ("m1-s1", "저장·정체성 보장", "Developer", [
            ("m1-s1-t1", "문서 키·프로젝트·해시 계약 확인", "동일 tenant/project/key head 중복 0; 기존 승인 포인터 보존"),
            ("m1-s1-t2", "불변 개정·멱등·버전충돌 검증", "동일 요청 replay 성공, 다른 내용의 같은 버전 409; 이전 개정 hash 불변")]),
        ("m1-s2", "승인 생명주기", "Developer", [
            ("m1-s2-t1", "draft→review→approved 전이 대조", "승인 경로 외 포인터 변경 없음; 충돌 409 근거 연결"),
            ("m1-s2-t2", "감사 이벤트·보관·복구 근거 연결", "actor·revision·generation 이력 및 archive 복구 참조 존재")]),
        ("m1-s3", "권한·DB 계약 검수", "QA", [
            ("m1-s3-t1", "tenant·프로젝트·접근권한 경계 검증", "미인증 401, 타 tenant 차단, grant별 허용/거부를 격리 DB 근거로 구분"),
            ("m1-s3-t2", "migration 왕복·훅·독립검수 연결", "기존 단위·격리 migration·정상 commit hook·독립 review 증거 SHA 연결")]),
    ]),
    (2, "f653c393-a3d2-4d1b-bf3d-5fe6e13b08b6", "세션·러너 승인 문서 브리프 연결", [
        ("m2-s1", "승인본 선택 규칙", "Developer", [
            ("m2-s1-t1", "승인 포인터와 최신 초안 선택 분리", "초안이 더 최신이어도 승인 브리프에는 승인 revision만 포함"),
            ("m2-s1-t2", "tenant·project·사용자별 브리프 접근 검증", "권한 없는 사용자에 본문 미노출; scope 정보 일치")]),
        ("m2-s2", "세션·러너 착수 연결", "Developer", [
            ("m2-s2-t1", "채팅 세션에 승인 브리프 연결 근거 수집", "실제 컨텍스트 provenance·revision·hash 확인; 미관측은 미검증"),
            ("m2-s2-t2", "러너 지시서의 승인 브리프 연결 확인", "착수 입력에 승인 revision ref 존재; 중복 본문·다른 project 혼입 없음")]),
        ("m2-s3", "크기·누락·회귀", "QA", [
            ("m2-s3-t1", "승인 없음·DB 오류·과대본문 복구 확인", "승인본 없음은 비정본 안내; 오류 때 내부본문 유출 없음"),
            ("m2-s3-t2", "관련 테스트·릴리스 SHA 연결", "기존 테스트 결과를 SHA/환경과 연결; 후보와 운영을 구분")]),
    ]),
    (3, "2e7625d6-99fd-42e1-8214-99da0e3f1d33", "기존 문서 대조·운영 검증", [
        ("m3-s1", "기존 문서 무손실 인벤토리", "PM", [
            ("m3-s1-t1", "goal_documents·chat_artifacts·파일 대조", "원본 식별자·파일존재·hash·legacy link를 대조표에 기록"),
            ("m3-s1-t2", "누락·사본·잘못된 경로 분류", "missing/invalid/unmatched 분리; 원본 삭제·자동등록 없음")]),
        ("m3-s2", "파일럿 연결 정합", "PM", [
            ("m3-s2-t1", "파일럿 74·92·109·110·119 연결 추적", "legacy link와 승인 revision hash·project 일치 증거"),
            ("m3-s2-t2", "연결 해제·중복 정본 방지 확인", "잘못된 연결 대상만 롤백 가능; duplicate head 검증 근거")]),
        ("m3-s3", "운영 인수인계 근거", "QA", [
            ("m3-s3-t1", "M9의 인증 화면·API 증거 재사용", "20261012 캡처/로그를 연결; 로그인 게이트를 기능 성공으로 세지 않음"),
            ("m3-s3-t2", "CEO 완료 판정·배포·핸드오버 대조", "20261014 M3 판정과 실제 release/handover refs 연결")]),
    ]),
    (4, "2e18a186-cf99-4ac7-9db6-10e4f82ae33f", "서버 문서 인벤토리·프로젝트별 연결", [
        ("m4-s1", "현재 운영 서버 기준 인벤토리", "PM", [
            ("m4-s1-t1", "서버 원장·프로젝트·실제 경로 대조", "AADS=116,GO100/KIS=14,SF/NTV2/NAS/ACCT=114; 구244는 이관/역사 자료"),
            ("m4-s1-t2", "운영 문서와 구서버 자료 분리", "운영 누락·구서버 archive·접속불가 구분; 구244를 운영 장애로 판단하지 않음")]),
        ("m4-s2", "고유 문서·사본 분류", "PM", [
            ("m4-s2-t1", "내용 hash·경로·document_key 대응표 작성", "같은 내용 사본과 서로 다른 개정 분리; 기존 key/path 보존"),
            ("m4-s2-t2", "프로젝트별 미등록·중복 배치 후보 작성", "읽기 전용 조회로 건별 대상·영향·승인 여부·rollback 기록")]),
        ("m4-s3", "승인된 배치 연결", "PM", [
            ("m4-s3-t1", "CEO 승인된 배치와 actor 증거 연결", "승인된 범위에만 연결; service actor 추적; 미승인 일괄연결 금지"),
            ("m4-s3-t2", "연결 전후 hash·해제 API 검증", "잘못된 연결 0; 동일 key 개정 불변; 기존 파일럿 재실행 금지")]),
    ]),
    (5, "942df3f2-5512-4c19-ab67-4b9a7f706d60", "정본 본문 검색", [
        ("m5-s1", "정본 색인 생명주기", "Developer", [
            ("m5-s1-t1", "승인본·최신본 청크와 본문 hash 대조", "canonical URI·revision·승인/초안 표시가 포인터와 일치"),
            ("m5-s1-t2", "색인 보존·승인갱신·보관 처리 확인", "기존 색인 삭제·재색인 금지; 최신 데이터는 SELECT로만 확인")]),
        ("m5-s2", "검색 품질", "Developer", [
            ("m5-s2-t1", "F1 정본 상위 결과 보장 작업 연결", "기존 runner-1467686b 재사용; 새 중복 제출 금지; 본문 질의로 정본 상위 k 노출"),
            ("m5-s2-t2", "프로젝트·tenant 필터·회수 검증", "필터 적용 전후 타 tenant 미노출·grant 회수 즉시 차단; 운영/격리 구분")]),
        ("m5-s3", "검색 결과에서 문서 열기", "Developer", [
            ("m5-s3-t1", "F2 canonical URI에서 개정 상세 이동 연결", "runner-a91f88a0 재사용; 검색→정확 project/key/revision 상세 열기"),
            ("m5-s3-t2", "F3 최초 진입 캐시와 수동 재스캔 연결", "runner-a91f88a0 재사용; 첫 진입 force=false, 명시 새로고침만 force=true; 측정값만 보고")]),
    ]),
    (6, "318c4707-5a92-42bc-b503-ec7040cd4d38", "세션 정본 게이트", [
        ("m6-s1", "세 진입점 게이트 계약", "Developer", [
            ("m6-s1-t1", "goals 문서 등록·러너 제출·pre-commit 연결 대조", "세 진입점의 off/shadow/enforce 동작·호출 SHA를 분리"),
            ("m6-s1-t2", "오류·시간상한·off 검증", "기존 완료기준 off 쿼리 0·300ms 상한·fail-open 근거 연결; 실측 부재는 미검증")]),
        ("m6-s2", "그림자 운영 계측", "QA", [
            ("m6-s2-t1", "경고·오탐 표본과 분모 수집", "7일 구간·경고 건수·오탐 분모·누락 사유 기록; 추정률 금지"),
            ("m6-s2-t2", "중복 경고·실패 원인 분류", "문서 미등록·도구장애·권한·구버전 경로를 구별; 관련 목표 결과 참조")]),
        ("m6-s3", "강제 전환 결정", "PM", [
            ("m6-s3-t1", "실측에 따른 강제 전환 권장안 작성", "그림자 결과·사용자 영향·복구 경로가 포함된 승인안"),
            ("m6-s3-t2", "정책 승인·적용·원복 근거 연결", "CEO 승인 없이 enabled/enforce 변경 금지; M7 provenance와 연결")]),
    ]),
    (7, "29dd7c05-cc40-4209-b8ff-cc1765b40b7e", "문서 저장 규칙·정본 등록 도구", [
        ("m7-s1", "문서 저장·명명 규칙", "PM", [
            ("m7-s1-t1", "kind별 저장 경로·한글 제목 확인", "plan/prd/design/report/spec 경로와 YYYYMMDD_PROJECT_한글제목·H1 일치"),
            ("m7-s1-t2", "document_key·동일 키 새판본 규칙 확인", "kebab key 유지; 날짜 사본·중복 정본·기존 경로 변경 금지")]),
        ("m7-s2", "초안 등록 도구", "Developer", [
            ("m7-s2-t1", "content/file_path·멱등·충돌 계약 확인", "동일 내용 replay·generation conflict·출처경로 한계 검증"),
            ("m7-s2-t2", "등록·승인 권한과 열람 참조 확인", "도구는 초안까지만; 승인 포인터 불변; 반환 view/report_line 그대로 사용")]),
        ("m7-s3", "전체 세션 적용 확인", "QA", [
            ("m7-s3-t1", "L1 scope·enabled·compiled provenance 대조", "에셋 존재와 실제 applied_assets 적용을 분리; 관련 R-DOC 목표 결과 재사용"),
            ("m7-s3-t2", "기간별 문서 등록률·미등록 큐 산출", "기간·분모·등록/미등록 원인·담당 기록; 같은 문서 재등록 금지")]),
    ]),
    (8, "c7f8f638-bb5b-4bc4-af02-60f6f260a04b", "승인 정본·코드·AAG 대조", [
        ("m8-s1", "요구사항 대응표", "PM", [
            ("m8-s1-t1", "승인 4문서의 revision·hash 기준 고정", "approved FK와 content hash·baseline SHA를 함께 기록"),
            ("m8-s1-t2", "요구사항→코드→테스트 대응표 대조", "99행은 20261012 역사값; 최신 검증 없이 현재 MATCH/MISMATCH 수로 보고 금지")]),
        ("m8-s2", "AAG 최신성·운영 대조", "Developer", [
            ("m8-s2-t1", "운영 AAG 포인터·origin·권한성 확인", "N-02 재조회; stale graph를 현재 구현 증거로 사용 금지; 공유 dirty 보존"),
            ("m8-s2-t2", "격리 rescan·누락 edge·검증환경 분리", "후보 rescan과 운영 authoritative를 분리; unresolved/finding 원인 기록")]),
        ("m8-s3", "잔여 결함 판정·인계", "PM", [
            ("m8-s3-t1", "N-01·N-03·C-S6-06 등 후속 책임 분리", "정본정책 불일치/코드결함/미검증/CEO결정 각각 담당·선행 기록"),
            ("m8-s3-t2", "독립 review·CEO 완료 판정 연결", "runner-a4df37a8 AI review와 20261014 M8 판정 연결; 결함 수정 완료로 확대 해석 금지")]),
    ]),
    (9, "a55743c9-e533-41a5-bab3-7a17a8f8fa89", "문서 화면·권한·실패 복구", [
        ("m9-s1", "사용자 핵심 화면", "QA", [
            ("m9-s1-t1", "목록→검색→정확 개정 상세·이력 확인", "desktop/mobile 실캡처; 승인·초안·승인본 선택·ACCT 전환 확인"),
            ("m9-s1-t2", "폰 레이아웃·새로고침·세션 보존 확인", "관련 runner-a64c6592 결과 참조; 가로넘침·터치영역·복귀 검증")]),
        ("m9-s2", "운영 권한 검증", "QA", [
            ("m9-s2-t1", "다른 tenant 실제 로그인 비노출 검증", "유효한 비내부 tenant 신원 사용; X-Tenant-ID 403과 구분; 확보 전 blocked"),
            ("m9-s2-t2", "비관리자 grant 전·허용·회수 검증", "운영 신원·한정 승인 grant 필요; 승인 전 쓰기 금지; 검증 뒤 원복 SELECT")]),
        ("m9-s3", "실패 복구·릴리스 인수", "QA", [
            ("m9-s3-t1", "401·403·네트워크·스캔실패 복구 확인", "사용자 다음 행동·재시도 UI를 실제 화면으로 확인; 실패시 HTTP→health→process"),
            ("m9-s3-t2", "릴리스·핸드오버·최종 완료 근거 연결", "새 릴리스 필요시 별도 승인+bluegreen·same-digest·rollback·5분 P0/P1; DB handover 확인")]),
    ]),
]

# 마일스톤별 기존 evidence ref. 원본 milestones.evidence 는 수정하지 않고 참조만 한다.
EVIDENCE = {
    1: "milestones.evidence(M1) 원본, 설계 보고서 " + DESIGN_DOC,
    2: "milestones.evidence(M2) 원본, 설계 보고서 " + DESIGN_DOC,
    3: "파일럿 74·92·109·110·119, 20261012 인증 화면 캡처/로그(M9 재사용), CEO 완료 판정 2026-10-14 11:29:27 KST; M3 evidence 일부 문구는 과거 미완료 상태로 남아 있으며 덮어쓰지 않는다",
    4: "milestones.evidence 가 NULL(증거 연결 부족이며 기능 실패의 증거 아님); 현재 ACCT=cafe24_114, jinah244(구244)는 역사 자료",
    5: "F1 runner-1467686b, F2/F3 runner-a91f88a0, CEO 완료 판정 2026-10-14 11:29:33 KST",
    6: "milestones.evidence 가 NULL; 별도 게이트 목표 0361c451… 결과 참조(같은 정책 재구현 금지)",
    7: "milestones.evidence 가 NULL; 별도 R-DOC 목표 a6cc6511… 결과 참조(같은 정책 재구현 금지)",
    8: "runner-a4df37a8 AI review, CEO 완료 판정 2026-10-14 11:29:35 KST, 99행 대응표는 20261012 역사값",
    9: "정본 가시성 runner-a64c6592, 후속 runner-9d30d5ad, 20261012 인증 화면 캡처/로그; 재작업 후보 기록(card e1b4db1d-ae3a-4a52-8357-4a032879fe74, 참조 runner-d51bf08f cancelled)",
}

# 논리적 선행·병렬 조건 재정의(없으면 기본 규칙). 공식 dependency API 없이 description 에만 적는다.
PRECONDITION_OVERRIDES = {
    "m5-s3-t2": "m5-s3-t1 과 병렬 가능(둘 다 runner-a91f88a0 결과를 재사용); 새 코드 작업 제출 금지",
    "m6-s3-t1": "선행: m6-s2-t1, m6-s2-t2 의 실측 결과",
    "m6-s3-t2": "선행: m6-s3-t1 승인안과 CEO 승인; M7 provenance(m7-s3-t1) 참조",
    "m3-s3-t1": "선행 없음; M9 의 20261012 증거를 재사용(재실행 아님)",
    "m9-s2-t2": "선행: m9-s2-t1 의 비내부 tenant 신원 확보 여부; 한정 승인 grant 없이는 쓰기 금지",
}


def key_for(*parts: str) -> str:
    return f"canonical-doc-goal-{GOAL_SHORT}-" + "-".join(parts)


def _desc(level: str, role: str, title: str, deliverable: str, scope: str,
          evidence: str, hist: str, pre: str) -> str:
    return "\n".join([
        f"[초안 명세·{level}] {title}",
        f"책임 역할: 미배정 (권장 역할: {role}; 실제 assignment_id 없음 — 역할명만으로 배정하지 않음)",
        f"산출물: {deliverable}",
        f"범위(전/후): {scope}",
        f"기존 evidence ref: {evidence}",
        f"역사적 완료 근거 / 새 후속 검증: {hist}",
        f"선행·병렬 조건: {pre}",
        "등록은 실행 승인·자동 grant·목표 재개와 무관하며 status 는 API 가 부여하는 기본값 그대로 둔다.",
    ])


HIST = ("역사적 완료 근거 = 해당 마일스톤의 CEO 완료 판정(2026-10-14)과 기존 evidence 원본이며 변경하지 않는다. "
        "이 항목의 달성은 새 구현 완료가 아니라 후속 검증 명세이고, 증거 없이 completed 로 만들지 않는다.")


def build_items() -> list[dict]:
    items: list[dict] = []
    for number, milestone_id, mtitle, stories in PLAN:
        mkey = f"m{number}"
        ev = EVIDENCE[number]
        items.append({
            "ref": mkey, "type": "epic", "parent_ref": None, "milestone_id": milestone_id,
            "title": f"M{number} {mtitle}", "priority": "P1",
            "idempotency_key": key_for(mkey),
            "description": _desc(
                "Epic", "PM", f"M{number} {mtitle} 의 구현·검증·운영 근거 추적",
                "Story 3개 + Task 6개의 상세 명세와 각 수용기준 충족 판정 기록",
                f"완료된 M{number} 를 되돌리지 않고 후속 검증 명세만 추가; 마일스톤 status/progress/completion_criteria/evidence/dispatch_note 불변",
                ev, HIST, "하위 Story 는 서로 병렬 가능; 이 Epic 이 부모 등록의 선행"),
            "acceptance_criteria": [
                f"M{number} 완료 판정과 기존 evidence 원본 보존",
                "하위 Story 3개·Task 6개가 draft 명세로 존재",
                "신규 Task 를 증거 없이 completed 로 표시하지 않음",
            ],
        })
        for story_key, stitle, role, tasks in stories:
            items.append({
                "ref": story_key, "type": "story", "parent_ref": mkey, "milestone_id": milestone_id,
                "title": stitle, "priority": "P2",
                "idempotency_key": key_for(*story_key.split("-")),
                "description": _desc(
                    "Story", role, stitle,
                    f"{stitle} 범위의 Task 2개 결과 및 수용기준 판정 요약",
                    f"M{number} 안의 '{stitle}' 한정; 다른 Story·다른 project 변경 없음",
                    ev, HIST, "하위 Task 는 번호 순서로 진행(t1→t2), 다른 Story 와는 병렬 가능"),
                "acceptance_criteria": [t[2] for t in tasks],
            })
            for task_key, ttitle, criterion in tasks:
                pre = PRECONDITION_OVERRIDES.get(
                    task_key, "같은 Story 의 이전 Task 완료 후" if task_key.endswith("t2") else "선행 없음(Story 시작 가능)")
                items.append({
                    "ref": task_key, "type": "task", "parent_ref": story_key, "milestone_id": milestone_id,
                    "title": ttitle, "priority": "P2",
                    "idempotency_key": key_for(*task_key.split("-")),
                    "description": _desc(
                        "Task", role, ttitle,
                        f"검증 기록(SELECT/로그/SHA 연결)과 수용기준 판정: {criterion}",
                        f"M{number}/{stitle} 한정, 읽기 전용 확인 우선; 원본 삭제·재실행·정책 변경 없음",
                        ev, HIST, pre),
                    "acceptance_criteria": [criterion],
                })
    return items


def self_check(items: list[dict]) -> dict:
    keys = [i["idempotency_key"] for i in items]
    refs = {i["ref"]: i for i in items}
    counts = {t: sum(1 for i in items if i["type"] == t) for t in ("epic", "story", "task")}
    expect_parent = {"epic": None, "story": "epic", "task": "story"}
    bad = []
    for i in items:
        p = i["parent_ref"]
        if expect_parent[i["type"]] is None:
            if p is not None:
                bad.append(i["ref"])
        else:
            if p not in refs or refs[p]["type"] != expect_parent[i["type"]] or refs[p]["milestone_id"] != i["milestone_id"]:
                bad.append(i["ref"])
    assert counts == {"epic": 9, "story": 27, "task": 54}, counts
    assert len(set(keys)) == len(keys) == 90, "duplicate idempotency_key"
    assert not bad, bad
    assert all(len(k) <= 200 for k in keys)
    return {"counts": counts, "total": len(items), "unique_keys": len(set(keys)), "bad_parent_links": bad}


def _call(base: str, token: str, session: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "X-Chat-Session-ID": session,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def _flatten(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten(node.get("children", [])))
    return out


def apply(items: list[dict], out_path: str) -> int:
    token = os.environ.get("AADS_BEARER_TOKEN", "").strip()
    if not token:
        print("AADS_BEARER_TOKEN 이 없어 등록하지 않습니다(차단).", file=sys.stderr)
        return 2
    base = os.environ.get("AADS_API_BASE", "https://aads.newtalk.kr/api/v1").rstrip("/")
    session = os.environ.get("AADS_ACTOR_SESSION_ID", DEFAULT_ACTOR_SESSION)
    tree_path = f"/goals/{GOAL_ID}/tree?include_history=true"
    st, before = _call(base, token, session, "GET", tree_path)
    if st != 200:
        print(f"tree 조회 실패 HTTP {st}: {before}", file=sys.stderr)
        return 2
    before_n = len(_flatten(before.get("items", [])))
    created: dict[str, dict] = {}
    log: list[dict] = []
    blocked: set[str] = set()
    for it in items:
        if it["parent_ref"] and (it["parent_ref"] in blocked or it["parent_ref"] not in created):
            blocked.add(it["ref"])
            log.append({"ref": it["ref"], "result": "skipped_parent_not_created"})
            continue
        body = {k: it[k] for k in ("type", "milestone_id", "title", "description",
                                   "acceptance_criteria", "priority", "idempotency_key")}
        body["parent_id"] = created[it["parent_ref"]]["id"] if it["parent_ref"] else None
        st, res = _call(base, token, session, "POST", f"/goals/{GOAL_ID}/work-items", body)
        if st in (200, 201):
            created[it["ref"]] = res["work_item"]
            log.append({"ref": it["ref"], "http": st, "id": res["work_item"]["id"],
                        "idempotent_replay": res.get("idempotent_replay")})
        else:
            blocked.add(it["ref"])
            log.append({"ref": it["ref"], "http": st, "error": res.get("detail")})
    replay_new = 0
    for it in items:
        if it["ref"] not in created:
            continue
        body = {k: it[k] for k in ("type", "milestone_id", "title", "description",
                                   "acceptance_criteria", "priority", "idempotency_key")}
        body["parent_id"] = created[it["parent_ref"]]["id"] if it["parent_ref"] else None
        st, res = _call(base, token, session, "POST", f"/goals/{GOAL_ID}/work-items", body)
        if not (st in (200, 201) and res.get("idempotent_replay") is True):
            replay_new += 1
    st, after = _call(base, token, session, "GET", tree_path)
    after_n = len(_flatten(after.get("items", []))) if st == 200 else None
    result = {"goal_id": GOAL_ID, "before_count": before_n, "after_count": after_n,
              "created_ids": {k: v["id"] for k, v in created.items()}, "log": log,
              "replay_non_idempotent": replay_new,
              "complete": len(created) == len(items) and replay_new == 0}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(json.dumps({k: result[k] for k in ("before_count", "after_count", "replay_non_idempotent", "complete")}))
    return 0 if result["complete"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--emit-spec", metavar="PATH", help="명세 JSON 을 파일로 쓴다")
    ap.add_argument("--result", default="work_hierarchy_registration_result.json")
    args = ap.parse_args()
    items = build_items()
    print(json.dumps(self_check(items), ensure_ascii=False))
    if args.emit_spec:
        with open(args.emit_spec, "w", encoding="utf-8") as fh:
            json.dump({"goal_id": GOAL_ID, "items": items}, fh, ensure_ascii=False, indent=2)
    if args.apply:
        return apply(items, args.result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
