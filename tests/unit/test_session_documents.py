"""세션 문서 수집기 — 경로 추출·링크·필터.

DB 를 타지 않는 순수 부분만 본다. 2026-09-15 문서파일 탭이 비어 있던
원인이 "결과 본문에는 경로가 있는데 읽지 않았다" 였으므로, 그 추출이
실제 결과 문자열에서 동작하는지를 고정한다.
"""

from app.services import session_documents as sd


class TestPathExtraction:
    def test_write_marker_is_read(self):
        content = (
            "[AADS 파일 패치 완료 — app/routers/chat.py] 61줄 → 16줄 교체\n"
            "[AADS 파일 쓰기 완료 — /root/aads/aads-server/docs/report.md] 1,024 bytes"
        )
        found = sd._MARK_RE.findall(content)
        assert "/root/aads/aads-server/docs/report.md" in found
        assert "app/routers/chat.py" in found

    def test_shell_redirect_and_tee(self):
        content = "$ cat > /root/aads/aads-server/reports/x.html <<'EOF'\n$ echo hi | tee -a /tmp/y.md"
        assert "/root/aads/aads-server/reports/x.html" in sd._REDIRECT_RE.findall(content)
        assert "/tmp/y.md" in sd._TEE_RE.findall(content)

    def test_html_markup_is_not_mistaken_for_a_path(self):
        # `<div>` 같은 태그가 리다이렉트로 잡히면 목록이 쓰레기가 된다.
        assert sd._REDIRECT_RE.findall("<div class='a'>본문</div>") == []

    def test_runner_diffstat(self):
        content = (
            " 작업/결과/20260915_업무흐름도식화/SUMMARY.md       | 200 ++++++\n"
            " .../20260915_업무흐름도식화/forms_inventory.html   | 104 ++++\n"
            " app/services/x.py                                  |  12 +-\n"
        )
        found = sd._DIFFSTAT_RE.findall(content)
        assert "작업/결과/20260915_업무흐름도식화/SUMMARY.md" in found
        assert ".../20260915_업무흐름도식화/forms_inventory.html" in found
        assert not any(p.endswith(".py") for p in found)

    def test_shell_variables_are_rejected(self):
        assert sd._looks_like_path("/tmp/$f.md") is False
        assert sd._looks_like_path("/tmp/*.md") is False
        assert sd._looks_like_path("/tmp/ok.md") is True


class TestViewUrl:
    def test_static_reports_maps_to_container_base(self):
        url = sd._view_url("/root/aads/aads-server/app/static/reports/a.html")
        assert url is not None
        assert "base_path=%2Fapp%2Fapp%2Fstatic%2Freports" in url
        assert "file_path=a.html" in url

    def test_longest_prefix_wins(self):
        # /app 보다 /app/static/reports 가 먼저 잡혀야 한다.
        url = sd._view_url("/root/aads/aads-server/app/static/docs/b.md")
        assert url is not None and "static%2Fdocs" in url

    def test_unknown_root_has_no_link(self):
        # 244 서버 경로는 이 뷰어가 열 수 없다 — 링크를 만들면 안 된다.
        assert sd._view_url("/srv/biseo/회계비서/작업/결과/index.html") is None
        assert sd._view_url("작업/결과/index.html") is None


class TestCollector:
    def _at(self, iso: str):
        from datetime import datetime

        return datetime.fromisoformat(iso)

    def test_scratch_files_are_counted_not_listed(self):
        col = sd._Collector()
        col.add("/tmp/probe.txt", source="tool_result", at=self._at("2026-09-15T10:00:00"))
        col.add("/root/aads/aads-server/docs/real.md", source="ledger", at=self._at("2026-09-15T10:00:00"))
        docs = col.result(10)
        assert [d["name"] for d in docs] == ["real.md"]
        assert col.others == 1

    def test_non_document_extensions_are_excluded(self):
        col = sd._Collector()
        col.add("scripts/pipeline-runner.sh", source="tool_input")
        assert col.result(10) == []
        assert col.others == 1

    def test_same_file_from_two_sources_is_one_row(self):
        col = sd._Collector()
        col.add("docs/x.md", source="tool_input", at=self._at("2026-09-15T09:00:00"))
        col.add("/root/aads/aads-server/docs/x.md", source="ledger", repo="aads-server",
                at=self._at("2026-09-15T11:00:00"))
        docs = col.result(10)
        assert len(docs) == 1
        assert docs[0]["writes"] == 2
        assert docs[0]["at"].startswith("2026-09-15T11:00:00")

    def test_relative_path_outside_known_roots_stays_relative(self):
        col = sd._Collector()
        col.add("작업/결과/index.html", source="runner")
        docs = col.result(10)
        assert docs[0]["path"] == "작업/결과/index.html"
        assert docs[0]["view_url"] is None
        assert docs[0]["source_label"] == "러너 산출물"
