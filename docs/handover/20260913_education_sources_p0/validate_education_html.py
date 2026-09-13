#!/usr/bin/env python3
"""교육자료 HTML 구조·앵커·외부링크 검증기 (AADS 교육자료 출처 보강 P0, 2026-09-13).

사용법:
    python3 validate_education_html.py [--links] <html 파일...>

--links 를 주면 외부 URL에 실제 HTTP 요청을 보내 응답 코드를 출력한다.
HTTP 403/302/301 은 기관 사이트의 봇 차단·리다이렉트인 경우가 많으므로
'죽은 링크'로 단정하지 말고 브라우저로 재확인할 것.
"""
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'source', 'track', 'wbr'}
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0 Safari/537.36')


class Doc(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.ids, self.hrefs = [], [], set(), []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if attr.get('id'):
            self.ids.add(attr['id'])
        if tag == 'a' and attr.get('href'):
            self.hrefs.append(attr['href'])
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"여는 태그 없는 </{tag}> ({self.getpos()[0]}행)")
            return
        name, line = self.stack.pop()
        if name != tag:
            self.errors.append(f"<{name}>({line}행)이 </{tag}>({self.getpos()[0]}행)로 닫힘")


def check_link(url):
    # law.go.kr 처럼 경로에 한글이 들어간 URL은 퍼센트 인코딩해야 urllib이 보낼 수 있다.
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(parts._replace(
        path=urllib.parse.quote(parts.path, safe="/%"),
        query=urllib.parse.quote(parts.query, safe="=&%+:")))
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:                      # noqa: BLE001 - 네트워크 오류 전부 보고
        return type(exc).__name__


def main(argv):
    check_links = '--links' in argv
    paths = [a for a in argv if not a.startswith('--')]
    failed = False
    for path in paths:
        source = open(path, encoding='utf-8').read()
        doc = Doc()
        doc.feed(source)
        anchors = [h[1:] for h in doc.hrefs if h.startswith('#')]
        broken = [a for a in anchors if a not in doc.ids]
        external = sorted({h for h in doc.hrefs if h.startswith('http')})
        unclosed = [f"<{t}>({line}행)" for t, line in doc.stack]
        ok = not doc.errors and not broken and not unclosed and source.rstrip().endswith('</html>')
        failed |= not ok
        print(f"{'PASS' if ok else 'FAIL'} {path}")
        print(f"   태그 오류 {len(doc.errors)} · 미종료 태그 {len(unclosed)} · "
              f"내부 앵커 {len(anchors)}(깨짐 {len(broken)}) · 외부 링크 {len(external)}")
        for item in doc.errors + broken + unclosed:
            print(f"   - {item}")
        if check_links:
            for url in external:
                print(f"   [{check_link(url)}] {url}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
