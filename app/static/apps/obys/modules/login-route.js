/* 오비서 새 화면 로그인 — 로그인 뒤 이동 경로 판정(순수 함수, DOM·네트워크 미사용).
 * 브라우저에서는 window.ObysLoginRoute, Node 테스트에서는 module.exports 로 노출한다. */
(function (root) {
  "use strict";

  var ADMIN_PATH = "/static/apps/obys/admin.html";
  var EMPLOYEE_PATH = "/static/apps/obys/employee.html";
  var LOGIN_PATH = "/static/apps/obys/login.html";
  var EMPLOYEE_ROLES = ["employee", "employee_pending", "employee_rejected"];

  // 로그인 뒤 돌아갈 주소는 언니냉면 조리법 경로만 허용한다(index.html safeRecipeRedirectPath 와 같은 규칙).
  function safeRedirectPath(value) {
    var path = String(value == null ? "" : value).trim();
    return /^\/unni-naengmyeon\/recipes(?:[?#]|$)/.test(path) && !/[\\\r\n]/.test(path) ? path : "";
  }

  function redirectFromSearch(search) {
    var params = new URLSearchParams(String(search || ""));
    return safeRedirectPath(params.get("redirect") || params.get("next") || "");
  }

  // /session 응답(permissions) 기준. legacy_access 는 레거시 원장 접근 여부일 뿐 직원/관리자 판정에 쓰지 않는다. 관리 권한이 있으면 직원이 아니다(index.html isEmployeeAccount 와 같은 판정).
  function isEmployeeSession(session) {
    var permissions = (session && session.permissions) || {};
    var role = String(permissions.role || "").trim().toLowerCase();
    var employee = EMPLOYEE_ROLES.indexOf(role) >= 0 || Boolean(permissions.employee_request_status);
    return Boolean(session) && employee && permissions.can_manage_onboarding !== true;
  }

  // 로그인 직후 이동할 곳. 서버가 관리자로 확인한 로그인(isAdmin=true)만 /session 없이 관리자 화면으로 간다.
  // 그 외에 /session 확인이 실패해 session 이 없으면 "" 를 돌려준다 — 호출자는 이동하지 말고 오류를 보여야 한다
  // (권한을 모르는 직원을 관리자 화면에 넣지 않는다).
  function destinationAfterLogin(session, redirect, isAdmin) {
    if (!session && isAdmin !== true) return "";
    var safe = safeRedirectPath(redirect);
    if (safe) return safe;
    return session && isEmployeeSession(session) ? EMPLOYEE_PATH : ADMIN_PATH + "#home";
  }

  function loginUrl(redirect) {
    var safe = safeRedirectPath(redirect);
    return safe ? LOGIN_PATH + "?redirect=" + encodeURIComponent(safe) : LOGIN_PATH;
  }

  var api = {
    ADMIN_PATH: ADMIN_PATH,
    EMPLOYEE_PATH: EMPLOYEE_PATH,
    LOGIN_PATH: LOGIN_PATH,
    safeRedirectPath: safeRedirectPath,
    redirectFromSearch: redirectFromSearch,
    isEmployeeSession: isEmployeeSession,
    destinationAfterLogin: destinationAfterLogin,
    loginUrl: loginUrl
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ObysLoginRoute = api;
})(typeof window !== "undefined" ? window : this);
