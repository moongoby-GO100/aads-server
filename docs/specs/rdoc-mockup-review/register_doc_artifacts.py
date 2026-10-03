#!/usr/bin/env python3
"""R-DOC 목업 검토 정본(plan/prd/spec v2.0.0) 전문을 현재 채팅 세션 아티팩트로 멱등 등록한다.

chat.document_body_not_registered 복구용. 본문은 요약하지 않고 정본
(project_document_revisions.content)을 DB 안에서 그대로 복사한다 — 따옴표/개행 변형 없음.

    register_doc_artifacts.py check      읽기 전용. 정본·git 해시 대조와 등록 상태 출력
    register_doc_artifacts.py apply      단일 트랜잭션 등록(이미 있으면 건너뜀). 사후 해시 검증 실패 시 롤백
    register_doc_artifacts.py verify     등록본 본문 해시 == 정본 해시, 테넌트 필터 확인
    register_doc_artifacts.py rollback   이 스크립트가 만든 ID(결정적 uuid5)만 삭제

DB 접속은 scripts/error_book.py 의 접속 규약을 그대로 쓴다(서버별 사본 금지).
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

TASK_ID = "AADS-RDOC-ARTIFACT-OPEN-REPAIR-20261003"
SESSION_ID = "8bf0405a-1f22-4ad9-bb09-6e0fce8c6339"
SOURCE_COMMIT = "5c88c56b260b9a972ac0c29a6d0df3c40ef92a29"
VERSION = "2.0.0"

# (document_key, subtype, 탭에 보일 제목, 저장소 경로)
DOCS = [
    ("rdoc-mockup-review-plan", "plan",
     "[기획] R-DOC 목업 필수 제출·승인 — 기획 v2.0.0 (미승인)",
     "docs/plans/20261003_AADS_RDOC_MOCKUP_REVIEW_PLAN.md"),
    ("rdoc-mockup-review-spec", "spec",
     "[설계] R-DOC 목업 필수 제출·승인 — 설계 명세 v2.0.0 (미승인)",
     "docs/specs/rdoc-mockup-review/spec.md"),
    ("rdoc-mockup-review-prd", "prd",
     "[PRD] R-DOC 목업 필수 제출·승인 — PRD v2.0.0 (미승인)",
     "docs/prd/20261003_AADS_RDOC_MOCKUP_REVIEW_PRD.md"),
]

REPO = Path(__file__).resolve().parents[3]


def _error_book():
    spec = importlib.util.spec_from_file_location("error_book", REPO / "scripts" / "error_book.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def psql(sql: str) -> str:
    if shutil.which("psql") is None:
        os.environ.pop("PGHOST", None)  # 호스트에 psql 이 없으면 aads-postgres 컨테이너 경로를 쓴다
    argv, env = _error_book()._psql_argv()
    proc = subprocess.run(
        argv + ["-X", "-v", "ON_ERROR_STOP=1", "-At", "-F", "\x1f", "-f", "-"],
        input=sql, text=True, capture_output=True, timeout=120, env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(f"psql 실패: {(proc.stderr or '')[:600]}")
    return (proc.stdout or "").strip()


def lit(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def git_blob_hash(path: str) -> str:
    data = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{SOURCE_COMMIT}:{path}"],
        capture_output=True, check=True, timeout=30,
    ).stdout
    return hashlib.sha256(data).hexdigest()


def artifact_id(document_key: str, revision_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aads:chat-artifact:{SESSION_ID}:{document_key}:{revision_id}"))


def source_rows() -> dict[str, dict]:
    keys = ",".join(lit(d[0]) for d in DOCS)
    out = psql(f"""
        SELECT h.document_key, h.id, r.id, r.revision, r.version, r.content_hash,
               coalesce(h.approved_revision_id::text,''), h.tenant_id, length(r.content), octet_length(r.content)
        FROM project_document_heads h
        JOIN project_document_revisions r ON r.id = h.latest_revision_id
        JOIN chat_sessions s ON s.id = {lit(SESSION_ID)} AND s.tenant_id = h.tenant_id
        WHERE h.project_key = 'AADS' AND h.document_key IN ({keys})
    """)
    rows = {}
    for line in out.splitlines():
        k, head, rev, n, ver, h, appr, tenant, chars, nbytes = line.split("\x1f")
        rows[k] = dict(head=head, rev=rev, revision=int(n), version=ver, hash=h, approved=appr,
                       tenant=tenant, chars=int(chars), bytes=int(nbytes))
    return rows


def preflight() -> dict[str, dict]:
    rows = source_rows()
    for key, _sub, _title, path in DOCS:
        r = rows.get(key)
        if not r:
            raise SystemExit(f"정본 head 없음 또는 세션 tenant 불일치: {key}")
        if r["version"] != VERSION:
            raise SystemExit(f"{key}: 최신 revision 이 {r['version']} — 기대 {VERSION}. 제목/해시를 재확인하라")
        if r["approved"]:
            raise SystemExit(f"{key}: 승인본이 존재한다 — 제목의 '미승인' 이 거짓이 된다. 중단")
        g = git_blob_hash(path)
        if g != r["hash"]:
            raise SystemExit(f"{key}: git {SOURCE_COMMIT[:8]} 해시 {g[:12]} != 정본 {r['hash'][:12]}")
        r["git_path"] = path
    return rows


def existing() -> dict[str, str]:
    out = psql(f"""
        SELECT metadata->>'document_key', id::text FROM chat_artifacts
        WHERE session_id = {lit(SESSION_ID)} AND metadata->>'registered_by' = {lit(TASK_ID)}
    """)
    return dict(line.split("\x1f") for line in out.splitlines() if line)


def cmd_check() -> None:
    rows = preflight()
    have = existing()
    for key, sub, title, path in DOCS:
        r = rows[key]
        print(f"{key} rev{r['revision']} v{r['version']} sha256={r['hash'][:16]}… {r['chars']}자/{r['bytes']}B "
              f"git==정본 OK 미승인 OK → id={artifact_id(key, r['rev'])} 등록={'있음' if key in have else '없음'}")


def cmd_apply() -> None:
    rows = preflight()
    parts = ["BEGIN;"]
    for key, sub, title, path in DOCS:
        r = rows[key]
        aid = artifact_id(key, r["rev"])
        parts.append(f"""
