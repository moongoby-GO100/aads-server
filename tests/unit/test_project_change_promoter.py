import pytest


def test_extract_changed_files_from_git_diff():
    from app.services.project_change_promoter import extract_changed_files

    diff = """diff --git a/app/services/context_builder.py b/app/services/context_builder.py
index 111..222 100644
diff --git a/migrations/081_project_change_promoter.sql b/migrations/081_project_change_promoter.sql
new file mode 100644
"""

    assert extract_changed_files(diff) == [
        "app/services/context_builder.py",
        "migrations/081_project_change_promoter.sql",
    ]


def test_classify_pipeline_job_promotes_strategic_categories():
    from app.services.project_change_promoter import classify_pipeline_job

    row = {
        "job_id": "runner-abc12345",
        "project": "AADS",
        "status": "done",
        "instruction": "Project Change Promoter를 추가해 세션 컨텍스트 자동 인지를 개선",
        "result_output": "서비스와 스케줄러를 구현",
        "review_feedback": "PASS",
        "git_diff": """diff --git a/app/services/project_change_promoter.py b/app/services/project_change_promoter.py
new file mode 100644
diff --git a/app/api/pipeline_runner.py b/app/api/pipeline_runner.py
index 111..222 100644
diff --git a/migrations/081_project_change_promoter.sql b/migrations/081_project_change_promoter.sql
new file mode 100644
+CREATE INDEX IF NOT EXISTS idx_memory_facts_strategic_changes
""",
    }

    categories = {change.category for change in classify_pipeline_job(row)}

    assert "architecture_decision" in categories
    assert "feature_change" in categories
    assert "api_contract" in categories
    assert "data_model_change" in categories


def test_classify_memory_fact_promotes_raw_change_event():
    from app.services.project_change_promoter import classify_memory_fact

    row = {
        "id": "fact-1",
        "project": "AADS",
        "category": "file_change",
        "subject": "context_builder.py에 workspace preload 중요 변경 주입 추가",
        "detail": "app/services/context_builder.py 및 app/services/workspace_preloader.py 변경",
        "confidence": 0.8,
    }

    categories = {change.category for change in classify_memory_fact(row)}

    assert "architecture_decision" in categories
    assert "feature_change" in categories


def test_promote_project_changes_script_defaults_to_safe_canary():
    from scripts import promote_project_changes as script

    args = script.parse_args([])

    assert args.days == 14
    assert args.limit == 20
    assert args.dry_run is False
    assert args.no_embed is False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.inserted = []

    async def fetch(self, *_args):
        return self.rows


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_promote_completed_project_changes_dry_run():
    from app.services.project_change_promoter import promote_completed_project_changes

    pool = _FakePool([
        {
            "job_id": "runner-dryrun",
            "project": "AADS",
            "status": "done",
            "instruction": "API 기능 추가",
            "result_output": "",
            "review_feedback": "",
            "git_diff": "diff --git a/app/api/example.py b/app/api/example.py\n",
        }
    ])

    result = await promote_completed_project_changes(pool, dry_run=True, limit=1)

    assert result["status"] == "dry_run"
    assert result["jobs_scanned"] == 1
    assert result["candidate_changes"] >= 2
    assert result["inserted"] == 0
