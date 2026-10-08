/* 오비서 출퇴근 PWA 서비스워커 — scope: /static/apps/obys/
 *
 * 캐시하는 것은 앱 셸(HTML·아이콘·manifest)뿐이다. 근태 데이터·API 응답·인증 토큰은
 * 캐시에 넣지 않는다 — 아래 fetch 핸들러는 APP_SHELL 경로가 아니면 손대지 않고
 * 브라우저 기본 네트워크 요청으로 흘려보낸다(respondWith 도 호출하지 않는다).
 *
 * 오프라인 출퇴근을 기기에 쌓아 나중에 올리는 기능은 없다(시각 위조 위험).
 * 오프라인이면 화면이 "지금 기록할 수 없음 + 재시도" 를 보여준다.
 *
 * 배포 시 셸을 바꾸면 CACHE_VERSION 을 올린다. 셸은 네트워크 우선이라 버전을
 * 깜빡해도 온라인 상태에서는 새 파일을 받는다. 캐시는 오프라인 안내용 사본이다.
 */
const CACHE_VERSION = "obys-clock-shell-20261008-r1";
const CACHE_PREFIX = "obys-clock-shell-";
const APP_SHELL = [
  "/static/apps/obys/clock.html",
  "/static/apps/obys/manifest.webmanifest",
  "/static/apps/obys/icons/obys-clock-192.png",
  "/static/apps/obys/icons/obys-clock-512.png"
];
const APP_SHELL_PATHS = new Set(APP_SHELL);

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then(cache => cache.addAll(APP_SHELL.map(path => new Request(path, { cache: "reload", credentials: "omit" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_VERSION)
        .map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (!APP_SHELL_PATHS.has(url.pathname)) return;  // API·근태·기타 화면은 캐시하지 않는다
  event.respondWith(
    fetch(request, { cache: "no-store" })
      .then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_VERSION).then(cache => cache.put(url.pathname, copy));
        }
        return response;
      })
      .catch(() => caches.match(url.pathname).then(cached => cached || Response.error()))
  );
});
