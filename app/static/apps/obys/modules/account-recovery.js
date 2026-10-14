/* 오비서 아이디 찾기·비밀번호 재설정 화면. 서버 응답은 textContent 로만 그린다(innerHTML 미사용). */
(function () {
  "use strict";

  var API = "/api/v1/auth";
  var STYLE_ID = "obysAccountRecoveryStyle";
  var CSS = [
    ".ar-overlay{position:fixed;inset:0;z-index:2147483000;background:rgba(15,23,42,.55);display:flex;align-items:center;justify-content:center;padding:16px}",
    ".ar-card{background:#fff;color:#0f172a;width:min(440px,100%);max-height:92dvh;overflow:auto;border-radius:14px;padding:20px;box-shadow:0 20px 50px rgba(0,0,0,.3);font:15px/1.5 system-ui,-apple-system,'Noto Sans KR',sans-serif}",
    ".ar-card h2{margin:0 0 6px;font-size:18px}",
    ".ar-card p{margin:6px 0}",
    ".ar-card label{display:block;margin:10px 0 0;font-weight:600;font-size:14px}",
    ".ar-card input[type=text],.ar-card input[type=email],.ar-card input[type=tel],.ar-card input[type=password]{display:block;width:100%;box-sizing:border-box;min-height:44px;margin-top:4px;padding:0 12px;border:1px solid #cbd5e1;border-radius:8px;font-size:16px}",
    ".ar-btn{min-height:44px;min-width:44px;padding:0 16px;border-radius:8px;border:1px solid #cbd5e1;background:#f8fafc;color:#0f172a;font-size:15px;font-weight:600;cursor:pointer}",
    ".ar-btn.primary{background:#2f6fed;border-color:#2f6fed;color:#fff}",
    ".ar-btn[disabled]{opacity:.55;cursor:not-allowed}",
    ".ar-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}",
    ".ar-row .ar-btn{flex:1 1 140px}",
    ".ar-note{background:#eef4ff;border-radius:8px;padding:10px 12px;font-size:13px;color:#1e3a8a}",
    ".ar-error{background:#fef2f2;border-radius:8px;padding:10px 12px;font-size:14px;color:#991b1b;margin-top:10px}",
    ".ar-ok{background:#f0fdf4;border-radius:8px;padding:10px 12px;font-size:14px;color:#166534;margin-top:10px}",
    ".ar-masked{font-size:20px;font-weight:700;letter-spacing:.02em;margin:8px 0;word-break:break-all}",
    ".ar-temp{font:700 22px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.06em;background:#fff7ed;border:1px dashed #f59e0b;border-radius:8px;padding:10px;text-align:center;user-select:all;word-break:break-all}",
    ".ar-list{display:flex;flex-direction:column;gap:8px}",
    ".ar-item{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px}",
    ".ar-item small{color:#64748b;display:block}",
    ".ar-links{display:flex;gap:8px;margin-top:8px}",
    ".ar-links .ar-btn{flex:1 1 0}"
  ].join("");

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = CSS;
    document.head.appendChild(style);
  }

  function el(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key];
      else if (key === "class") node.className = attrs[key];
      else node.setAttribute(key, attrs[key]);
    });
    for (var i = 2; i < arguments.length; i += 1) {
      if (arguments[i]) node.appendChild(arguments[i]);
    }
    return node;
  }

  function field(label, name, type, extra) {
    var input = el("input", Object.assign({ name: name, type: type || "text", required: "required" }, extra || {}));
    return { wrap: el("label", { text: label }, input), input: input };
  }

  function request(path, options) {
    var opts = options || {};
    var headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    return fetch(API + path, {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (payload) {
        if (!response.ok) {
          var detail = payload && payload.detail;
          var error = new Error(typeof detail === "string" ? detail : "요청을 처리하지 못했습니다.");
          error.status = response.status;
          throw error;
        }
        return payload;
      });
    }, function () {
      var error = new Error("서버에 연결하지 못했습니다. 네트워크를 확인한 뒤 다시 시도해 주십시오.");
      error.status = 0;
      throw error;
    });
  }

  function openDialog(title, build) {
    ensureStyle();
    var previousFocus = document.activeElement;
    var overlay = el("div", { class: "ar-overlay", role: "dialog", "aria-modal": "true", "aria-label": title });
    var card = el("div", { class: "ar-card" }, el("h2", { text: title }));
    overlay.appendChild(card);
    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      overlay.remove();
      document.removeEventListener("keydown", onKey, true);
      if (previousFocus && previousFocus.focus) previousFocus.focus();
    }
    function onKey(event) {
      if (event.key === "Escape") close();
    }
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(overlay);
    build(card, close);
    var first = card.querySelector("input,button");
    if (first) first.focus();
    return close;
  }

  function messageBox(card) {
    var box = el("div", { "aria-live": "polite" });
    card.appendChild(box);
    return {
      error: function (text) { box.replaceChildren(el("div", { class: "ar-error", text: text })); },
      ok: function (text) { box.replaceChildren(el("div", { class: "ar-ok", text: text })); },
      clear: function () { box.replaceChildren(); },
      node: box
    };
  }

  function guidance(error) {
    if (error.status === 429) return error.message + " 급하면 소속 관리자에게 직접 문의해 주십시오.";
    if (error.status === 503) return error.message;
    if (error.status === 0) return error.message;
    return error.message + " 입력한 내용을 확인하시거나 소속 관리자에게 문의해 주십시오.";
  }

  function openFindId() {
    openDialog("아이디 찾기", function (card, close) {
      card.appendChild(el("p", { text: "가입할 때 입력한 이름과 연락처를 입력해 주십시오. 아이디는 일부만 가려서 보여드립니다." }));
      var name = field("이름", "name", "text", { autocomplete: "name", maxlength: "100" });
      var phone = field("연락처", "phone", "tel", { autocomplete: "tel", inputmode: "tel", placeholder: "010-0000-0000", maxlength: "40" });
      var msg = messageBox(card);
      var submit = el("button", { class: "ar-btn primary", type: "submit", text: "아이디 찾기" });
      var cancel = el("button", { class: "ar-btn", type: "button", text: "닫기" });
      var form = el("form", {}, name.wrap, phone.wrap, el("div", { class: "ar-row" }, submit, cancel));
      card.insertBefore(form, msg.node);
      cancel.addEventListener("click", close);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        submit.disabled = true;
        msg.clear();
        request("/recovery/find-id", { method: "POST", body: { name: name.input.value, phone: phone.input.value } })
          .then(function (result) {
            var box = el("div", {});
            if (result.found) {
              box.appendChild(el("p", { text: result.message }));
              box.appendChild(el("div", { class: "ar-masked", text: result.masked_email }));
              box.appendChild(el("p", { class: "ar-note", text: "전체 아이디가 필요하면 소속 관리자에게 확인을 요청해 주십시오." }));
              var toLogin = el("button", { class: "ar-btn primary", type: "button", text: "로그인 화면으로" });
              toLogin.addEventListener("click", close);
              box.appendChild(el("div", { class: "ar-row" }, toLogin));
            } else {
              box.appendChild(el("div", { class: "ar-error", text: result.message }));
              var toReset = el("button", { class: "ar-btn", type: "button", text: "비밀번호 찾기로 이동" });
              toReset.addEventListener("click", function () { close(); openReset(); });
              box.appendChild(el("div", { class: "ar-row" }, toReset));
            }
            msg.node.replaceChildren(box);
          })
          .catch(function (error) { msg.error(guidance(error)); })
          .then(function () { submit.disabled = false; });
      });
    });
  }

  function openReset() {
    openDialog("비밀번호 찾기", function (card, close) {
      var options = { reset_sms: false };
      var msg = messageBox(card);
      var note = el("p", { class: "ar-note", text: "관리자 승인 방식: 요청하면 소속 관리자에게 알림이 가고, 관리자가 임시 비밀번호를 알려주면 그 비밀번호로 로그인한 뒤 새 비밀번호를 정합니다." });
      var email = field("이메일(아이디)", "email", "email", { autocomplete: "username", maxlength: "200" });
      var name = field("이름", "name", "text", { autocomplete: "name", maxlength: "100" });
      var phone = field("연락처", "phone", "tel", { autocomplete: "tel", inputmode: "tel", placeholder: "010-0000-0000", maxlength: "40" });
      var methodWrap = el("div", { class: "ar-note", hidden: "hidden", style: "margin-top:10px" });
      var useSms = el("input", { type: "checkbox", id: "arUseSms" });
      methodWrap.appendChild(el("label", { for: "arUseSms", style: "font-weight:600;margin:0" }, useSms, document.createTextNode(" 문자로 인증번호 받기(휴대폰으로 바로 재설정)")));
      var submit = el("button", { class: "ar-btn primary", type: "submit", text: "재설정 요청" });
      var cancel = el("button", { class: "ar-btn", type: "button", text: "닫기" });
      var form = el("form", {}, email.wrap, name.wrap, phone.wrap, methodWrap, el("div", { class: "ar-row" }, submit, cancel));
      card.insertBefore(note, msg.node);
      card.insertBefore(form, msg.node);
      cancel.addEventListener("click", close);
      request("/recovery/options").then(function (opts) {
        options = opts || options;
        if (options.reset_sms) methodWrap.removeAttribute("hidden");
      }).catch(function () { /* 옵션을 못 받으면 기본(관리자 승인)만 노출 */ });

      function showSmsConfirm() {
        var code = field("인증번호 6자리", "code", "text", { inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6" });
        var pw = field("새 비밀번호(8자 이상, 영문+숫자)", "new_password", "password", { autocomplete: "new-password", minlength: "8", maxlength: "72" });
        var pw2 = field("새 비밀번호 확인", "new_password2", "password", { autocomplete: "new-password", minlength: "8", maxlength: "72" });
        var go = el("button", { class: "ar-btn primary", type: "submit", text: "비밀번호 변경" });
        var confirmForm = el("form", {}, code.wrap, pw.wrap, pw2.wrap, el("div", { class: "ar-row" }, go));
        form.replaceWith(confirmForm);
        confirmForm.addEventListener("submit", function (event) {
          event.preventDefault();
          if (pw.input.value !== pw2.input.value) { msg.error("새 비밀번호가 서로 다릅니다. 다시 입력해 주십시오."); return; }
          go.disabled = true;
          request("/recovery/sms/confirm", { method: "POST", body: { email: email.input.value, code: code.input.value, new_password: pw.input.value } })
            .then(function (result) {
              confirmForm.remove();
              note.remove();
              msg.ok(result.message);
              var toLogin = el("button", { class: "ar-btn primary", type: "button", text: "로그인 화면으로" });
              toLogin.addEventListener("click", close);
              card.appendChild(el("div", { class: "ar-row" }, toLogin));
            })
            .catch(function (error) { msg.error(guidance(error)); })
            .then(function () { go.disabled = false; });
        });
        code.input.focus();
      }

      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var sms = options.reset_sms && useSms.checked;
        submit.disabled = true;
        msg.clear();
        request(sms ? "/recovery/sms/request" : "/recovery/password-reset-request", {
          method: "POST",
          body: { email: email.input.value, name: name.input.value, phone: phone.input.value }
        })
          .then(function (result) {
            msg.ok(result.message);
            if (sms) showSmsConfirm();
            else { form.remove(); card.appendChild(el("div", { class: "ar-row" }, (function () { var b = el("button", { class: "ar-btn primary", type: "button", text: "확인" }); b.addEventListener("click", close); return b; })())); }
          })
          .catch(function (error) { msg.error(guidance(error)); })
          .then(function () { submit.disabled = false; });
      });
    });
  }

  /* 임시 비밀번호로 로그인한 직후: 새 비밀번호를 정해야만 새 토큰을 돌려준다. 취소하면 reject — 토큰은 저장되지 않는다. */
  function forcePasswordChange(token) {
    return new Promise(function (resolve, reject) {
      var done = false;
      openDialog("새 비밀번호 설정", function (card, close) {
        card.appendChild(el("p", { text: "임시 비밀번호로 로그인했습니다. 계속하려면 새 비밀번호를 정해 주십시오." }));
        var pw = field("새 비밀번호(8자 이상, 영문+숫자)", "new_password", "password", { autocomplete: "new-password", minlength: "8", maxlength: "72" });
        var pw2 = field("새 비밀번호 확인", "new_password2", "password", { autocomplete: "new-password", minlength: "8", maxlength: "72" });
        var msg = messageBox(card);
        var go = el("button", { class: "ar-btn primary", type: "submit", text: "변경하고 계속" });
        var cancel = el("button", { class: "ar-btn", type: "button", text: "나중에(로그인 취소)" });
        var form = el("form", {}, pw.wrap, pw2.wrap, el("div", { class: "ar-row" }, go, cancel));
        card.insertBefore(form, msg.node);
        cancel.addEventListener("click", function () {
          close();
          if (!done) { done = true; reject(new Error("비밀번호를 변경해야 로그인할 수 있습니다.")); }
        });
        form.addEventListener("submit", function (event) {
          event.preventDefault();
          if (pw.input.value !== pw2.input.value) { msg.error("새 비밀번호가 서로 다릅니다. 다시 입력해 주십시오."); return; }
          go.disabled = true;
          request("/password/change", {
            method: "POST",
            headers: { Authorization: "Bearer " + token },
            body: { new_password: pw.input.value }
          })
            .then(function (result) {
              done = true;
              close();
              resolve(result.token);
            })
            .catch(function (error) { msg.error(guidance(error)); go.disabled = false; });
        });
      });
    });
  }

  function formatTime(value) {
    if (!value) return "";
    var date = new Date(value);
    return isNaN(date.getTime()) ? "" : date.toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" });
  }

  function showIssued(result, onClose) {
    openDialog("임시 비밀번호", function (card, close) {
      card.appendChild(el("div", { class: "ar-note", text: result.message }));
      card.appendChild(el("div", { class: "ar-temp", text: result.temp_password }));
      card.appendChild(el("p", { text: "유효 기간: " + formatTime(result.expires_at) + " 까지 · 1회만 사용 가능" }));
      var copy = el("button", { class: "ar-btn", type: "button", text: "복사" });
      var done = el("button", { class: "ar-btn primary", type: "button", text: "전달했습니다(닫기)" });
      copy.addEventListener("click", function () {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(result.temp_password).then(function () { copy.textContent = "복사됨"; }, function () { copy.textContent = "직접 선택해 복사하십시오"; });
        } else copy.textContent = "직접 선택해 복사하십시오";
      });
      done.addEventListener("click", function () { close(); if (onClose) onClose(); });
      card.appendChild(el("div", { class: "ar-row" }, copy, done));
    });
  }

  /* 관리자 목록. ctx: { headers: () => ({Authorization}), toast: (msg) => void } */
  function mountAdminList(container, ctx) {
    if (!container) return Promise.resolve();
    ensureStyle();
    var context = ctx || {};
    function headers() { return typeof context.headers === "function" ? context.headers() : {}; }
    function toast(text) { if (typeof context.toast === "function") context.toast(text); }

    function render(requests) {
      var list = el("div", { class: "ar-list" });
      if (!requests.length) list.appendChild(el("div", { class: "ar-note", text: "대기 중인 비밀번호 재설정 요청이 없습니다." }));
      requests.forEach(function (item) {
        var issued = item.status === "issued";
        var info = el("div", {}, el("strong", { text: (item.user_name || "이름 미등록") + " · " + item.masked_email }),
          el("small", { text: "요청 " + formatTime(item.requested_at) + (issued ? " · 임시 비밀번호 발급됨(" + formatTime(item.temp_expires_at) + " 만료)" : "") }));
        var issue = el("button", { class: "ar-btn primary", type: "button", text: issued ? "재발급" : "임시 비밀번호 발급" });
        var cancel = el("button", { class: "ar-btn", type: "button", text: "취소" });
        issue.addEventListener("click", function () {
          var who = item.user_name || item.masked_email;
          if (!window.confirm(who + "님의 임시 비밀번호를 발급할까요? 기존 로그인 세션이 모두 끊기고, 임시 비밀번호는 지금 한 번만 표시됩니다.")) return;
          issue.disabled = true;
          request("/password-reset-requests/" + encodeURIComponent(item.id) + "/issue", { method: "POST", headers: headers() })
            .then(function (result) { showIssued(result, load); })
            .catch(function (error) { toast(error.message); issue.disabled = false; });
        });
        cancel.addEventListener("click", function () {
          if (!window.confirm("이 재설정 요청을 취소할까요?")) return;
          request("/password-reset-requests/" + encodeURIComponent(item.id) + "/cancel", { method: "POST", headers: headers() })
            .then(load, function (error) { toast(error.message); });
        });
        list.appendChild(el("div", { class: "ar-item" }, info, el("div", { class: "ar-links" }, issue, cancel)));
      });
      container.replaceChildren(list);
    }

    function load() {
      container.replaceChildren(el("div", { class: "ar-note", text: "재설정 요청을 불러오는 중입니다." }));
      return request("/password-reset-requests", { headers: headers() })
        .then(function (payload) { render(payload.requests || []); })
        .catch(function (error) {
          var text = error.status === 403 ? "관리자 권한이 있어야 재설정 요청을 볼 수 있습니다." : error.message;
          var retry = el("button", { class: "ar-btn", type: "button", text: "다시 불러오기" });
          retry.addEventListener("click", load);
          container.replaceChildren(el("div", { class: "ar-error", text: text }), el("div", { class: "ar-row" }, retry));
        });
    }
    return load();
  }

  function wireLoginLinks() {
    document.addEventListener("click", function (event) {
      var target = event.target && event.target.closest ? event.target.closest("[data-ar-open]") : null;
      if (!target) return;
      event.preventDefault();
      if (target.getAttribute("data-ar-open") === "find-id") openFindId();
      else openReset();
    });
  }

  window.ObysAccountRecovery = {
    openFindId: openFindId,
    openReset: openReset,
    forcePasswordChange: forcePasswordChange,
    mountAdminList: mountAdminList
  };
  wireLoginLinks();
})();