INSERT INTO chat_artifacts (id, session_id, workspace_id, tenant_id, type, title, content, metadata)
SELECT {lit(aid)}::uuid, s.id, s.workspace_id, s.tenant_id, 'report', {lit(title)}, r.content,
       jsonb_build_object(
         'status', 'draft', 'subtype', {lit(sub)}, 'approval', 'unapproved',
         'document_key', h.document_key, 'document_id', h.id::text, 'revision_id', r.id::text,
         'revision', r.revision, 'version', r.version, 'content_sha256', r.content_hash,
         'canonical_source', 'project_document_revisions', 'source_path', {lit(path)},
         'source_commit', {lit(SOURCE_COMMIT)}, 'execution_hub', 'AADS', 'target_project', 'AADS',
         'registered_by', {lit(TASK_ID)})
FROM project_document_heads h
JOIN project_document_revisions r ON r.id = h.latest_revision_id
JOIN chat_sessions s ON s.id = {lit(SESSION_ID)} AND s.tenant_id = h.tenant_id
WHERE h.project_key = 'AADS' AND h.document_key = {lit(key)}
  AND r.version = {lit(VERSION)} AND r.content_hash = {lit(r['hash'])} AND h.approved_revision_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM chat_artifacts a
                  WHERE a.session_id = s.id AND a.metadata->>'document_key' = h.document_key
                    AND a.metadata->>'revision_id' = r.id::text);""")
    checks = " AND ".join(
        f"(SELECT count(*) FROM chat_artifacts a WHERE a.id = {lit(artifact_id(k, rows[k]['rev']))}::uuid "
        f"AND a.session_id = {lit(SESSION_ID)}::uuid AND a.type = 'report' "
        f"AND encode(digest(convert_to(a.content,'UTF8'),'sha256'),'hex') = {lit(rows[k]['hash'])}) = 1"
        for k, *_ in DOCS)
    parts.append(f"""
DO $$ BEGIN
  IF NOT ({checks}) THEN RAISE EXCEPTION 'post-check 실패: 등록본 해시 불일치 또는 누락'; END IF;
END $$;
COMMIT;""")
    psql("\n".join(parts))
    print("apply 완료(사후 해시 검증 통과). 등록 ID:")
    for key, *_ in DOCS:
        print(f"  {key}: {artifact_id(key, rows[key]['rev'])}")


def cmd_verify() -> None:
    rows = preflight()
    other_tenant = "00000000-0000-0000-0000-000000000000"
    bad = 0
    for key, sub, title, path in DOCS:
        r = rows[key]
        aid = artifact_id(key, r["rev"])
        got = psql(f"""
            SELECT type, title, length(content), encode(digest(convert_to(content,'UTF8'),'sha256'),'hex'),
                   (tenant_id = {lit(r['tenant'])}::uuid), coalesce(workspace_id::text,'')
            FROM chat_artifacts WHERE id = {lit(aid)}::uuid AND session_id = {lit(SESSION_ID)}::uuid
              AND tenant_id = {lit(r['tenant'])}::uuid""")
        leak = psql(f"SELECT count(*) FROM chat_artifacts WHERE id = {lit(aid)}::uuid AND tenant_id = {lit(other_tenant)}::uuid")
        if not got:
            print(f"FAIL {key}: 등록본 없음 (id={aid})")
            bad += 1
            continue
        typ, ttl, chars, h, tenant_ok, ws = got.split("\x1f")
        ok = h == r["hash"] and int(chars) == r["chars"] and tenant_ok == "t" and leak == "0"
        bad += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {key} type={typ} {chars}자 sha256={h[:16]}… 정본일치={h == r['hash']} "
              f"테넌트일치={tenant_ok == 't'} 타테넌트조회={leak}건 ws={ws[:8]} title={ttl}")
    if bad:
        raise SystemExit(1)


def cmd_rollback() -> None:
    rows = source_rows()
    ids = [artifact_id(k, rows[k]["rev"]) for k, *_ in DOCS if k in rows]
    if not ids:
        raise SystemExit("정본 조회 실패 — ID 를 계산할 수 없다")
    in_list = ",".join(lit(i) + "::uuid" for i in ids)
    out = psql(f"""
        BEGIN;
        DELETE FROM chat_artifacts WHERE id IN ({in_list}) AND session_id = {lit(SESSION_ID)}::uuid
          AND metadata->>'registered_by' = {lit(TASK_ID)} RETURNING id;
        COMMIT;""")
    print("삭제:", out.replace("\n", ", ") or "없음")


if __name__ == "__main__":
    cmds = {"check": cmd_check, "apply": cmd_apply, "verify": cmd_verify, "rollback": cmd_rollback}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(__doc__)
    cmds[sys.argv[1]]()
