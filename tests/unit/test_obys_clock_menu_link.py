"""오비서 index.html 메뉴에 출퇴근(clock.html) 진입 링크가 있고 직원 모드에서 숨겨지지 않는지 확인."""
import re
from pathlib import Path

STATIC = Path(__file__).parents[2] / "app/static/apps/obys"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
LINK = re.compile(r'<a\b[^>]*\bdata-clock-link\b[^>]*>', re.S)


def test_clock_link_in_side_nav_and_quick_tabs_same_page():
    links = LINK.findall(HTML)
    assert len(links) == 2
    for tag in links:
        assert 'class="tab"' in tag
        assert 'href="/static/apps/obys/clock.html"' in tag
        assert "target=" not in tag
    assert (STATIC / "clock.html").is_file()
    side = HTML.split('<nav class="side-nav"', 1)[1].split("</nav>", 1)[0]
    assert side.index("data-clock-link") < side.index('data-view="attendance"')


def test_clock_link_not_hidden_by_access_policy_for_employees():
    policy = HTML.split("function applyAccessPolicy()", 1)[1].split("[data-external-link]", 1)[0]
    exempt = policy.split("const allowed = permitted.includes", 1)[0]
    assert 'data-clock-link' in exempt
    assert 'classList.toggle("hidden", !isAuthed)' in exempt


def test_clock_link_first_in_employee_view_and_not_intercepted():
    assert re.search(r'body\.employee-view \.tab\[data-clock-link\]\s*\{\s*order:\s*-1;', HTML)
    assert 'querySelectorAll(".tab[data-view]").forEach(btn => btn.addEventListener("click"' in HTML
    assert 'data-clock-link' not in "".join(re.findall(r'\.hidden\s*\{[^}]*\}', HTML))
