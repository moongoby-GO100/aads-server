"""release_migration_autoverify AI 교차 검토 캐시 및 리비전 갱신 단위 테스트.

요구사항:
1. 본문 해시 동일 → 새 리비전 등록 안 함, 기존 판정 사용
2. 본문 해시 다름 → 새 리비전 등록 + 재검토 예약 + 옛 reject 미사용
3. 409 경합 처리 (동시 배포 시 최신 리비전을 다시 읽어 판정, 무한 루프 없음)
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("autoverify", ROOT / "scripts/release_migration_autoverify.py")
av = importlib.util.module_from_spec(spec)
spec.loader.exec_module(av)


class FakeDB:
    def __init__(self):
        # key -> {"generation": int, "latest_revision_id": str, "revisions": {rev_id: {...}}, "reviews": {rev_id: [...]}}
        self.docs: dict[str, dict[str, Any]] = {}

    def psql(self, sql: str, *args, **kwargs) -> subprocess.CompletedProcess:
        # 1. _latest_revision_info
        if "COALESCE(r.content_hash, '')" in sql and "COALESCE(r.version, '')" in sql:
            for key, doc in self.docs.items():
                if f"h.document_key='{key}'" in sql:
                    rev = doc.get("latest_revision_id") or ""
                    gen = doc.get("generation", 0)
                    rev_data = doc.get("revisions", {}).get(rev, {})
                    chash = rev_data.get("content_hash", "")
                    ver = rev_data.get("version", "1.0.0")
                    return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{rev}|{gen}|{chash}|{ver}\n")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

        # 2. _head_generation
        if "latest_revision_id::text || '|' || generation" in sql:
            for key, doc in self.docs.items():
                if f"document_key='{key}'" in sql:
                    rev = doc.get("latest_revision_id") or ""
                    gen = doc.get("generation", 0)
                    return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{rev}|{gen}\n")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

        # 3. _reviews
        if "project_document_reviews" in sql:
            for key, doc in self.docs.items():
                if f"h.document_key='{key}'" in sql:
                    target_rev = None
                    if "r.revision_id::text='" in sql:
                        target_rev = sql.split("r.revision_id::text='", 1)[1].split("'", 1)[0]
                    else:
                        target_rev = doc.get("latest_revision_id")

                    rows = doc.get("reviews", {}).get(target_rev, [])
                    data = {"rev": target_rev, "rows": rows}
                    return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(data) + "\n")
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")


class FakeHTTP:
    def __init__(self, db: FakeDB):
        self.db = db
        self.calls: list[dict[str, Any]] = []
        self.post_docs_hook = None

    def urlopen(self, req: urllib.request.Request, timeout: int = 120):
        url = req.full_url
        data = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls.append({"url": url, "data": data, "headers": dict(req.headers)})

        if url.endswith("/documents"):
            if self.post_docs_hook:
                return self.post_docs_hook(req, data)
            key = data["document_key"]
            doc = self.db.docs.setdefault(key, {"generation": 0, "latest_revision_id": None, "revisions": {}, "reviews": {}})
            new_rev_id = f"rev-{len(doc['revisions']) + 1}"
            doc["generation"] += 1
            doc["latest_revision_id"] = new_rev_id
            content = data["content"]
            doc["revisions"][new_rev_id] = {
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "version": data["version"],
                "content": content,
            }
            res_payload = {
                "document_id": f"head-{key}",
                "revision_id": new_rev_id,
                "generation": doc["generation"],
            }
            return io.BytesIO(json.dumps(res_payload).encode("utf-8"))

        if "/review" in url:
            return io.BytesIO(b'{"status": "queued"}')

        return io.BytesIO(b"{}")


def test_autoverify_review_same_hash_does_not_register_new_revision(tmp_path, monkeypatch):
    """(a) 본문 해시 동일 → 새 리비전 등록 안 함, 기존 판정 사용."""
    mig_file = tmp_path / "20990101_sample.sql"
    mig_file.write_text("CREATE TABLE IF NOT EXISTS sample_test (id int);\n")
    release_sha = "1111222233334444555566667777888899990000"

    items = av.manifest_items([mig_file])
    key = "migration-autoverify-m-" + av.manifest_sha(items)[:12]
    content = av.review_content([mig_file], items, release_sha)
    chash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    db = FakeDB()
    # 기존 문서가 같은 본문 해시를 가지며 이미 통과 판정이 있는 상태
    db.docs[key] = {
        "generation": 1,
        "latest_revision_id": "rev-1",
        "revisions": {
            "rev-1": {
                "content_hash": chash,
                "version": "1.0.0",
                "content": content,
            }
        },
        "reviews": {
            "rev-1": [
                {"model": "codex", "provider": "openai", "cross": True, "verdict": "pass", "reason": ""},
                {"model": "claude", "provider": "anthropic", "cross": False, "verdict": "pass", "reason": ""},
            ]
        },
    }
    http = FakeHTTP(db)

    monkeypatch.setattr(av, "_psql", db.psql)
    monkeypatch.setattr(av, "_token", lambda: "test-token")
    monkeypatch.setattr(av, "source_task_for", lambda paths: "runner-test1234")
    monkeypatch.setattr(urllib.request, "urlopen", http.urlopen)

    result = av.review([mig_file], release_sha)

    # 1. 즉시 통과 (None 반환)
    assert result is None
    # 2. 새 리비전 등록(POST /documents) 호출 없음
    doc_posts = [c for c in http.calls if c["url"].endswith("/documents")]
    assert len(doc_posts) == 0
    # 3. 이미 판정이 있으므로 review kick 호출도 없음
    review_kicks = [c for c in http.calls if "/review" in c["url"]]
    assert len(review_kicks) == 0


def test_autoverify_review_different_hash_registers_new_revision_and_ignores_stale_reject(tmp_path, monkeypatch):
    """(b) 본문 해시 다름 → 새 리비전 등록 + 재검토 예약 + 옛 reject 미사용."""
    mig_file = tmp_path / "20990101_sample.sql"
    mig_file.write_text("CREATE TABLE IF NOT EXISTS sample_test (id int);\n")
    release_sha = "2222333344445555666677778888999900001111"

    items = av.manifest_items([mig_file])
    key = "migration-autoverify-m-" + av.manifest_sha(items)[:12]
    stale_hash = "0" * 64  # 이전 릴리스의 낡은 해시

    db = FakeDB()
    # rev-old 에는 codex reject 가 남아있는 상태
    db.docs[key] = {
        "generation": 1,
        "latest_revision_id": "rev-old",
        "revisions": {
            "rev-old": {
                "content_hash": stale_hash,
                "version": "1.0.0",
                "content": "old stale content",
            }
        },
        "reviews": {
            "rev-old": [
                {"model": "codex", "provider": "openai", "cross": True, "verdict": "reject", "reason": "적용·롤백 파일 없음"},
            ],
            "rev-2": [
                {"model": "codex", "provider": "openai", "cross": True, "verdict": "pass", "reason": ""},
                {"model": "claude", "provider": "anthropic", "cross": False, "verdict": "pass", "reason": ""},
            ],
        },
    }
    http = FakeHTTP(db)

    monkeypatch.setattr(av, "_psql", db.psql)
    monkeypatch.setattr(av, "_token", lambda: "test-token")
    monkeypatch.setattr(av, "source_task_for", lambda paths: "runner-test1234")
    monkeypatch.setattr(urllib.request, "urlopen", http.urlopen)

    result = av.review([mig_file], release_sha)

    # 1. 옛 reject ("적용·롤백 파일 없음")로 즉시 실패하지 않고, 새 리비전의 pass 로 성공
    assert result is None

    # 2. 새 리비전 등록(POST /documents) 호출 확인
    doc_posts = [c for c in http.calls if c["url"].endswith("/documents")]
    assert len(doc_posts) == 1
    post_data = doc_posts[0]["data"]
    assert post_data["document_key"] == key
    assert post_data["expected_generation"] == 1
    assert post_data["version"] == "1.0.1"
    assert post_data["source_task_id"] == "runner-test1234"

    # 3. 재검토 예약(_kick_review) 호출 확인 (새 리비전 rev-2 에 대해)
    review_kicks = [c for c in http.calls if "/review" in c["url"]]
    assert len(review_kicks) == 1
    assert review_kicks[0]["data"]["revision_id"] == "rev-2"
    assert review_kicks[0]["data"]["expected_generation"] == 2


def test_autoverify_review_409_conflict_rereads_latest_revision_without_loop(tmp_path, monkeypatch):
    """(c) 409 경합 처리: 동시 배포 시 최신 리비전을 다시 읽어 판정, 무한 루프 없음."""
    mig_file = tmp_path / "20990101_sample.sql"
    mig_file.write_text("CREATE TABLE IF NOT EXISTS sample_test (id int);\n")
    release_sha = "3333444455556666777788889999000011112222"

    items = av.manifest_items([mig_file])
    key = "migration-autoverify-m-" + av.manifest_sha(items)[:12]
    stale_hash = "0" * 64

    db = FakeDB()
    db.docs[key] = {
        "generation": 1,
        "latest_revision_id": "rev-1",
        "revisions": {
            "rev-1": {
                "content_hash": stale_hash,
                "version": "1.0.0",
                "content": "old content",
            }
        },
        "reviews": {
            "rev-1": [],
            "rev-concurrent": [
                {"model": "codex", "provider": "openai", "cross": True, "verdict": "pass", "reason": ""},
                {"model": "claude", "provider": "anthropic", "cross": False, "verdict": "pass", "reason": ""},
            ],
        },
    }
    http = FakeHTTP(db)

    # POST /documents 시 동시 배포에 의한 409 conflict 발생 시뮬레이션
    def conflict_hook(req, data):
        # 경쟁 배포가 이미 rev-concurrent(generation 2)를 올림
        db.docs[key]["generation"] = 2
        db.docs[key]["latest_revision_id"] = "rev-concurrent"
        db.docs[key]["revisions"]["rev-concurrent"] = {
            "content_hash": hashlib.sha256(data["content"].encode("utf-8")).hexdigest(),
            "version": "1.0.1",
            "content": data["content"],
        }
        raise urllib.error.HTTPError(
            req.full_url, 409, "generation_conflict", hdrs={}, fp=io.BytesIO(b'{"detail":"generation_conflict"}')
        )

    http.post_docs_hook = conflict_hook

    monkeypatch.setattr(av, "_psql", db.psql)
    monkeypatch.setattr(av, "_token", lambda: "test-token")
    monkeypatch.setattr(av, "source_task_for", lambda paths: "runner-test1234")
    monkeypatch.setattr(urllib.request, "urlopen", http.urlopen)

    result = av.review([mig_file], release_sha)

    # 1. 동시 배포가 올린 최신 리비전을 읽고 판정 통과
    assert result is None

    # 2. POST /documents 는 단 1회만 시도(무한 루프 없음)
    doc_posts = [c for c in http.calls if c["url"].endswith("/documents")]
    assert len(doc_posts) == 1

    # 3. 최신 리비전인 rev-concurrent 에 대해 review kick
    review_kicks = [c for c in http.calls if "/review" in c["url"]]
    assert len(review_kicks) == 1
    assert review_kicks[0]["data"]["revision_id"] == "rev-concurrent"
    assert review_kicks[0]["data"]["expected_generation"] == 2


def test_autoverify_review_initial_document_creation(tmp_path, monkeypatch):
    """문서가 처음 생성되는 경우(rev is None): 버전 1.0.0 등록 및 검토 예약."""
    mig_file = tmp_path / "20990101_init.sql"
    mig_file.write_text("CREATE TABLE IF NOT EXISTS init_test (id int);\n")
    release_sha = "4444555566667777888899990000111122223333"

    db = FakeDB()
    http = FakeHTTP(db)

    # 검토가 큐잉된 후 pass 결과 주입
    orig_post_docs = http.post_docs_hook

    def create_and_seed_review(req, data):
        key = data["document_key"]
        doc = db.docs.setdefault(key, {"generation": 0, "latest_revision_id": None, "revisions": {}, "reviews": {}})
        doc["generation"] = 1
        doc["latest_revision_id"] = "rev-1"
        doc["revisions"]["rev-1"] = {
            "content_hash": hashlib.sha256(data["content"].encode("utf-8")).hexdigest(),
            "version": data["version"],
            "content": data["content"],
        }
        doc["reviews"]["rev-1"] = [
            {"model": "codex", "provider": "openai", "cross": True, "verdict": "pass", "reason": ""},
            {"model": "claude", "provider": "anthropic", "cross": False, "verdict": "pass", "reason": ""},
        ]
        return io.BytesIO(json.dumps({"document_id": f"head-{key}", "revision_id": "rev-1", "generation": 1}).encode("utf-8"))

    http.post_docs_hook = create_and_seed_review

    monkeypatch.setattr(av, "_psql", db.psql)
    monkeypatch.setattr(av, "_token", lambda: "test-token")
    monkeypatch.setattr(av, "source_task_for", lambda paths: "runner-test1234")
    monkeypatch.setattr(urllib.request, "urlopen", http.urlopen)

    result = av.review([mig_file], release_sha)

    assert result is None
    doc_posts = [c for c in http.calls if c["url"].endswith("/documents")]
    assert len(doc_posts) == 1
    assert doc_posts[0]["data"]["version"] == "1.0.0"
    assert doc_posts[0]["data"]["expected_generation"] == 0


def test_autoverify_review_new_revision_reject_verdict(tmp_path, monkeypatch):
    """새 리비전 등록 후 새 리비전의 검토 결과가 reject 인 경우 정상 실패 판정."""
    mig_file = tmp_path / "20990101_sample.sql"
    mig_file.write_text("CREATE TABLE IF NOT EXISTS sample_test (id int);\n")
    release_sha = "5555666677778888999900001111222233334444"

    items = av.manifest_items([mig_file])
    key = "migration-autoverify-m-" + av.manifest_sha(items)[:12]

    db = FakeDB()
    db.docs[key] = {
        "generation": 1,
        "latest_revision_id": "rev-1",
        "revisions": {
            "rev-1": {
                "content_hash": "0" * 64,
                "version": "1.0.0",
                "content": "old",
            }
        },
        "reviews": {
            "rev-1": [],
            "rev-2": [
                {"model": "codex", "provider": "openai", "cross": True, "verdict": "reject", "reason": "테이블 명명 규칙 위반"},
            ],
        },
    }
    http = FakeHTTP(db)

    monkeypatch.setattr(av, "_psql", db.psql)
    monkeypatch.setattr(av, "_token", lambda: "test-token")
    monkeypatch.setattr(av, "source_task_for", lambda paths: "runner-test1234")
    monkeypatch.setattr(urllib.request, "urlopen", http.urlopen)

    result = av.review([mig_file], release_sha)

    assert result is not None
    assert "AI 교차 검토 불통과" in result
    assert "테이블 명명 규칙 위반" in result
