/* 오비서 직원 화면(employee.html) — 내 계약서·입사서류·급여명세·근태·출퇴근.
 *
 * 2026-11-16 대표 지시: 직원 화면을 레거시(index.html)에서 새 화면으로 옮긴다.
 * - 계약서 목록·읽기·서명은 modules/employee-portal.js 를 그대로 쓴다(이 파일은 bridge 만 만든다).
 * - 급여명세·근태 달력·입사서류는 이 파일이 그린다. 근태 정정 요청은 modules/attendance-corrections.js 를 붙인다.
 * - 출퇴근은 clock.html 로 보낸다(위치 확인·동의 흐름이 그 화면에 있다).
 * - 데이터는 기존 서버 API 만 쓴다. 직원 범위(본인·본인 조직)는 서버가 정한다 — 이 화면은 범위를 넓히지 않는다.
 * - 관리자의 '직원 화면 보기'(?preview=1)는 읽기 전용이다: GET 외 요청은 보내기 전에 막는다.
 *
 * index.html 의 전역 변수·함수는 하나도 쓰지 않는다. 로그인 화면으로 보낼 때만 그 주소를 쓴다.
 * 계산(진입 판정·미리보기 범위·달력·급여·서류 상태·복구 안내)은 DOM 없이 도는 core 로 나눠 node 에서 검증한다.
 */
