/* 앱 설치·접속 현황 수집 — 직원이 쓰는 화면(index.html·clock.html)에서 로드한다.
 * 실패해도 화면 동작에 영향 없이 조용히 무시한다(요청 타임아웃 5초).
 * 로그인·사업자 확정이 늦어도 폴링으로 재시도하고, 사업자가 바뀌면 새 사업자로 다시 보낸다.
 * 보내는 값은 display_mode·platform·event 뿐이며 UA 원문·IP·위치는 보내지 않는다. */
(() => {
  if (window.ObysAppInstallPing) return;
  const script = document.currentScript;
  const keys = ((script && script.dataset.businessKeys) || "obys_v41_business_id,obys_clock_business_id,yeoljeong-finance-settings-business")
    .split(",").map(x => x.trim()).filter(Boolean);
  const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const platform = () => {
    const ua = navigator.userAgent;
    return /iphone|ipad|ipod/i.test(ua) ? "ios" : /android/i.test(ua) ? "android" : /Windows|Macintosh|Linux|CrOS/i.test(ua) ? "desktop" : "other";
  };
  const cookie = name => {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    try { return m ? decodeURIComponent(m[1]) : ""; } catch (_e) { return ""; }
  };
  const token = () => localStorage.getItem("aads_token") || localStorage.getItem("fb_access_token")
    || cookie("aads_token") || cookie("fb_access_token") || "";
  const storedBusinesses = () => {
    const out = [];
    for (const key of keys) {
      const value = localStorage.getItem(key);
      if (value && !out.includes(value)) out.push(value);
    }
    return out;
  };
  const timedFetch = (url, options) => {
    const ctrl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = setTimeout(() => { if (ctrl) ctrl.abort(); }, 5000);
    return fetch(url, { ...options, signal: ctrl ? ctrl.signal : undefined }).finally(() => clearTimeout(timer));
  };
  const SENT_TTL = 6 * 3600 * 1000;      // 같은 문서를 며칠 유지해도 서버의 하루 간격 갱신에 닿도록 만료시킨다.
  const LIST_RETRY = 30 * 1000;          // 사업자 목록 조회 실패 후 재시도 간격
  const LIST_TTL = 10 * 60 * 1000;       // 성공한 목록을 다시 쓰는 기간
  const sent = new Map();                // "사업자|토큰끝|이벤트" -> 마지막 성공 시각
  const rejected = new Set();            // 이 토큰으로 403/404 를 받은 "사업자|토큰끝" (저장값 검증 실패)
  let busy = false;
  let list = [];                         // 서버가 알려준 사업자 id 목록
  let listToken = "";
  let listAt = 0;                        // 마지막 시도 시각
  let listOk = false;

  const fresh = id => sent.has(id) && Date.now() - sent.get(id) < SENT_TTL;
  const serverBusinesses = async tk => {
    const now = Date.now();
    if (listToken === tk && now - listAt < (listOk ? LIST_TTL : LIST_RETRY)) return list;
    listToken = tk; listAt = now; listOk = false; list = [];
    try {
      const res = await timedFetch("/api/v1/workspaces/businesses", { credentials: "same-origin", headers: { Authorization: "Bearer " + tk } });
      if (res.ok) {
        const body = await res.json();
        list = (body.businesses || []).map(x => x && x.id).filter(Boolean);
        listOk = true;
      }
    } catch (_e) { /* 실패 시 LIST_RETRY 뒤 다시 조회 */ }
    return list;
  };

  const send = async (event, force) => {
    const tk = token();
    if (!tk || busy) return;
    busy = true;
    try {
      const suffix = "|" + tk.slice(-12);
      const tried = new Set();
      const ping = async bid => {
        tried.add(bid);
        const id = bid + suffix + "|" + event;
        if (!force && fresh(id)) return "done";
        const res = await timedFetch("/api/v1/obys/" + encodeURIComponent(bid) + "/app-install/ping", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + tk },
          body: JSON.stringify({ display_mode: standalone() ? "standalone" : "browser", platform: platform(), event }),
        });
        if (res.ok) { sent.set(id, Date.now()); return "done"; }
        if (res.status === 403 || res.status === 404) { rejected.add(bid + suffix); return "next"; }
        return "stop";   // 401·408·429·5xx 등 일시 오류는 다음 폴링에서 재시도
      };
      // 1) 저장된 사업자 — 거절된 값은 건너뛰고 다음 후보로 넘어간다.
      for (const bid of storedBusinesses()) {
        if (rejected.has(bid + suffix)) continue;
        const r = await ping(bid);
        if (r !== "next") return;
      }
      // 2) 서버가 알려주는 사업자 목록(저장값이 없거나 모두 거절된 경우)
      for (const bid of await serverBusinesses(tk)) {
        if (tried.has(bid) || rejected.has(bid + suffix)) continue;
        const r = await ping(bid);
        if (r !== "next") return;
      }
    } catch (_error) { /* 조용히 무시, 다음 폴링에서 재시도 */ }
    finally { busy = false; }
  };
  const tick = () => { if (!document.hidden) send("launch", false); };

  window.ObysAppInstallPing = { tick, send };
  window.addEventListener("appinstalled", () => send("appinstalled", true));
  window.addEventListener("storage", tick);
  window.addEventListener("focus", tick);
  document.addEventListener("visibilitychange", tick);
  setTimeout(tick, 1500);
  setInterval(tick, 10000);   // 로그인·사업자 확정이 늦거나 사업자가 바뀐 경우를 따라간다. 이미 보낸 값은 건너뛴다.
})();
