(() => {
  const installBtn = document.getElementById("pwaInstallBtn");
  const hint = document.getElementById("pwaInstallHint");
  const bar = document.getElementById("pwaInstallBar");
  if (!installBtn || !hint) return;
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let deferredPrompt = null;
  const hideAll = () => {
    installBtn.hidden = true;
    hint.hidden = true;
    if (bar) bar.hidden = true;
  };
  if (!standalone) {
    const ua = navigator.userAgent;
    const inApp = /KAKAOTALK|NAVER\(inapp|Instagram|FBAN|FBAV|\bLine\/|DaumApps/i.test(ua);
    const showHint = (text, link) => {
      hint.textContent = "";
      const msg = document.createElement("span");
      msg.textContent = text;
      hint.appendChild(msg);
      if (link) {
        const a = document.createElement("a");
        a.href = link.href;
        a.textContent = link.label;
        a.style.marginLeft = "6px";
        hint.appendChild(a);
      }
      const close = document.createElement("button");
      close.type = "button";
      close.className = "pwa-hint-close";
      close.setAttribute("aria-label", "안내 닫기");
      close.textContent = "닫기";
      close.addEventListener("click", () => { hint.hidden = true; });
      hint.appendChild(close);
      hint.hidden = false;
    };
    const fallbackHint = () => {
      if (inApp) {
        const chromeLink = /KAKAOTALK/i.test(ua) && /android/i.test(ua)
          ? {
              href: "intent://" + location.host + location.pathname + location.search
                + "#Intent;scheme=https;package=com.android.chrome;end",
              label: "크롬으로 열기",
            }
          : null;
        showHint("지금 앱 안의 브라우저입니다. 오른쪽 위 ⋮ 또는 공유 → '다른 브라우저로 열기'(크롬/사파리)로 연 뒤 다시 '앱 설치'를 누르세요.", chromeLink);
      } else if (/iphone|ipad|ipod/i.test(ua)) {
        showHint("Safari 아래쪽 공유 버튼 → '홈 화면에 추가'를 누르면 앱처럼 설치됩니다.");
      } else if (/SamsungBrowser/i.test(ua)) {
        showHint("아래 메뉴(≡) → '현재 페이지 추가' → '홈 화면'을 누르세요.");
      } else if (/android/i.test(ua)) {
        showHint("주소창 오른쪽 ⋮ → '앱 설치' 또는 '홈 화면에 추가'를 누르세요.");
      } else if (/Chrome\/|Edg\//.test(ua)) {
        showHint("주소창 오른쪽 설치 아이콘(⊕) 또는 ⋮ → '오비서 설치'를 누르세요.");
      } else {
        showHint("이 브라우저는 앱 설치를 지원하지 않을 수 있습니다. 크롬 또는 엣지로 열어 '앱 설치'를 눌러 주세요.");
      }
    };
    if (bar) bar.hidden = false;
    installBtn.hidden = false;
    window.addEventListener("beforeinstallprompt", event => {
      event.preventDefault();
      deferredPrompt = event;
    });
    installBtn.addEventListener("click", async () => {
      if (!deferredPrompt) {
        fallbackHint();
        return;
      }
      deferredPrompt.prompt();
      await deferredPrompt.userChoice.catch(() => null);
      deferredPrompt = null;
      hideAll();
    });
    window.addEventListener("appinstalled", hideAll);
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/apps/obys/sw.js", { scope: "/static/apps/obys/" }).catch(() => {});
  }
})();