(function (root) {
  "use strict";

  const PATHS = Object.freeze({
    login: "/static/apps/obys/index.html",
    admin: "/static/apps/obys/admin.html",
    clock: "/static/apps/obys/clock.html",
    self: "/static/apps/obys/employee.html"
  });
  const FINANCE_BASE = "/api/v1/yeoljeong-finance";
  const WORKSPACE_BASE = "/api/v1/workspaces";
  const AUTH_BASE = "/api/v1/auth";
  const TOKEN_KEYS = ["aads_token", "fb_access_token"];
  const PREVIEW_REQUEST_KEY = "obys.employeePreviewRequest";
  const PREVIEW_RETURN_KEY = "obys.employeePreviewReturn";
  const PREVIEW_REQUEST_TTL_MS = 5 * 60 * 1000;
  const TENANT_SWITCH_KEY = "obys.employeeApp.tenantSwitch";
  const EMPLOYEE_ROLES = ["employee", "employee_pending", "employee_rejected"];
  const PAYROLL_EMPLOYEE_VISIBLE = ["confirmed", "paid"];
  const ONBOARDING_EXPIRY_REQUIRED_TYPES = ["health_certificate", "foreign_registration", "visa_status_certificate", "work_permission_confirmation"];
  const RESUBMIT_STATUSES = ["rejected", "needs_fix", "resubmit_required", "revision_requested"];
  const REQUEST_TIMEOUT_MS = 15000;
  const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
  const READ_ONLY_MESSAGE = "읽기 전용 미리보기에서는 저장·서명·제출·업로드를 할 수 없습니다.";
  const SECTIONS = ["payroll", "attendance", "onboarding"];
  const V41_HASH_RE = /^#(?:home|tasks|reports|documents|sales|orders|cards|settlements|accounts|receivables|unmatched|purchases|suppliers|inventory|purchase-orders|employees|attendance|payroll|hr-docs|tax-evidence|tax-gap|journals|tax-returns|approvals|audit-history|alerts|errors|businesses|branches|margins|menu-costs|integrations|sales-connect|bank-connect|tax-connect|access-log)$/;

  // ── core: DOM 없이 계산만 한다 ─────────────────────────────────────────────

  const clean = value => String(value ?? "").trim();
  const lower = value => clean(value).toLowerCase();

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // admin.html leaveForEmployeeEntryV41 과 같은 기준이다: 직원 역할이거나 직원 가입요청 상태가 있고, 관리 권한이 없다.
  function isEmployeeSession(session) {
    const permissions = session?.permissions || {};
    const role = lower(permissions.role);
    const employee = EMPLOYEE_ROLES.includes(role) || Boolean(permissions.employee_request_status);
    return employee && permissions.can_manage_onboarding !== true;
  }

  // 진입 판정. preview 는 관리자만, 직원이 아닌 계정은 관리자 화면으로 보낸다.
  function decideEntry(session, options = {}) {
    const permissions = session?.permissions || {};
    if (options.preview) return permissions.can_manage_onboarding === true ? "preview" : "forbidden_preview";
    if (clean(session?.entry_tenant_redirect?.tenant_id)) return "switch_tenant";
    if (isEmployeeSession(session)) return "employee";
    return "admin";
  }

  function employeeStatus(session) {
    const permissions = session?.permissions || {};
    const status = lower(permissions.employee_request_status);
    const role = lower(permissions.role);
    if (status === "rejected" || role === "employee_rejected") return "rejected";
    if (status === "pending" || role === "employee_pending") return "pending";
    return "approved";
  }

  function safeReturnHash(hash, fallback = "#employees") {
    return V41_HASH_RE.test(String(hash || "")) ? String(hash) : fallback;
  }

  // admin.html 이 남긴 1회성 요청을 읽는 즉시 지운다. 5분이 지났거나 모양이 틀리면 무시한다.
  function consumePreviewRequest(storage, now = Date.now()) {
    if (!storage) return null;
    let raw = null;
    try { raw = storage.getItem(PREVIEW_REQUEST_KEY); } catch (_) { return null; }
    if (raw === null || raw === undefined) return null;
    try { storage.removeItem(PREVIEW_REQUEST_KEY); } catch (_) { /* 저장소 차단 */ }
    let request = null;
    try { request = JSON.parse(raw); } catch (_) { return null; }
    const age = now - Number(request?.createdAt);
    if (!Number.isFinite(age) || age < 0 || age > PREVIEW_REQUEST_TTL_MS) return null;
    const returnHash = request?.returnTo === "v41" ? safeReturnHash(request.returnHash) : "";
    const email = lower(request?.email);
    const target = {
      email: email.includes("*") ? "" : email,
      name: clean(request?.name),
      requestId: clean(request?.requestId)
    };
    if (request?.open === "picker" || (!target.email && !target.requestId)) return { open: "picker", returnHash };
    return { open: "employee", target, returnHash };
  }

  function previewCandidates(joinRequests) {
    const found = new Map();
    (Array.isArray(joinRequests) ? joinRequests : [])
      .filter(item => lower(item?.status) === "approved")
      .forEach(item => {
        const email = lower(item.email || item.employee_email);
        const safeEmail = email.includes("*") ? "" : email;
        const name = clean(item.name || item.employee_name);
        const id = clean(item.id);
        if (!id && !safeEmail) return;
        const key = id ? `id:${id}` : `email:${safeEmail}`;
        if (found.has(key)) return;
        found.set(key, { requestId: id, email: safeEmail, name, branch: clean(item.branch), businessId: clean(item.business_id || item.businessId) });
      });
    return Array.from(found.values()).sort((a, b) => String(a.name || a.email).localeCompare(String(b.name || b.email), "ko"));
  }

  function filterCandidates(candidates, query) {
    const q = lower(query);
    if (!q) return candidates;
    return candidates.filter(item => `${item.name} ${item.email} ${item.branch}`.toLowerCase().includes(q));
  }

  function maskEmail(email) {
    const [local, domain] = String(email || "").split("@");
    return domain ? `${local.slice(0, 2)}***@${domain}` : "";
  }

  // 관리자 토큰으로 받은 목록에서 미리볼 직원 한 명 몫만 남긴다(관리자는 원래 볼 수 있는 자료다).
  // 가입요청 번호가 양쪽에 있으면 번호로, 아니면 이메일로 맞춘다. 둘 다 없으면 보이지 않는다.
  function matchesTarget(record, target) {
    if (!record || !target) return false;
    const recordId = clean(record.employee_request_id || record.employeeRequestId);
    const recordEmail = lower(record.employee_email || record.employeeEmail || record.email);
    if (target.requestId && recordId) return recordId === target.requestId;
    return Boolean(target.email && recordEmail && recordEmail === target.email);
  }

  function scopeToTarget(rows, target) {
    return (Array.isArray(rows) ? rows : []).filter(row => matchesTarget(row, target));
  }

  // 직원에게는 서버가 확정·지급만 준다. 미리보기(관리자 응답)도 같은 기준으로 맞춘다.
  function visiblePayroll(rows, options = {}) {
    let list = Array.isArray(rows) ? rows.slice() : [];
    if (options.preview) {
      list = scopeToTarget(list, options.target)
        .filter(row => PAYROLL_EMPLOYEE_VISIBLE.includes(lower(row.status)) && !row.superseded_by);
    }
    return list.sort((a, b) => clean(b.payroll_month || b.period).localeCompare(clean(a.payroll_month || a.period))
      || clean(b.updated_at).localeCompare(clean(a.updated_at)));
  }

  function won(value) {
    return `${Math.round(Number(value) || 0).toLocaleString("ko-KR")}원`;
  }

  function payrollCard(row) {
    const totals = row?.totals || {};
    const pick = key => (totals[key] ?? row?.[key] ?? 0);
    const status = lower(row?.status);
    const month = clean(row?.payroll_month || row?.period).slice(0, 7);
    return {
      id: clean(row?.id),
      month,
      title: /^\d{4}-\d{2}$/.test(month) ? `${month.slice(0, 4)}년 ${Number(month.slice(5))}월 급여` : "급여명세",
      payDate: clean(row?.pay_date),
      gross: won(pick("gross_pay")),
      deductions: won(pick("total_deductions")),
      net: won(pick("net_pay")),
      held: Number(totals.held_amount || 0) > 0 ? won(totals.held_amount) : "",
      status: status === "paid" ? ["ok", "지급 완료"] : status === "confirmed" ? ["info", "확정"] : ["muted", clean(row?.status) || "상태 확인"]
    };
  }

  function requiredDocTypes(types) {
    return (Array.isArray(types) ? types : []).filter(item => item?.requirement === "필수");
  }

  // 필수 서류마다 지금 상태 하나를 정한다. 같은 종류가 여러 건이면 최근 것을 본다.
  function onboardingChecklist(types, docs) {
    const rows = (Array.isArray(docs) ? docs : []).filter(doc => !doc?.deleted_at && !doc?.is_placeholder && !doc?.missing_document);
    return requiredDocTypes(types).map(meta => {
      const mine = rows.filter(doc => clean(doc.document_type || doc.documentType) === meta.type && lower(doc.status) !== "superseded")
        .sort((a, b) => clean(b.uploaded_at || b.updated_at).localeCompare(clean(a.uploaded_at || a.updated_at)));
      const latest = mine[0] || null;
      const status = lower(latest?.status);
      let state = "missing";
      if (latest) state = RESUBMIT_STATUSES.includes(status) ? "resubmit" : (["approved", "verified", "accepted"].includes(status) ? "approved" : "submitted");
      const label = { missing: "제출 필요", resubmit: "다시 제출 필요", approved: "확인 완료", submitted: "제출됨 · 확인 중" }[state];
      return { type: meta.type, label: meta.label || meta.type, state, stateLabel: clean(latest?.status_label) && state !== "missing" ? clean(latest.status_label) : label, doc: latest, memo: clean(latest?.review_memo) };
    });
  }

  function onboardingProgress(checklist) {
    const done = checklist.filter(item => item.state === "submitted" || item.state === "approved").length;
    return { done, total: checklist.length };
  }

  function expiryRequired(type) {
    return ONBOARDING_EXPIRY_REQUIRED_TYPES.includes(String(type || ""));
  }

  // 보건증은 검진일부터 1년(검진일+1년-1일)을 추천값으로 채운다. 서류의 날짜가 다르면 직원이 고친다.
  function suggestExpiry(type, issueDate) {
    if (type !== "health_certificate" || !/^\d{4}-\d{2}-\d{2}$/.test(String(issueDate || ""))) return "";
    const date = new Date(`${issueDate}T00:00:00Z`);
    date.setUTCFullYear(date.getUTCFullYear() + 1);
    date.setUTCDate(date.getUTCDate() - 1);
    return date.toISOString().slice(0, 10);
  }

  function validateUpload(input) {
    if (!input?.file) return "제출할 파일을 선택해 주세요.";
    if (Number(input.file.size) > MAX_UPLOAD_BYTES) return "파일은 최대 15MB까지 올릴 수 있습니다. 사진 크기를 줄여 다시 선택해 주세요.";
    if (!clean(input.documentType)) return "서류 종류를 선택해 주세요.";
    if (expiryRequired(input.documentType) && !clean(input.expiresAt)) return "이 서류는 만료일을 입력해야 합니다. 서류에 적힌 유효기간 끝 날짜를 넣어 주세요.";
    if (!clean(input.employeeEmail)) return "로그인 계정의 이메일을 확인할 수 없습니다. 다시 로그인해 주세요.";
    return "";
  }

  // KST 기준 날짜(YYYY-MM-DD).
  function kstDate(now = Date.now()) {
    return new Date(Number(now) + 9 * 3600 * 1000).toISOString().slice(0, 10);
  }

  function shiftMonth(month, delta) {
    const [y, m] = String(month).split("-").map(Number);
    const date = new Date(Date.UTC(y, m - 1 + delta, 1));
    return date.toISOString().slice(0, 7);
  }

  function monthBounds(month) {
    const [y, m] = String(month).split("-").map(Number);
    const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
    return { from: `${month}-01`, to: `${month}-${String(last).padStart(2, "0")}`, days: last };
  }

  // 일요일 시작 주 단위 칸. 달 밖 칸은 null.
  function monthGrid(month) {
    const { days } = monthBounds(month);
    const [y, m] = String(month).split("-").map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1)).getUTCDay();
    const cells = [];
    for (let i = 0; i < first; i += 1) cells.push(null);
    for (let d = 1; d <= days; d += 1) cells.push(`${month}-${String(d).padStart(2, "0")}`);
    while (cells.length % 7) cells.push(null);
    const weeks = [];
    for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
    return weeks;
  }

  const ATTENDANCE_STATUS = { approved: ["ok", "확정"], pending: ["warn", "확인대기"], rejected: ["muted", "제외"] };

  function minutesBetween(start, end, breakMinutes) {
    const parse = value => {
      const match = /^(\d{1,2}):(\d{2})/.exec(String(value || ""));
      return match ? Number(match[1]) * 60 + Number(match[2]) : null;
    };
    const s = parse(start);
    const e = parse(end);
    if (s === null || e === null) return 0;
    const span = e >= s ? e - s : e + 1440 - s;
    return Math.max(0, span - (Number(breakMinutes) || 0));
  }

  function attendanceEntry(record, businessName = "") {
    const raw = record?.raw || record || {};
    const start = clean(raw.start_at).slice(0, 5);
    const end = clean(raw.end_at).slice(0, 5);
    const breakMin = Number(raw.break_minutes || 0);
    const worked = Number(raw.worked_minutes) > 0 ? Number(raw.worked_minutes) : minutesBetween(start, end, breakMin);
    const status = lower(raw.status) || "pending";
    return {
      id: clean(record?.id || raw.id),
      date: clean(raw.work_date).slice(0, 10),
      start, end, breakMin, worked, status,
      statusLabel: (ATTENDANCE_STATUS[status] || ["muted", clean(raw.status) || "상태 확인"])[1],
      tone: (ATTENDANCE_STATUS[status] || ["muted"])[0],
      business: clean(businessName || record?.business_name || raw.business_name),
      branch: clean(raw.branch),
      memo: clean(raw.memo)
    };
  }

  function attendanceByDay(entries) {
    const map = {};
    (entries || []).forEach(entry => {
      if (!entry.date) return;
      (map[entry.date] = map[entry.date] || []).push(entry);
    });
    Object.values(map).forEach(list => list.sort((a, b) => a.start.localeCompare(b.start)));
    return map;
  }

  function attendanceSummary(entries, month) {
    const inMonth = (entries || []).filter(entry => entry.date.startsWith(month));
    const counted = inMonth.filter(entry => entry.status !== "rejected");
    const minutes = counted.reduce((sum, entry) => sum + entry.worked, 0);
    return {
      days: new Set(counted.map(entry => entry.date)).size,
      minutes,
      hoursText: `${Math.floor(minutes / 60)}시간${minutes % 60 ? ` ${minutes % 60}분` : ""}`,
      pending: inMonth.filter(entry => entry.status === "pending").length
    };
  }

  function durationText(minutes) {
    const m = Math.max(0, Math.round(Number(minutes) || 0));
    if (!m) return "";
    return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
  }

  // 실패 → 다음 행동. 화면은 이 결과를 그대로 그린다.
  function recoveryFor(error, context = "") {
    const status = Number(error?.status);
    if (error?.code === "EMPLOYEE_PREVIEW_READ_ONLY") {
      return { kind: "read_only", title: "미리보기는 읽기 전용입니다", message: READ_ONLY_MESSAGE, actions: ["close"] };
    }
    if (status === 401) {
      return { kind: "session", title: "로그인이 만료되었습니다", message: "다시 로그인하면 이 화면으로 돌아옵니다. 작성 중이던 서명·서류는 아직 제출되지 않았습니다.", actions: ["login", "retry"] };
    }
    if (status === 403) {
      return { kind: "forbidden", title: "이 화면을 볼 권한이 없습니다", message: `${clean(error?.message) ? `${clean(error.message)}. ` : ""}회사·점포 연결이 바뀌었을 수 있습니다. 관리자에게 계정 연결을 확인해 달라고 요청하거나 다른 계정으로 로그인해 주세요.`, actions: ["login", "retry"] };
    }
    if (error?.network || status === 0 || !Number.isFinite(status)) {
      return { kind: "network", title: "연결을 확인해 주세요", message: `${context ? `${context}을(를) ` : ""}불러오지 못했습니다. 와이파이·데이터 연결을 확인한 뒤 다시 시도해 주세요.`, actions: ["retry"] };
    }
    return { kind: "error", title: "잠시 후 다시 시도해 주세요", message: `${context ? `${context} 조회 중 ` : ""}서버가 요청을 처리하지 못했습니다(${status}). ${clean(error?.message)}`.trim(), actions: ["retry"] };
  }

  function assertWritable(method, readOnly) {
    const verb = String(method || "GET").toUpperCase();
    if (readOnly && !["GET", "HEAD", "OPTIONS"].includes(verb)) {
      const error = new Error(READ_ONLY_MESSAGE);
      error.code = "EMPLOYEE_PREVIEW_READ_ONLY";
      throw error;
    }
  }

  function loginUrl(contractToken = "") {
    return contractToken ? `${PATHS.login}?yf_contract_token=${encodeURIComponent(contractToken)}` : PATHS.login;
  }

  function errorDetail(payload, status) {
    const detail = payload?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object") return clean(detail.message || detail.reason) || `HTTP ${status}`;
    return clean(payload?.message) || `HTTP ${status}`;
  }

  const core = {
    PATHS, PREVIEW_REQUEST_KEY, PREVIEW_RETURN_KEY, PREVIEW_REQUEST_TTL_MS, READ_ONLY_MESSAGE, SECTIONS, MAX_UPLOAD_BYTES,
    esc, isEmployeeSession, decideEntry, employeeStatus, safeReturnHash, consumePreviewRequest, previewCandidates, filterCandidates,
    maskEmail, matchesTarget, scopeToTarget, visiblePayroll, payrollCard, requiredDocTypes, onboardingChecklist, onboardingProgress,
    expiryRequired, suggestExpiry, validateUpload, kstDate, shiftMonth, monthBounds, monthGrid, attendanceEntry, attendanceByDay,
    attendanceSummary, durationText, minutesBetween, recoveryFor, assertWritable, loginUrl, errorDetail
  };

  // ── 화면 ────────────────────────────────────────────────────────────────

  function createApp(env) {
    const doc = env.document;
    const win = env.window;
    const fetchImpl = env.fetch;
    const storage = env.sessionStorage || null;
    const local = env.localStorage || null;
    const portalApi = env.portal;
    const contractCore = env.contractCore || {};
    const corrections = env.corrections || null;
    const params = new URLSearchParams(win.location.search || "");
    const app = {
      mode: "", ready: false, session: null, target: null, returnHash: "", section: "", contractToken: clean(params.get("yf_contract_token")),
      candidates: [], pickerQuery: "",
      data: { contracts: [], pendingSign: [], payroll: [], docs: [], docTypes: [], joinRequests: [], businesses: [] },
      loaded: {}, errors: {}, busy: {},
      month: kstDate().slice(0, 7), attendance: [], selectedDay: "", uploadType: "", uploadMessage: ""
    };
    const $ = id => doc.getElementById(id);

    // ── 인증·요청 ──
    function cookieValue(name) {
      const found = String(doc.cookie || "").split(";").map(part => part.trim()).find(part => part.startsWith(`${name}=`));
      return found ? decodeURIComponent(found.slice(name.length + 1)) : "";
    }
    function authToken() {
      for (const key of TOKEN_KEYS) {
        try { const value = local?.getItem(key); if (value) return value; } catch (_) { /* 저장소 차단 */ }
      }
      return cookieValue("aads_token") || cookieValue("fb_access_token");
    }
    function clearAuth() {
      TOKEN_KEYS.forEach(key => {
        try { local?.removeItem(key); } catch (_) { /* 저장소 차단 */ }
        doc.cookie = `${key}=; path=/; max-age=0; SameSite=Lax`;
      });
    }
    function saveToken(token) {
      TOKEN_KEYS.forEach(key => { try { local?.setItem(key, token); } catch (_) { /* 저장소 차단 */ } });
      const secure = win.location.protocol === "https:" ? "; Secure" : "";
      doc.cookie = `aads_token=${encodeURIComponent(token)}; path=/; max-age=604800; SameSite=Lax${secure}`;
    }

    async function send(url, options = {}, as = "json") {
      assertWritable(options.method, app.mode === "preview");
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const timer = setTimeout(() => controller?.abort(), REQUEST_TIMEOUT_MS);
      const headers = { ...(options.headers || {}) };
      const token = authToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      if (typeof options.body === "string" && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
      let response;
      try {
        response = await fetchImpl(url, { credentials: "same-origin", ...options, headers, signal: controller?.signal });
      } catch (cause) {
        const error = new Error(cause?.name === "AbortError" ? "서버 응답이 15초 안에 오지 않았습니다." : "네트워크에 연결할 수 없습니다.");
        error.status = 0;
        error.network = true;
        throw error;
      } finally {
        clearTimeout(timer);
      }
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const error = new Error(errorDetail(payload || {}, response.status));
        error.status = response.status;
        throw error;
      }
      if (as === "blob") return response;
      return (await response.json().catch(() => ({}))) || {};
    }
    const financeApi = (path, options) => send(`${FINANCE_BASE}${path}`, options);
    const workspaceApi = (path, options) => send(`${WORKSPACE_BASE}${path}`, options);
    const authApi = (path, options) => send(`${AUTH_BASE}${path}`, options);

    // ── 알림·복구 ──
    let toastTimer = null;
    function showToast(message) {
      const el = $("emToast");
      if (!el) return;
      el.textContent = String(message || "");
      el.hidden = !message;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
    }

    function goLogin() {
      clearAuth();
      win.location.replace(loginUrl(app.contractToken));
    }

    function actionButtons(actions) {
      const labels = { login: "다시 로그인", retry: "다시 시도", admin: "관리자 화면으로", close: "닫기" };
      return actions.map((key, i) => `<button type="button" class="em-btn${i === 0 ? " primary" : ""}" data-em-recover="${key}">${labels[key]}</button>`).join("");
    }

    // 시작 단계 실패: 포털을 끄고 복구 화면 하나만 보인다.
    function renderFatal(recovery) {
      app.ready = false;
      syncPortal();
      const host = $("emRecovery");
      host.hidden = false;
      host.innerHTML = `<section class="em-recovery" role="alert"><div class="em-logo"><i>오</i>오비서 직원</div><h1>${esc(recovery.title)}</h1><p>${esc(recovery.message)}</p><div class="em-actions">${actionButtons(recovery.actions)}</div></section>`;
      setLoading(false);
    }

    // 사용 중 세션 만료: 화면은 두고 위에 안내를 띄운다(보던 계약서·입력값 유지).
    function showSessionAlert() {
      const host = $("emAlert");
      if (!host) return;
      const recovery = recoveryFor({ status: 401 });
      host.hidden = false;
      host.innerHTML = `<b>${esc(recovery.title)}</b><span>${esc(recovery.message)}</span><div class="em-actions">${actionButtons(["login"])}</div>`;
    }

    function handleError(error, context) {
      if (Number(error?.status) === 401) { showSessionAlert(); return; }
      if (error?.code === "EMPLOYEE_PREVIEW_READ_ONLY") { showToast(READ_ONLY_MESSAGE); return; }
      showToast(recoveryFor(error, context).message);
    }

    function setLoading(on, text) {
      const el = $("emLoading");
      if (!el) return;
      el.hidden = !on;
      if (text) el.textContent = text;
    }

    // ── 포털(계약서) 연결 ──
    function ownRequest() {
      const requests = app.mode === "preview"
        ? app.data.joinRequests.filter(item => app.target?.requestId ? clean(item.id) === app.target.requestId : lower(item.email || item.employee_email) === app.target?.email)
        : app.data.joinRequests;
      const approved = requests.filter(item => lower(item.status) === "approved");
      const preferred = clean(app.session?.permissions?.employee_request_id);
      return approved.find(item => clean(item.id) === preferred) || approved[0] || requests[0] || null;
    }
    function employeeEmail() {
      return app.mode === "preview" ? (app.target?.email || "") : lower(app.session?.user?.email);
    }
    function employeeName() {
      const plain = value => { const text = clean(value); return text && !text.includes("@") ? text : ""; };
      if (app.mode === "preview") return plain(app.target?.name) || plain(ownRequest()?.name) || "직원";
      return plain(ownRequest()?.name || ownRequest()?.employee_name) || plain(app.session?.user?.name) || plain(employeeEmail().split("@")[0]) || "직원";
    }

    const bridge = {
      get root() { return $("employeePortal"); },
      get active() { return app.ready; },
      get readOnly() { return app.mode === "preview"; },
      get employeeEmail() { return employeeEmail(); },
      get employeeName() { return employeeName(); },
      get branch() { return clean(ownRequest()?.branch); },
      get currentView() { return app.section; },
      get today() { return kstDate(); },
      get contracts() { return app.data.contracts; },
      get pendingSign() { return app.data.pendingSign; },
      get payroll() { return app.data.payroll; },
      get onboardingDocs() { return app.data.docs; },
      get requiredDocTypes() { return requiredDocTypes(app.data.docTypes); },
      labels: { contractType: contractCore.contractTypeLabels || {} },
      contractPreviewHtml: contract => (contractCore.contractPreviewHtml ? contractCore.contractPreviewHtml(contract) : ""),
      financeApi,
      setView: name => openSection(name),
      showToast,
      handleExpiredServerSession: showSessionAlert,
      downloadPdf: (id, button) => downloadFile(`${FINANCE_BASE}/contracts/${encodeURIComponent(id)}/signed-pdf`, "계약서_서명본.pdf", button),
      reloadContracts: () => loadContracts().then(syncPortal).catch(error => handleError(error, "계약서")),
      logout,
      onSigned: payload => {
        const signed = payload?.contract;
        if (signed?.id) app.data.contracts = [signed, ...app.data.contracts.filter(item => item.id !== signed.id)];
        loadPending().then(syncPortal).catch(() => {});
        if ((payload?.bundle?.results || []).length > 1) loadContracts().then(syncPortal).catch(() => {});
      }
    };

    function syncPortal() {
      doc.body.classList.toggle("employee-preview", app.mode === "preview" && app.ready);
      portalApi?.sync(bridge);
    }

    // ── 자료 불러오기 ──
    async function loadContracts() {
      const payload = await financeApi("/contracts");
      const rows = Array.isArray(payload.contracts) ? payload.contracts : [];
      app.data.contracts = app.mode === "preview" ? scopeToTarget(rows, app.target) : rows;
    }
    async function loadPending() {
      if (app.mode === "preview") { app.data.pendingSign = []; return; }
      const payload = await financeApi("/contracts/pending-signature");
      app.data.pendingSign = Array.isArray(payload.pending) ? payload.pending : [];
    }
    async function loadPayroll() {
      const payload = await financeApi("/payroll");
      app.data.payroll = visiblePayroll(payload.statements, { preview: app.mode === "preview", target: app.target });
    }
    async function loadDocs() {
      const [types, docs] = await Promise.all([financeApi("/onboarding/document-types"), financeApi("/onboarding/documents")]);
      app.data.docTypes = Array.isArray(types.document_types) ? types.document_types : [];
      const rows = Array.isArray(docs.documents) ? docs.documents : [];
      app.data.docs = app.mode === "preview" ? scopeToTarget(rows, app.target) : rows;
    }
    async function loadJoinRequests() {
      const payload = await financeApi("/employees/join-requests");
      app.data.joinRequests = Array.isArray(payload.requests) ? payload.requests : [];
    }
    async function loadBusinesses() {
      const payload = await workspaceApi("/businesses");
      app.data.businesses = Array.isArray(payload.businesses) ? payload.businesses : [];
      contractCore.setBusinessResolver?.(id => app.data.businesses.find(item => clean(item.id) === clean(id)) || null);
    }
    async function loadAttendance() {
      if (!app.loaded.businesses) { await loadBusinesses(); app.loaded.businesses = true; }
      const { from, to } = monthBounds(app.month);
      const month = app.month;
      const entries = [];
      if (app.mode === "preview" && !app.target?.email) {
        app.attendance = [];
        app.errors.attendance = { kind: "notice", message: "이 직원은 이메일이 없어 근태를 조회할 수 없습니다." };
        return;
      }
      for (const business of app.data.businesses) {
        const query = new URLSearchParams({ from, to, limit: "500" });
        if (app.mode === "preview") query.set("employee_email", app.target.email);
        const payload = await workspaceApi(`/${encodeURIComponent(business.id)}/attendance/records?${query}`);
        (payload.records || []).forEach(record => entries.push(attendanceEntry(record, app.data.businesses.length > 1 ? business.name : "")));
      }
      if (month === app.month) app.attendance = entries;
    }

    // 각 묶음은 따로 실패한다 — 급여가 안 와도 계약서는 보인다.
    async function loadPart(key, loader, context) {
      app.busy[key] = true;
      try {
        await loader();
        app.loaded[key] = true;
        delete app.errors[key];
      } catch (error) {
        if (Number(error?.status) === 401) showSessionAlert();
        app.errors[key] = recoveryFor(error, context);
      } finally {
        app.busy[key] = false;
      }
    }

    async function loadAll() {
      await Promise.all([
        loadPart("joinRequests", loadJoinRequests, "근무지 정보"),
        loadPart("contracts", loadContracts, "계약서"),
        loadPart("pending", loadPending, "서명 대기 목록"),
        loadPart("payroll", loadPayroll, "급여명세"),
        loadPart("docs", loadDocs, "입사서류"),
        loadPart("businesses", loadBusinesses, "사업장")
      ]);
    }

    // ── 섹션(급여·근태·서류) ──
    function openSection(name) {
      if (!SECTIONS.includes(name)) return;
      app.section = name;
      renderSection();
      if (name === "attendance" && !app.loaded.attendance && !app.busy.attendance) reloadAttendance();
    }

    function sectionHead(title, desc, reloadKey, extra = "") {
      return `<header class="em-head"><div><h2>${esc(title)}</h2><p>${esc(desc)}</p></div><div class="em-head-actions">${extra}<button type="button" class="em-btn" data-em-reload="${reloadKey}">새로고침</button></div></header>`;
    }

    function errorBox(error) {
      if (!error) return "";
      if (error.kind === "notice") return `<p class="em-note" role="status">${esc(error.message)}</p>`;
      return `<div class="em-alert-box" role="alert"><b>${esc(error.title)}</b><p>${esc(error.message)}</p><div class="em-actions">${actionButtons(error.actions)}</div></div>`;
    }

    function payrollHtml() {
      const rows = app.data.payroll.map(payrollCard);
      let body;
      if (app.busy.payroll && !app.loaded.payroll) body = '<p class="em-note">급여명세를 불러오는 중입니다.</p>';
      else if (app.errors.payroll) body = errorBox(app.errors.payroll);
      else if (!rows.length) body = '<div class="em-empty"><b>아직 발행된 급여명세가 없습니다.</b><p>관리자가 명세를 확정하면 여기에 표시됩니다. 지급일이 지났는데 보이지 않으면 관리자에게 문의해 주세요.</p></div>';
      else body = `<div class="em-list">${rows.map(card => `<article class="em-card"><div class="em-card-top"><b>${esc(card.title)}</b><span class="em-badge ${card.status[0]}">${esc(card.status[1])}</span></div>
        <dl class="em-kv"><dt>총지급</dt><dd>${esc(card.gross)}</dd><dt>공제 합계</dt><dd>${esc(card.deductions)}</dd>${card.held ? `<dt>보류</dt><dd>${esc(card.held)}</dd>` : ""}<dt>실지급</dt><dd class="em-strong">${esc(card.net)}</dd><dt>지급일</dt><dd>${esc(card.payDate || "-")}</dd></dl>
        <button type="button" class="em-btn block" data-em-payroll-pdf="${esc(card.id)}" data-em-name="${esc(card.title)}">명세서 PDF 받기</button></article>`).join("")}</div>`;
      return `${sectionHead("급여명세", "확정된 급여명세만 보입니다. 금액이 다르면 관리자에게 문의해 주세요.", "payroll")}${body}`;
    }

    function attendanceHtml() {
      const preview = app.mode === "preview";
      const summary = attendanceSummary(app.attendance, app.month);
      const byDay = attendanceByDay(app.attendance);
      const today = kstDate();
      const [y, m] = app.month.split("-");
      const clock = preview ? "" : `<a class="em-btn primary" href="${PATHS.clock}" data-em-clock>출퇴근 하기</a>`;
      const week = ["일", "월", "화", "수", "목", "금", "토"];
      const grid = monthGrid(app.month).map(row => row.map(day => {
        if (!day) return '<span class="em-day empty" aria-hidden="true"></span>';
        const list = byDay[day] || [];
        const minutes = list.filter(entry => entry.status !== "rejected").reduce((sum, entry) => sum + entry.worked, 0);
        const pending = list.some(entry => entry.status === "pending");
        const label = `${Number(day.slice(8))}일${list.length ? ` 근무 ${durationText(minutes) || "기록"}${pending ? " 확인대기 포함" : ""}` : " 기록 없음"}`;
        return `<button type="button" class="em-day${list.length ? " has" : ""}${pending ? " pending" : ""}${day === app.selectedDay ? " on" : ""}" data-em-day="${day}" aria-label="${esc(label)}"${day === today ? ' aria-current="date"' : ""}><span>${Number(day.slice(8))}</span><small>${esc(list.length ? (durationText(minutes) || "기록") : "")}</small></button>`;
      }).join("")).join("");
      const selected = app.selectedDay && byDay[app.selectedDay] ? byDay[app.selectedDay] : [];
      const detail = app.selectedDay
        ? `<div class="em-card em-day-detail" aria-live="polite"><b>${esc(app.selectedDay)}</b>${selected.length ? selected.map(entry => `<div class="em-shift"><span>${esc(entry.start || "미기록")}–${esc(entry.end || "미기록")} · 휴게 ${esc(entry.breakMin)}분 · ${esc(durationText(entry.worked) || "0:00")}</span><span class="em-badge ${entry.tone}">${esc(entry.statusLabel)}</span>${entry.business || entry.branch ? `<small>${esc([entry.business, entry.branch].filter(Boolean).join(" · "))}</small>` : ""}</div>`).join("") : '<p class="em-note">이 날은 기록이 없습니다. 근무했는데 빠졌다면 아래 \'근무 직접 입력\'으로 신청해 주세요.</p>'}</div>`
        : '<p class="em-note">날짜를 누르면 그날 출퇴근 기록을 볼 수 있습니다.</p>';
      let body;
      if (app.busy.attendance && !app.loaded.attendance) body = '<p class="em-note">근태 기록을 불러오는 중입니다.</p>';
      else if (app.errors.attendance) body = errorBox(app.errors.attendance);
      else body = `<div class="em-metrics"><div><small>근무일</small><b>${summary.days}일</b></div><div><small>근무시간</small><b>${esc(summary.hoursText)}</b></div><div><small>확인대기</small><b>${summary.pending}건</b></div></div>
        <div class="em-cal" role="group" aria-label="${esc(`${y}년 ${Number(m)}월 근태 달력`)}">${week.map(w => `<span class="em-week">${w}</span>`).join("")}${grid}</div>${detail}`;
      const fix = preview
        ? '<p class="em-note" role="status">정정 요청·근무 직접 입력은 직원 본인 계정에서만 할 수 있습니다(읽기 전용 미리보기).</p>'
        : '<section class="em-card em-fix"><h3>근태 정정 요청</h3><p class="em-note">기록이 틀렸으면 정정을 요청하세요. 관리자 승인 후 반영됩니다.</p><div id="emCorrections"></div><details class="em-details"><summary>근무 직접 입력(기록이 아예 없을 때)</summary><div id="emSelfEntry"></div></details></section>';
      return `${sectionHead("근태", "이번 달 출퇴근 기록과 확인 상태입니다.", "attendance", clock)}
        <div class="em-month"><button type="button" class="em-btn" data-em-month="-1" aria-label="이전 달">‹ 이전 달</button><b>${esc(`${y}년 ${Number(m)}월`)}</b><button type="button" class="em-btn" data-em-month="1" aria-label="다음 달">다음 달 ›</button></div>${body}${fix}`;
    }

    function onboardingHtml() {
      const preview = app.mode === "preview";
      const status = app.mode === "employee" ? employeeStatus(app.session) : "approved";
      const notice = status === "pending"
        ? '<p class="em-note em-warn" role="status">가입 승인 대기 중입니다. 서류는 먼저 올릴 수 있고, 관리자가 승인하면 계약서·급여명세가 보입니다.</p>'
        : status === "rejected" ? '<p class="em-note em-warn" role="status">가입 요청이 반려되었습니다. 관리자에게 사유를 확인해 주세요.</p>' : "";
      const checklist = onboardingChecklist(app.data.docTypes, app.data.docs);
      const progress = onboardingProgress(checklist);
      let body;
      if (app.busy.docs && !app.loaded.docs) body = '<p class="em-note">입사서류를 불러오는 중입니다.</p>';
      else if (app.errors.docs) body = errorBox(app.errors.docs);
      else {
        const rows = checklist.length
          ? checklist.map(item => `<div class="em-doc"><div><b>${esc(item.label)}</b><span class="em-badge ${item.state === "approved" ? "ok" : item.state === "submitted" ? "info" : "warn"}">${esc(item.stateLabel)}</span>${item.memo && item.state === "resubmit" ? `<small>관리자 메모: ${esc(item.memo)}</small>` : ""}</div><div class="em-doc-actions">${item.doc ? `<button type="button" class="em-btn" data-em-doc-view="${esc(item.doc.id)}" data-em-name="${esc(item.doc.original_filename || item.label)}">보기</button>` : ""}${preview || status === "rejected" ? "" : `<button type="button" class="em-btn${item.state === "missing" || item.state === "resubmit" ? " primary" : ""}" data-em-doc-start="${esc(item.type)}">${item.state === "missing" ? "제출하기" : "다시 제출"}</button>`}</div></div>`).join("")
          : '<p class="em-note">제출해야 할 필수 서류가 없습니다.</p>';
        const requiredSet = new Set(checklist.map(item => item.type));
        const others = app.data.docs.filter(doc => !requiredSet.has(clean(doc.document_type)) && !doc.deleted_at && !doc.is_placeholder);
        const otherHtml = others.length ? `<h3 class="em-sub">기타 제출 서류</h3>${others.map(doc => `<div class="em-doc"><div><b>${esc(doc.document_label || doc.document_type)}</b><span class="em-badge info">${esc(doc.status_label || doc.status || "제출됨")}</span></div><div class="em-doc-actions"><button type="button" class="em-btn" data-em-doc-view="${esc(doc.id)}" data-em-name="${esc(doc.original_filename || doc.document_label || "서류")}">보기</button></div></div>`).join("")}` : "";
        body = `<div class="em-card"><p class="em-progress">필수 서류 <b>${progress.done} / ${progress.total}</b> 제출</p>${rows}${otherHtml}</div>${preview ? '<p class="em-note" role="status">서류 제출은 직원 본인 계정에서만 할 수 있습니다(읽기 전용 미리보기).</p>' : (status === "rejected" ? "" : uploadFormHtml())}`;
      }
      return `${sectionHead("입사서류", "근무에 필요한 서류를 사진이나 PDF로 올려 주세요.", "docs")}${notice}${body}`;
    }

    function uploadFormHtml() {
      const types = app.data.docTypes;
      const requests = app.data.joinRequests.filter(item => lower(item.status) === "approved");
      const own = ownRequest();
      const type = app.uploadType || "";
      const workplace = requests.length > 1
        ? `<label>근무지<select name="request_id" class="em-field" required>${requests.map(item => `<option value="${esc(item.id)}"${clean(item.id) === clean(own?.id) ? " selected" : ""}>${esc([item.business_name || item.workplace, item.branch].filter(Boolean).join(" · ") || item.branch || "근무지")}</option>`).join("")}</select></label>`
        : "";
      return `<form class="em-card em-upload" data-em-upload novalidate><h3>서류 올리기</h3>${workplace}
        <label>서류 종류<select name="document_type" class="em-field" required><option value="">선택해 주세요</option>${types.map(item => `<option value="${esc(item.type)}"${item.type === type ? " selected" : ""}>${esc(item.label || item.type)}${item.requirement === "필수" ? " (필수)" : ""}</option>`).join("")}</select></label>
        <div class="em-two"><label>발급일·검진일 (선택)<input type="date" name="issue_date" class="em-field"></label><label>만료일 <span data-em-expiry-hint>${expiryRequired(type) ? "(필수)" : "(선택)"}</span><input type="date" name="expires_at" class="em-field"${expiryRequired(type) ? " required" : ""}></label></div>
        <label>메모 (선택)<input type="text" name="memo" class="em-field" maxlength="300" placeholder="관리자에게 남길 말"></label>
        <label>파일 (사진·PDF, 15MB 이하)<input type="file" name="file" class="em-field" accept="image/*,application/pdf" required></label>
        <p class="em-error" role="alert" data-em-upload-error>${esc(app.uploadMessage)}</p>
        <button type="submit" class="em-btn primary block">서류 제출</button></form>`;
    }

    function renderSection() {
      const host = $("emSection");
      if (!host) return;
      if (!app.ready || !SECTIONS.includes(app.section)) { host.innerHTML = ""; return; }
      host.innerHTML = app.section === "payroll" ? payrollHtml() : app.section === "attendance" ? attendanceHtml() : onboardingHtml();
      if (app.section === "attendance") mountCorrections();
    }

    // 정정 요청 모듈은 사업장마다 하나. 같은 직원·사업장 구성이면 다시 붙이지 않고 목록만 갱신한다.
    let correctionKey = "";
    const correctionHosts = [];
    function mountCorrections() {
      if (app.mode === "preview" || !corrections || !app.loaded.businesses) return;
      const box = $("emCorrections");
      const selfBox = $("emSelfEntry");
      if (!box || !selfBox) return;
      const key = `${employeeEmail()}#${app.data.businesses.map(item => item.id).join("|")}`;
      if (key === correctionKey && correctionHosts.length) {
        correctionHosts.forEach(({ fix, self }) => { box.appendChild(fix); selfBox.appendChild(self); self.obysRefresh?.(); });
        return;
      }
      correctionKey = key;
      correctionHosts.length = 0;
      if (!app.data.businesses.length) { box.innerHTML = '<p class="em-note">연결된 사업장이 없어 정정 요청을 할 수 없습니다. 관리자에게 근무지 연결을 확인해 주세요.</p>'; return; }
      app.data.businesses.forEach(business => {
        const fix = doc.createElement("div");
        const self = doc.createElement("div");
        fix.className = "em-fix-slot";
        self.className = "em-fix-slot";
        box.appendChild(fix);
        selfBox.appendChild(self);
        correctionHosts.push({ fix, self });
        const onSaved = () => reloadAttendance();
        Promise.resolve(corrections.mount(fix, { api: workspaceApi, businessId: business.id, onSaved })).catch(() => {});
        Promise.resolve(corrections.mountSelfEntry?.(self, { api: workspaceApi, businessId: business.id, businessName: app.data.businesses.length > 1 ? business.name : "", onSaved })).catch(() => {});
      });
    }

    async function reloadAttendance() {
      await loadPart("attendance", loadAttendance, "근태 기록");
      if (app.section === "attendance") renderSection();
    }

    async function reload(key) {
      if (key === "payroll") await loadPart("payroll", loadPayroll, "급여명세");
      else if (key === "docs") await loadPart("docs", loadDocs, "입사서류");
      else if (key === "attendance") { await reloadAttendance(); return; }
      renderSection();
      syncPortal();
    }

    // ── 내려받기·업로드 ──
    async function downloadFile(url, fallbackName, button) {
      if (button) button.disabled = true;
      try {
        const response = await send(url, {}, "blob");
        const blob = await response.blob();
        const disposition = response.headers?.get?.("content-disposition") || "";
        const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
        const plain = disposition.match(/filename="?([^";]+)"?/i)?.[1];
        const name = encoded ? decodeURIComponent(encoded) : (plain || fallbackName);
        const link = doc.createElement("a");
        link.href = win.URL.createObjectURL(blob);
        link.download = name;
        doc.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => win.URL.revokeObjectURL(link.href), 30000);
        showToast("파일을 내려받았습니다.");
      } catch (error) {
        handleError(error, "파일");
      } finally {
        if (button) button.disabled = false;
      }
    }

    async function submitUpload(form) {
      if (app.mode === "preview") { showToast(READ_ONLY_MESSAGE); return; }
      const requests = app.data.joinRequests.filter(item => lower(item.status) === "approved");
      const chosen = requests.find(item => clean(item.id) === clean(form.elements.request_id?.value)) || ownRequest();
      const input = {
        file: form.elements.file?.files?.[0] || null,
        documentType: form.elements.document_type.value,
        issueDate: form.elements.issue_date.value,
        expiresAt: form.elements.expires_at.value || suggestExpiry(form.elements.document_type.value, form.elements.issue_date.value),
        memo: form.elements.memo.value,
        employeeEmail: lower(app.session?.user?.email)
      };
      const problem = validateUpload(input);
      const errorEl = form.querySelector("[data-em-upload-error]");
      if (problem) { errorEl.textContent = problem; return; }
      const body = new FormData();
      body.append("employee_name", employeeName());
      body.append("employee_email", input.employeeEmail);
      body.append("branch", clean(chosen?.branch));
      body.append("employee_request_id", clean(chosen?.id));
      body.append("business_id", clean(chosen?.business_id || chosen?.businessId));
      body.append("document_type", input.documentType);
      body.append("issue_date", input.issueDate || "");
      body.append("expires_at", input.expiresAt || "");
      body.append("memo", clean(input.memo));
      body.append("file", input.file);
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      errorEl.textContent = "올리는 중입니다. 창을 닫지 마세요.";
      try {
        await financeApi("/onboarding/documents", { method: "POST", body });
        app.uploadType = "";
        app.uploadMessage = "";
        await loadPart("docs", loadDocs, "입사서류");
        renderSection();
        syncPortal();
        showToast("서류를 제출했습니다. 관리자 확인 후 상태가 바뀝니다.");
      } catch (error) {
        if (Number(error?.status) === 401) showSessionAlert();
        errorEl.textContent = Number(error?.status) === 413
          ? "파일은 최대 15MB까지 올릴 수 있습니다. 크기를 줄여 다시 선택해 주세요."
          : `제출하지 못했습니다: ${error.message}. 선택한 파일은 그대로 있으니 다시 제출해 주세요.`;
        button.disabled = false;
      }
    }

    // ── 미리보기(관리자) ──
    function renderPreviewBanner() {
      const banner = $("emPreviewBanner");
      if (!banner) return;
      const on = app.mode === "preview";
      banner.hidden = !on;
      if (!on) return;
      $("emPreviewText").textContent = app.target ? `${app.target.name || maskEmail(app.target.email) || "직원"} 님 화면을 보는 중입니다 (읽기 전용)` : "미리볼 직원을 선택해 주세요 (읽기 전용)";
      $("emPreviewSwitch").hidden = !app.target;
    }

    function renderPicker() {
      const host = $("emPicker");
      if (!host) return;
      const show = app.mode === "preview" && !app.target;
      host.hidden = !show;
      if (!show) { host.innerHTML = ""; return; }
      const list = filterCandidates(app.candidates, app.pickerQuery);
      const rows = !app.candidates.length
        ? '<p class="em-note">미리 볼 수 있는 승인된 직원이 없습니다. 직원 가입 승인 후 다시 열어 주세요.</p>'
        : (list.length ? list.map((item, i) => `<button type="button" class="em-pick" data-em-pick="${i}"><b>${esc(item.name || maskEmail(item.email))}</b><span>${esc([item.branch, maskEmail(item.email)].filter(Boolean).join(" · "))}</span></button>`).join("") : '<p class="em-note">검색 결과가 없습니다.</p>');
      host.innerHTML = `<section class="em-card em-picker"><h1>직원 화면 보기</h1><p class="em-note">직원을 고르면 그 직원이 보는 화면을 읽기 전용으로 엽니다. 서명·제출·출퇴근은 할 수 없습니다.</p>
        <input type="search" class="em-field" data-em-pick-search placeholder="직원 이름·지점 검색" aria-label="직원 검색" value="${esc(app.pickerQuery)}" autocomplete="off"><div class="em-pick-list" data-em-pick-list>${rows}</div></section>`;
      host._filtered = list;
    }

    async function startPreview(target) {
      app.target = target;
      app.loaded = {};
      app.errors = {};
      app.attendance = [];
      app.section = "";
      setLoading(true, "직원 화면을 불러오는 중입니다.");
      renderPicker();
      renderPreviewBanner();
      await loadAll();
      app.ready = true;
      setLoading(false);
      renderSection();
      syncPortal();
    }

    function leavePreview() {
      try { storage?.removeItem(PREVIEW_RETURN_KEY); } catch (_) { /* 저장소 차단 */ }
      win.location.replace(`${PATHS.admin}${safeReturnHash(app.returnHash, "#employees")}`);
    }

    function guardFetchForPreview() {
      // 화면 밖 코드가 window.fetch 를 직접 불러도 미리보기 중에는 쓰기를 보내지 않는다(이중 잠금).
      if (!win.fetch || win.__emPreviewFetchGuard) return;
      const native = win.fetch.bind(win);
      win.__emPreviewFetchGuard = true;
      win.fetch = (input, init) => {
        try { assertWritable(init?.method || input?.method || "GET", app.mode === "preview"); } catch (error) { return Promise.reject(error); }
        return native(input, init);
      };
    }

    // ── 시작 ──
    async function logout() {
      if (app.mode !== "preview") {
        try { await fetchImpl(`${AUTH_BASE}/logout`, { method: "POST", credentials: "same-origin", headers: authToken() ? { Authorization: `Bearer ${authToken()}` } : {} }); } catch (_) { /* 토큰 삭제가 기준이다 */ }
      }
      goLogin();
    }

    async function switchTenant(tenantId) {
      let tried = "";
      try { tried = storage?.getItem(TENANT_SWITCH_KEY) || ""; } catch (_) { tried = ""; }
      if (tried === tenantId) return false;  // 같은 전환을 두 번 하지 않는다(왕복 방지)
      try { storage?.setItem(TENANT_SWITCH_KEY, tenantId); } catch (_) { /* 저장소 차단 */ }
      const result = await authApi(`/tenants/${encodeURIComponent(tenantId)}/switch`, { method: "POST" });
      if (!result?.token) return false;
      saveToken(result.token);
      win.location.reload();
      return true;
    }

    async function openContractLink() {
      const token = app.contractToken;
      if (!token || app.mode !== "employee") return;
      try {
        const payload = await financeApi(`/contracts/signing/${encodeURIComponent(token)}`);
        const contract = payload?.contract;
        if (!contract?.id) return;
        app.data.contracts = [contract, ...app.data.contracts.filter(item => item.id !== contract.id)];
        syncPortal();
        portalApi?.openContract(contract, payload.bundle || [], { token });
        win.history?.replaceState?.(null, "", PATHS.self);
        app.contractToken = "";
      } catch (error) {
        if (Number(error?.status) === 401) { showSessionAlert(); return; }
        const host = $("emAlert");
        host.hidden = false;
        const renew = Number(error?.status) === 410;
        host.innerHTML = `<b>서명 링크를 열 수 없습니다</b><span>${esc(error.message)}${renew ? ". 아래 버튼을 누르면 관리자에게 새 링크를 요청합니다." : ". 관리자에게 서명 링크를 다시 요청해 주세요."}</span><div class="em-actions">${renew ? '<button type="button" class="em-btn primary" data-em-renew>새 링크 요청</button>' : ""}<button type="button" class="em-btn" data-em-alert-close>닫기</button></div>`;
      }
    }

    async function renewSignLink(button) {
      button.disabled = true;
      try {
        const result = await financeApi("/contracts/signing-renewal", { method: "POST", body: JSON.stringify({ token: app.contractToken }) });
        $("emAlert").hidden = true;
        showToast(result?.notified ? "관리자에게 다시 요청했습니다. 새 서명 요청을 기다려 주세요." : "관리자에게 알림을 보내지 못했습니다. 직접 연락해 주세요.");
      } catch (error) {
        handleError(error, "서명 링크 재요청");
        button.disabled = false;
      }
    }

    async function start() {
      app.ready = false;
      $("emRecovery").hidden = true;
      $("emAlert").hidden = true;
      setLoading(true, "로그인 정보를 확인하는 중입니다.");
      if (!authToken()) {
        renderFatal({ kind: "session", title: "로그인이 필요합니다", message: "오비서 계정으로 로그인하면 내 계약서·급여명세·근태를 볼 수 있습니다.", actions: ["login"] });
        return;
      }
      let session;
      try {
        session = await financeApi("/session");
      } catch (error) {
        renderFatal(recoveryFor(error, "로그인 정보"));
        return;
      }
      app.session = session;
      const entry = decideEntry(session, { preview: params.get("preview") === "1" });
      if (entry === "forbidden_preview") {
        renderFatal({ kind: "forbidden", title: "직원 화면 보기 권한이 없습니다", message: "회사 관리자(직원 관리 권한)만 직원 화면을 미리볼 수 있습니다. 관리자 계정으로 다시 로그인해 주세요.", actions: ["admin", "login"] });
        return;
      }
      if (entry === "switch_tenant") {
        try { if (await switchTenant(clean(session.entry_tenant_redirect.tenant_id))) return; } catch (_) { /* 전환 실패 시 아래 판정 */ }
        if (!isEmployeeSession(session)) { win.location.replace(PATHS.admin); return; }
      } else if (entry === "admin") {
        win.location.replace(PATHS.admin);
        return;
      }
      try { storage?.removeItem(TENANT_SWITCH_KEY); } catch (_) { /* 저장소 차단 */ }
      if (entry === "preview") {
        app.mode = "preview";
        guardFetchForPreview();
        const request = consumePreviewRequest(storage);
        let savedReturn = "";
        try { savedReturn = JSON.parse(storage?.getItem(PREVIEW_RETURN_KEY) || "null")?.returnHash || ""; } catch (_) { savedReturn = ""; }
        app.returnHash = safeReturnHash(request?.returnHash || savedReturn, "#employees");
        try { storage?.setItem(PREVIEW_RETURN_KEY, JSON.stringify({ returnTo: "v41", returnHash: app.returnHash })); } catch (_) { /* 메모리 값만 쓴다 */ }
        try {
          await loadJoinRequests();
        } catch (error) {
          renderFatal(recoveryFor(error, "직원 목록"));
          return;
        }
        app.candidates = previewCandidates(app.data.joinRequests);
        renderPreviewBanner();
        let target = null;
        if (request?.open === "employee") {
          target = app.candidates.find(item => (request.target.requestId && item.requestId === request.target.requestId) || (!request.target.requestId && request.target.email && item.email === request.target.email))
            || (request.target.email ? { email: request.target.email, name: request.target.name, requestId: request.target.requestId, branch: "" } : null);
        }
        setLoading(false);
        if (target) await startPreview(target);
        else renderPicker();
        return;
      }
      app.mode = "employee";
      setLoading(true, "내 자료를 불러오는 중입니다.");
      await loadAll();
      app.ready = true;
      setLoading(false);
      syncPortal();
      renderSection();
      await openContractLink();
    }

    // ── 이벤트 ──
    function bind() {
      doc.addEventListener("click", event => {
        const target = event.target?.closest?.("[data-em-recover],[data-em-reload],[data-em-payroll-pdf],[data-em-doc-view],[data-em-doc-start],[data-em-month],[data-em-day],[data-em-pick],[data-em-preview-exit],[data-em-preview-switch],[data-em-renew],[data-em-alert-close]");
        if (!target || target.disabled) return;
        const data = target.dataset;
        if (data.emRecover === "login") goLogin();
        else if (data.emRecover === "retry") { $("emAlert").hidden = true; if (app.ready) { loadAll().then(() => { renderSection(); syncPortal(); }); } else start(); }
        else if (data.emRecover === "admin") win.location.replace(PATHS.admin);
        else if (data.emRecover === "close") $("emAlert").hidden = true;
        else if (data.emReload) reload(data.emReload);
        else if (data.emPayrollPdf) downloadFile(`${FINANCE_BASE}/payroll/${encodeURIComponent(data.emPayrollPdf)}/pdf`, `${data.emName || "급여명세"}.pdf`, target);
        else if (data.emDocView) downloadFile(`${FINANCE_BASE}/onboarding/documents/${encodeURIComponent(data.emDocView)}/download`, data.emName || "입사서류", target);
        else if (data.emDocStart) {
          app.uploadType = data.emDocStart;
          renderSection();
          const form = doc.querySelector("[data-em-upload]");
          form?.scrollIntoView?.({ block: "start", behavior: "smooth" });
          form?.elements?.file?.focus?.();
        } else if (data.emMonth) {
          app.month = shiftMonth(app.month, Number(data.emMonth));
          app.selectedDay = "";
          app.loaded.attendance = false;
          reloadAttendance();
          renderSection();
        } else if (data.emDay) { app.selectedDay = data.emDay; renderSection(); }
        else if (data.emPick !== undefined) {
          const chosen = $("emPicker")._filtered?.[Number(data.emPick)];
          if (chosen) startPreview(chosen);
        } else if ("emPreviewExit" in data) leavePreview();
        else if ("emPreviewSwitch" in data) {
          app.target = null;
          app.ready = false;
          app.pickerQuery = "";
          syncPortal();
          renderSection();
          renderPreviewBanner();
          renderPicker();
        } else if ("emRenew" in data) renewSignLink(target);
        else if ("emAlertClose" in data) $("emAlert").hidden = true;
      });
      doc.addEventListener("input", event => {
        if (event.target?.matches?.("[data-em-pick-search]")) {
          app.pickerQuery = event.target.value;
          const host = $("emPicker");
          const list = filterCandidates(app.candidates, app.pickerQuery);
          host._filtered = list;
          const box = host.querySelector("[data-em-pick-list]");
          if (box) box.innerHTML = list.length ? list.map((item, i) => `<button type="button" class="em-pick" data-em-pick="${i}"><b>${esc(item.name || maskEmail(item.email))}</b><span>${esc([item.branch, maskEmail(item.email)].filter(Boolean).join(" · "))}</span></button>`).join("") : '<p class="em-note">검색 결과가 없습니다.</p>';
        }
      });
      doc.addEventListener("change", event => {
        const form = event.target?.closest?.("[data-em-upload]");
        if (!form) return;
        const type = form.elements.document_type.value;
        if (event.target.name === "document_type" || event.target.name === "issue_date") {
          const required = expiryRequired(type);
          form.elements.expires_at.required = required;
          const hint = form.querySelector("[data-em-expiry-hint]");
          if (hint) hint.textContent = required ? "(필수)" : "(선택)";
          if (!form.elements.expires_at.value) form.elements.expires_at.value = suggestExpiry(type, form.elements.issue_date.value);
        }
      });
      doc.addEventListener("submit", event => {
        const form = event.target?.closest?.("[data-em-upload]");
        if (!form) return;
        event.preventDefault();
        submitUpload(form);
      });
    }

    return { app, bridge, start, bind, financeApi, workspaceApi, openSection, renderSection };
  }

  const api = { core, createApp };
  root.obysEmployeeApp = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
