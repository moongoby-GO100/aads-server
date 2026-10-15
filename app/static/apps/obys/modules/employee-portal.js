/* 오비서 직원 포털 — mockup-employee-contract-v1.html 화면 1~5 (직원 홈 / 내 계약서 / 계약서 읽기 / 동의·서명 / 서명 완료).
 *
 * index.html 이 직원(또는 관리자 '직원 화면 보기')일 때 #employeePortal 에 그린다. 서버 API 는 새로 만들지 않는다:
 * 목록은 index.html 이 이미 불러온 계약(visibleContracts), 전문은 contractPreviewHtml, 서명은 POST /contracts/signing
 * (submitContractSignature 와 같은 페이로드), 서명본은 downloadSignedContractPdf 를 index.html 의 bridge 로 받아 쓴다.
 * 급여명세·근태·입사서류는 기존 직원 보기(setView)로 넘긴다.
 *
 * 화면 로직(정렬·필터·읽음·제출 조건)은 DOM 없이 돌도록 core 로 나누고, 그리기는 HTML 문자열로 만든다.
 */
(function (global) {
  "use strict";

  const TABS = [
    { key: "home", pc: "홈", m: "홈", icon: "⌂" },
    { key: "contracts", pc: "계약서", m: "계약서", icon: "✎" },
    { key: "payroll", pc: "급여명세", m: "급여", icon: "₩", view: "payroll" },
    { key: "attendance", pc: "근태", m: "근태", icon: "◷", view: "attendance" },
    { key: "onboarding", pc: "입사서류", m: "서류", icon: "▤", view: "onboarding" }
  ];
  const FILTERS = [
    { key: "all", pc: "전체", m: "전체", count: "pc" },
    { key: "pending", pc: "서명 대기", m: "서명 대기", count: "both" },
    { key: "signed", pc: "서명 완료", m: "완료", count: "pc" },
    { key: "old", pc: "이전 계약", m: "이전", count: "pc" }
  ];
  const CATEGORY_RANK = { pending: 0, signed: 1, old: 2 };
  const CONSENT_VERSION = "yeoljeong-contract-sign-v1";
  const RESUME_KEY = "obys.employeePortal.resume";
  const RESUME_TTL_MS = 30 * 60 * 1000;
  const TODO_TITLE = "근로계약서 서명이 필요합니다";
  const DONE_TITLE = "서명이 완료됐습니다";
  const PREVIEW_SUBMIT_BLOCKED = "읽기 전용 미리보기에서는 서명을 제출할 수 없습니다.";
  const EDIT_REQUEST_UNAVAILABLE = "수정 요청 기능은 아직 없습니다. 내용이 다르면 관리자에게 직접 연락해 주십시오.";
  const EMPTY_CONTRACTS = "아직 받은 계약서가 없습니다. 관리자가 서명을 요청하면 알림이 옵니다.";
  const SESSION_EXPIRED = "로그인이 만료되었습니다. 다시 로그인해 주십시오. 로그인하면 보던 계약서로 돌아옵니다. 서명은 아직 제출되지 않았습니다.";

  // ── core: DOM 없이 계산만 한다 ─────────────────────────────────────────────

  function val(contract, camel, snake) {
    const value = contract?.[camel] ?? (snake ? contract?.[snake] : undefined);
    return value === undefined || value === null ? "" : value;
  }

  function contractCategory(contract) {
    const status = String(contract?.status || "");
    if (val(contract, "supersededBy", "superseded_by") || status === "archived" || status === "cancelled") return "old";
    if (status === "requested") return "pending";
    if (status === "signed") return "signed";
    return "";  // 작성중(draft)은 아직 직원에게 보낸 계약이 아니다
  }

  function contractSortTime(contract) {
    return String(
      val(contract, "requestedAt", "requested_at") || val(contract, "signedAt", "signed_at")
      || val(contract, "updatedAt", "updated_at") || val(contract, "createdAt", "created_at") || ""
    );
  }

  // 서명 대기 → 서명 완료 → 이전 계약. 같은 묶음 안에서는 최근 것이 위.
  function sortContracts(contracts) {
    return (Array.isArray(contracts) ? contracts : [])
      .filter(contract => contractCategory(contract))
      .slice()
      .sort((a, b) => {
        const rank = CATEGORY_RANK[contractCategory(a)] - CATEGORY_RANK[contractCategory(b)];
        return rank || contractSortTime(b).localeCompare(contractSortTime(a));
      });
  }

  function filterCounts(contracts) {
    const rows = sortContracts(contracts);
    const counts = { all: rows.length, pending: 0, signed: 0, old: 0 };
    rows.forEach(contract => { counts[contractCategory(contract)] += 1; });
    return counts;
  }

  function filterContracts(contracts, filter) {
    const rows = sortContracts(contracts);
    return !filter || filter === "all" ? rows : rows.filter(contract => contractCategory(contract) === filter);
  }

  function documentKind(contract) {
    const type = String(val(contract, "contractType", "contract_type"));
    return type === "freelancer" ? "용역계약서" : type === "confidentiality" ? "서약서" : "근로계약서";
  }

  function contractTitle(contract, labels = {}) {
    const type = String(val(contract, "contractType", "contract_type"));
    return String(val(contract, "printTitle", "print_title") || labels[type] || val(contract, "contractLabel", "contract_label") || documentKind(contract));
  }

  function shortTitle(title) {
    return String(title || "").replace(/\s*\([^)]*\)\s*$/, "") || String(title || "");
  }

  function won(value) {
    return `${Math.round(Number(value) || 0).toLocaleString("ko-KR")}원`;
  }

  function wageText(contract) {
    const wage = Number(val(contract, "wage") || 0);
    if (!wage) return "";
    const type = String(val(contract, "wageType", "wage_type") || "hourly");
    const prefix = { monthly: "월", hourly: "시급", daily: "일급", case_fee: "건별" }[type] || "";
    return `${prefix ? `${prefix} ` : ""}${won(wage)}`;
  }

  function dateOnly(value) {
    return String(value || "").slice(0, 10);
  }

  function monthDay(value) {
    const day = dateOnly(value);
    return /^\d{4}-\d{2}-\d{2}$/.test(day) ? day.slice(5) : day;
  }

  function kstDateTime(value) {
    const time = new Date(value || Date.now());
    if (Number.isNaN(time.getTime())) return String(value || "");
    const parts = {};
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    }).formatToParts(time).forEach(part => { parts[part.type] = part.value; });
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  }

  function payDateText(contract) {
    const raw = String(val(contract, "payDate", "pay_date") || "").trim();
    if (!raw) return "";
    return /^\d{1,2}$/.test(raw) ? `매월 ${raw}일` : raw;
  }

  // 3. 계약서 읽기의 핵심 조건 요약(시작일·근무지·근무·휴게·휴일·임금·지급일).
  function contractSummary(contract) {
    const start = dateOnly(val(contract, "startDate", "start_date"));
    const end = dateOnly(val(contract, "endDate", "end_date"));
    const workDays = String(val(contract, "workDays", "work_days") || "").trim();
    const workTime = String(val(contract, "workTime", "work_time") || "").trim();
    return {
      start: start ? `${start}${end ? ` ~ ${end}` : " (기간 없음)"}` : "",
      startShort: monthDay(start),
      workplace: String(val(contract, "branch") || val(contract, "workplace") || "").trim(),
      work: [workDays, workTime].filter(Boolean).join(" "),
      workDays,
      workTime,
      rest: String(val(contract, "restTime", "rest_time") || "").trim(),
      holidays: String(val(contract, "holidays") || "").trim(),
      wage: wageText(contract),
      payDate: payDateText(contract)
    };
  }

  // 읽은 정도(0~100). 스크롤이 필요 없을 만큼 짧으면 다 읽은 것으로 본다.
  function readPercent(metrics) {
    const scrollHeight = Number(metrics?.scrollHeight) || 0;
    const clientHeight = Number(metrics?.clientHeight) || 0;
    const scrollTop = Number(metrics?.scrollTop) || 0;
    if (scrollHeight <= clientHeight + 4) return 100;
    const pct = Math.round(((scrollTop + clientHeight) / scrollHeight) * 100);
    return pct >= 99 ? 100 : Math.max(0, Math.min(100, pct));
  }

  function canStartSigning(state) {
    return Boolean(state.contract && contractCategory(state.contract) === "pending" && state.read && !state.openError && !state.loading);
  }

  function canSubmit(state) {
    const sign = state.sign || {};
    return Boolean(
      !state.readOnly && canStartSigning(state) && String(sign.name || "").trim() && sign.ink && sign.dataUri
      && sign.consentContent && sign.consentEsign && !sign.submitting
    );
  }

  function signPayload(token, sign) {
    return {
      token,
      signer_name: String(sign.name || "").trim(),
      consent: true,
      consent_version: CONSENT_VERSION,
      signature_data_uri: sign.dataUri
    };
  }

  function emptySign(name) {
    return { name: String(name || ""), ink: false, dataUri: "", consentContent: false, consentEsign: false, submitting: false, error: "", failed: false };
  }

  // ── 화면 그리기: 상태 → HTML 문자열 ─────────────────────────────────────

  function esc(value) {
    return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function pcm(pcText, mText) {
    if (pcText === mText) return esc(pcText);
    return `<span class="ep-pc-only">${esc(pcText)}</span><span class="ep-m-only">${esc(mText)}</span>`;
  }

  function badgeHtml(category) {
    const label = { pending: ["req", "서명 대기"], signed: ["ok", "서명 완료"], old: ["old", "이전 계약"] }[category];
    return label ? `<span class="ep-badge ${label[0]}">${label[1]}</span>` : "";
  }

  function dotHtml(count) {
    return count ? `<span class="ep-dot">${esc(count)}</span>` : "";
  }

  function navTab(screen) {
    if (screen === "home") return "home";
    if (["list", "read", "sign", "done"].includes(screen)) return "contracts";
    return "";
  }

  function pcNavHtml(model) {
    const active = model.screen === "legacy" ? model.legacyTab : navTab(model.screen);
    return `<nav class="ep-pc-nav" aria-label="직원 메뉴">${TABS.map(tab => `<button type="button" data-ep-nav="${tab.key}" class="${tab.key === active ? "on" : ""}"${tab.key === active ? ' aria-current="page"' : ""}>${esc(tab.pc)}${tab.key === "contracts" ? dotHtml(model.counts.pending) : ""}</button>`).join("")}</nav>`;
  }

  function tabbarHtml(model) {
    const active = model.screen === "legacy" ? model.legacyTab : navTab(model.screen);
    return `<nav class="ep-tabbar" aria-label="직원 하단 메뉴">${TABS.map(tab => `<button type="button" data-ep-nav="${tab.key}" class="${tab.key === active ? "on" : ""}"${tab.key === active ? ' aria-current="page"' : ""}><i aria-hidden="true">${tab.icon}</i>${esc(tab.m)}${tab.key === "contracts" && model.screen !== "done" ? dotHtml(model.counts.pending) : ""}</button>`).join("")}</nav>`;
  }

  const LOGO = '<div class="ep-logo"><i>오</i>오비서</div>';

  function topHtml(model) {
    const name = `${model.employeeName} 님`;
    if (model.screen === "read") {
      return `<header class="ep-top">${LOGO}<button type="button" class="ep-btn ghost" data-ep-nav="contracts">← 내 계약서</button><div class="ep-who">${badgeHtml(contractCategory(model.contract))}<b>${esc(model.title)}</b></div></header>`;
    }
    if (model.screen === "sign") {
      return `<header class="ep-top">${LOGO}<button type="button" class="ep-btn ghost" data-ep-back-read>← 계약서로</button><div class="ep-who"><b>${esc(model.title)} · 서명</b></div></header>`;
    }
    if (model.screen === "done") {
      return `<header class="ep-top">${LOGO}<div class="ep-who"><b>${esc(name)}</b></div></header>`;
    }
    const who = model.screen === "home"
      ? `${model.branch ? `<span>${esc(model.branch)}</span>` : ""}<b>${esc(name)}</b><button type="button" class="ep-btn ghost" data-ep-logout>로그아웃</button>`
      : `<b>${esc(name)}</b>`;
    return `<header class="ep-top">${LOGO}${pcNavHtml(model)}<div class="ep-who">${who}</div></header>`;
  }

  function mobileHeadHtml(model) {
    if (model.screen === "home") return `<div class="ep-m-head"><h3>오비서</h3><span style="margin-left:auto;color:var(--ep-sub);font-size:13px">${esc(model.employeeName)} 님</span></div>`;
    if (model.screen === "list") return '<div class="ep-m-head"><h3>내 계약서</h3></div>';
    if (model.screen === "read") return `<div class="ep-m-head"><button type="button" class="ep-back" data-ep-nav="contracts" aria-label="내 계약서로">‹</button><h3>${esc(documentKind(model.contract))}</h3><span style="margin-left:auto">${badgeHtml(contractCategory(model.contract))}</span></div>`;
    if (model.screen === "sign") return `<div class="ep-m-head"><button type="button" class="ep-back" data-ep-back-read aria-label="계약서로">‹</button><h3>${esc(documentKind(model.contract))}</h3></div>`;
    if (model.screen === "done") return '<div class="ep-m-head"><h3>서명 완료</h3></div>';
    return "";
  }

  function homeHtml(model) {
    const pending = model.sorted.filter(contract => contractCategory(contract) === "pending");
    const first = pending[0];
    let todo = "";
    if (first) {
      const title = contractTitle(first, model.labels);
      const kind = documentKind(first);
      const requested = dateOnly(val(first, "requestedAt", "requested_at"));
      const heading = TODO_TITLE.replace("근로계약서", kind);
      todo = `<div class="ep-card ep-todo"><div class="ep-todo-main"><div class="ep-ic">✍</div><div><b>${pcm(heading, `${kind} 서명 필요`)}</b><p>${pcm(`${requested ? `${requested}에 요청됨 · ` : ""}${title}`, `${requested ? `${monthDay(requested)} 요청 · ` : ""}${shortTitle(title)}`)}</p></div></div><button type="button" class="ep-btn primary lg" data-ep-open="${esc(first.id)}" data-preview-allow>계약서 읽고 서명하기</button></div>`;
    }
    const tiles = model.tiles;
    const sub = [model.branch, "직원"].filter(Boolean).join(" · ");
    return `<div class="ep-body"><div class="ep-hello"><h3>안녕하세요, ${esc(model.employeeName)} 님</h3><p>${esc(sub)}</p></div>${todo}
      <div class="ep-grid3">
        <div class="ep-card ep-tile ep-pc-only"><small>계약서</small><b>서명 대기 ${model.counts.pending}건</b><span>서명 완료 ${model.counts.signed}건</span></div>
        <div class="ep-card ep-tile" role="button" tabindex="0" data-ep-nav="payroll"><small>${pcm("이번 달 급여명세", "급여명세")}</small><b>${pcm(tiles.payroll.pc, tiles.payroll.m)}</b><span class="ep-pc-only">${esc(tiles.payroll.hint)}</span></div>
        <div class="ep-card ep-tile" role="button" tabindex="0" data-ep-nav="onboarding"><small>입사서류</small><b>${pcm(tiles.onboarding.pc, tiles.onboarding.m)}</b><span class="ep-pc-only">${esc(tiles.onboarding.hint)}</span></div>
      </div></div>`;
  }

  function rowHtml(contract, model) {
    const category = contractCategory(contract);
    const title = contractTitle(contract, model.labels);
    const sum = contractSummary(contract);
    const id = esc(contract.id);
    const requested = val(contract, "requestedAt", "requested_at");
    const due = model.dueById[contract.id] || "";
    const viewBtn = `<button type="button" class="ep-btn" data-ep-open="${id}">내용 보기</button>`;
    const pdfBtn = String(contract.status) === "signed" ? `<button type="button" class="ep-btn" data-ep-pdf="${id}">${pcm("PDF 받기", "PDF")}</button>` : "";
    if (category === "pending") {
      const notePc = [requested ? `${monthDay(requested)} 요청` : "", due ? `${monthDay(due)}까지` : ""].filter(Boolean).join(" · ");
      const noteM = due ? `${monthDay(due)}까지` : (requested ? `${monthDay(requested)} 요청` : "");
      const descPc = [sum.workplace, sum.start ? `${dateOnly(val(contract, "startDate", "start_date"))}부터` : "", sum.wage, sum.workDays].filter(Boolean).join(" · ");
      const descM = [sum.startShort ? `${sum.startShort}부터` : "", sum.wage].filter(Boolean).join(" · ");
      return `<div class="ep-crow req"><div><div class="ep-meta">${badgeHtml(category)}<span class="ep-note">${pcm(notePc, noteM)}</span></div><h4>${pcm(title, shortTitle(title))}</h4><p>${pcm(descPc, descM)}</p></div><button type="button" class="ep-btn primary" data-ep-open="${id}" data-preview-allow>읽고 서명하기</button></div>`;
    }
    if (category === "signed") {
      const signed = dateOnly(val(contract, "signedAt", "signed_at"));
      return `<div class="ep-crow"><div><div class="ep-meta">${badgeHtml(category)}<span class="ep-note ep-pc-only">${esc(signed ? `${signed} 서명` : "")}</span></div><h4>${pcm(title, shortTitle(title))}</h4><p>${pcm([sum.workplace, sum.wage].filter(Boolean).join(" · "), signed ? `${signed} 서명` : sum.workplace)}</p></div><div class="ep-crow-actions">${viewBtn}${pdfBtn}</div></div>`;
    }
    const start = dateOnly(val(contract, "startDate", "start_date"));
    const end = dateOnly(val(contract, "endDate", "end_date"));
    const period = start || end ? `${start}${end ? ` ~ ${end}` : " ~"}` : "";
    const replaced = val(contract, "supersededBy", "superseded_by") ? "변경 계약으로 대체" : (String(contract.status) === "cancelled" ? "취소된 계약" : "보관된 계약");
    return `<div class="ep-crow"><div><div class="ep-meta">${badgeHtml(category)}<span class="ep-note ep-pc-only">${esc([period, replaced].filter(Boolean).join(" · "))}</span></div><h4>${pcm(title, shortTitle(title))}</h4><p>${pcm([sum.workplace, sum.wage].filter(Boolean).join(" · "), end ? `~ ${end}` : period)}</p></div><div class="ep-crow-actions single">${viewBtn}<span class="ep-pc-only">${pdfBtn}</span></div></div>`;
  }

  function listHtml(model) {
    const counts = model.counts;
    const seg = FILTERS.map(item => {
      const count = counts[item.key];
      const pcLabel = `${item.pc} ${count}`;
      const mLabel = item.count === "both" ? `${item.m} ${count}` : item.m;
      return `<button type="button" data-ep-filter="${item.key}" data-preview-allow class="${model.filter === item.key ? "on" : ""}" aria-pressed="${model.filter === item.key}">${pcm(pcLabel, mLabel)}</button>`;
    }).join("");
    const rows = filterContracts(model.contracts, model.filter);
    let body;
    if (!counts.all) {
      body = `<div class="ep-card ep-empty"><p>${esc(EMPTY_CONTRACTS)}</p><button type="button" class="ep-btn" data-ep-refresh>새로고침</button></div>`;
    } else if (!rows.length) {
      body = '<div class="ep-card ep-empty"><p>이 조건에 맞는 계약서가 없습니다.</p></div>';
    } else {
      body = `<div class="ep-card">${rows.map(contract => rowHtml(contract, model)).join("")}</div>`;
    }
    return `<div class="ep-body"><div class="ep-list-head"><h3>내 계약서</h3><div class="ep-seg" role="group" aria-label="계약서 필터">${seg}</div></div>${body}
      <p class="ep-note ep-pc-only" style="margin-top:12px">계약서가 안 보이면: 가입한 이메일과 관리자가 계약서에 적은 이메일이 다를 수 있습니다. <button type="button" class="ep-link" disabled title="${esc(EDIT_REQUEST_UNAVAILABLE)}">관리자에게 확인 요청</button></p></div>`;
  }

  function progressHtml(model) {
    return `<div class="ep-read-hint"><span>읽은 정도</span><div class="ep-progress"><i data-ep-progress style="width:${model.readPct}%"></i></div><b data-ep-progress-text>${model.readPct}%</b></div>`;
  }

  function paperHtml(model, dim) {
    let inner;
    try {
      inner = model.previewHtml ? model.previewHtml(model.contract) : "";
    } catch (error) {
      inner = `<p>계약서 전문을 그리지 못했습니다: ${esc(error?.message || error)}</p>`;
    }
    return `<div class="ep-paper${dim ? " ep-dim" : ""}" data-ep-paper tabindex="0" aria-label="계약서 전문"><article class="contract-paper contract-a4-paper">${inner}</article><p class="ep-paper-end">— 계약서 끝 —</p></div>`;
  }

  function summaryHtml(sum) {
    const rows = [["시작일", sum.start], ["근무지", sum.workplace], ["근무", sum.work], ["휴게", sum.rest], ["휴일", sum.holidays], ["월 임금", sum.wage], ["지급일", sum.payDate]];
    return `<div class="ep-card ep-sum"><h4>핵심 조건 요약</h4><dl class="ep-kv">${rows.map(([key, value]) => `<dt>${key === "월 임금" ? "임금" : key}</dt><dd>${esc(value || "-")}</dd>`).join("")}</dl></div>`;
  }

  function chipsHtml(sum) {
    const chips = [["시작일", sum.startShort], ["임금", sum.wage], ["근무", sum.work], ["휴일", sum.holidays]];
    return `<div class="ep-sum-chips">${chips.map(([key, value]) => `<div><small>${key}</small><b>${esc(value || "-")}</b></div>`).join("")}</div>`;
  }

  function signButtonHtml(model, cls) {
    const disabled = !canStartSigning(model.state);
    return `<button type="button" class="ep-btn primary lg ${cls}" data-ep-go-sign data-preview-allow${disabled ? " disabled" : ""}>동의하고 서명하기</button>`;
  }

  function readHtml(model) {
    const category = contractCategory(model.contract);
    const sum = contractSummary(model.contract);
    const state = model.state;
    const status = state.loading
      ? '<p class="ep-note">계약서를 불러오는 중입니다.</p>'
      : (state.openError ? `<p class="ep-error" role="alert">${esc(state.openError)}</p>` : "");
    let side;
    let sticky = "";
    if (category === "pending") {
      side = `<div class="ep-card ep-sum">${progressHtml(model)}<p class="ep-note">끝까지 읽어야 서명할 수 있습니다.</p>${status}${signButtonHtml(model, "block")}<p class="ep-note" style="margin-top:8px">내용이 다르면 <button type="button" class="ep-link" data-ep-edit-request disabled title="${esc(EDIT_REQUEST_UNAVAILABLE)}">관리자에게 수정 요청</button></p><p class="ep-note">${esc(EDIT_REQUEST_UNAVAILABLE)}</p></div>`;
      sticky = `<div class="ep-sticky">${progressHtml(model)}${status}${signButtonHtml(model, "block")}</div>`;
    } else if (category === "signed") {
      const signed = val(model.contract, "signedAt", "signed_at");
      side = `<div class="ep-card ep-sum"><p class="ep-note">${esc(signed ? `${kstDateTime(signed)} 서명 완료` : "서명 완료")}</p><button type="button" class="ep-btn primary lg block" style="margin-top:10px" data-ep-pdf="${esc(model.contract.id)}">서명본 PDF 받기</button></div>`;
      sticky = `<div class="ep-sticky"><button type="button" class="ep-btn primary lg block" data-ep-pdf="${esc(model.contract.id)}">서명본 PDF 받기</button></div>`;
    } else {
      side = '<div class="ep-card ep-sum"><p class="ep-note">이전 계약입니다. 지금 적용되는 계약은 목록 맨 위에 있습니다.</p></div>';
    }
    return `<div class="ep-body ep-tight">${chipsHtml(sum)}<div class="ep-read">${paperHtml(model, false)}<div class="ep-side-sum">${summaryHtml(sum)}${side}</div></div></div>${sticky}`;
  }

  function signHtml(model) {
    const sign = model.state.sign;
    const readOnly = model.state.readOnly;
    const submitDisabled = !canSubmit(model.state);
    const error = sign.error ? `<p class="ep-error" role="alert" data-ep-sign-error>${esc(sign.error)}</p>` : '<p class="ep-error hidden" role="alert" data-ep-sign-error hidden></p>';
    const preview = readOnly ? `<p class="ep-note" style="margin-top:8px">${esc(PREVIEW_SUBMIT_BLOCKED)}</p>` : "";
    return `<div class="ep-body ep-tight"><div class="ep-read">${paperHtml(model, true)}
      <div class="ep-dim-layer" data-ep-back-read aria-hidden="true"></div>
      <div class="ep-side-sum ep-sign-col"><div class="ep-card ep-sum ep-sign-card" role="dialog" aria-label="서명하기">
        <div class="ep-grab ep-m-only" aria-hidden="true"></div>
        <h4>${pcm("서명", "서명하기")}</h4>
        <label class="ep-label" for="epSignerName">이름</label>
        <input id="epSignerName" class="ep-input" data-ep-name autocomplete="name" value="${esc(sign.name)}">
        <div class="ep-label" style="margin-top:12px">손글씨 서명</div>
        <div class="ep-pad${sign.ink ? " has-ink" : ""}" data-ep-pad-wrap><canvas data-ep-pad aria-label="손글씨 서명 칸"></canvas><span>${pcm("여기에 서명하세요", "손가락으로 서명하세요")}</span></div>
        <div class="ep-pad-tools"><button type="button" class="ep-btn ghost" data-ep-clear>지우기</button></div>
        <label class="ep-check" style="margin-top:6px"><input type="checkbox" data-ep-consent="content"${sign.consentContent ? " checked" : ""}> ${pcm("계약서 내용을 모두 읽었고, 내용에 동의합니다.", "내용을 모두 읽었고 동의합니다.")}</label>
        <label class="ep-check"><input type="checkbox" data-ep-consent="esign"${sign.consentEsign ? " checked" : ""}> ${pcm("전자서명이 손으로 한 서명과 같은 효력이 있음을 이해합니다.", "전자서명의 효력을 이해합니다.")}</label>
        ${error}
        <button type="button" class="ep-btn primary lg block" style="margin-top:14px" data-ep-submit${submitDisabled ? " disabled" : ""}>${sign.submitting ? "제출 중…" : (sign.failed ? "다시 제출" : "서명 제출")}</button>
        <p class="ep-note" style="margin-top:8px">제출하면 고칠 수 없습니다. 서명본 PDF가 바로 만들어집니다.</p>${preview}
      </div></div></div></div>`;
  }

  function doneHtml(model) {
    const done = model.state.done || {};
    const contract = done.contract || {};
    const title = contractTitle(contract, model.labels);
    const at = kstDateTime(done.at);
    const notified = done.notified ? " · 관리자에게도 알렸습니다" : "";
    const pdfNote = done.pdfStored === false ? '<p class="ep-note">서명본 PDF 준비가 늦어지고 있습니다. 잠시 뒤 다시 받아 주십시오.</p>' : "";
    return `<div class="ep-body"><div class="ep-card ep-done"><div class="ep-big">✓</div><h3>${DONE_TITLE}</h3><p class="ep-note">${pcm(`${at} · ${title}${notified}`, `${at.slice(5)} · ${shortTitle(title)}`)}</p>${pdfNote}
      <div class="ep-done-actions"><button type="button" class="ep-btn primary" data-ep-pdf="${esc(contract.id || "")}">서명본 PDF 받기</button><button type="button" class="ep-btn" data-ep-nav="contracts">내 계약서 목록</button><button type="button" class="ep-btn ep-pc-only" data-ep-nav="home">홈으로</button></div></div></div>`;
  }

  function screenHtml(model) {
    const tabbar = ["home", "list", "done", "legacy"].includes(model.screen) ? tabbarHtml(model) : "";
    let body = "";
    if (model.screen === "home") body = homeHtml(model);
    else if (model.screen === "list") body = listHtml(model);
    else if (model.screen === "read") body = readHtml(model);
    else if (model.screen === "sign") body = signHtml(model);
    else if (model.screen === "done") body = doneHtml(model);
    return `${topHtml(model)}${mobileHeadHtml(model)}${body}${tabbar}`;
  }

  // ── 홈 요약 칸 ───────────────────────────────────────────────────────────

  function payrollTile(payroll, today, contracts) {
    const month = String(today || "").slice(0, 7);
    const issued = (payroll || []).find(item => String(item.period || "").startsWith(month) && String(item.status || "") !== "draft");
    const current = sortContracts(contracts).find(contract => contractCategory(contract) !== "old");
    const payDay = String(val(current, "payDate", "pay_date") || "").trim();
    const hint = payDay ? (/^\d{1,2}$/.test(payDay) ? `급여일 ${payDay}일` : `급여일 ${payDay}`) : "";
    if (issued) {
      return { pc: "발행됨", m: "발행됨", hint: `실지급 ${won(issued.net_pay || issued.netPay || 0)}` };
    }
    return { pc: "아직 발행 전", m: "발행 전", hint };
  }

  function onboardingTile(docs, requiredTypes) {
    const required = Array.isArray(requiredTypes) ? requiredTypes : [];
    if (!required.length) return { pc: "제출할 서류 없음", m: "없음", hint: "" };
    const done = required.filter(meta => (docs || []).some(doc => doc.document_type === meta.type
      && !["missing", "rejected", "superseded"].includes(String(doc.status || "").toLowerCase()) && !doc.missing_document));
    const missing = required.filter(meta => !done.includes(meta));
    return {
      pc: `${done.length} / ${required.length} 제출`,
      m: `${done.length} / ${required.length}`,
      hint: missing.length ? `${missing[0].label || missing[0].type} 미제출${missing.length > 1 ? ` 외 ${missing.length - 1}건` : ""}` : "모두 제출"
    };
  }

  // ── 컨트롤러 ────────────────────────────────────────────────────────────

  // 저장소가 막힌 환경(사생활 보호 모드·샌드박스)에서는 sessionStorage 를 읽기만 해도 예외가 난다.
  function defaultStorage() {
    try {
      return typeof sessionStorage !== "undefined" ? sessionStorage : null;
    } catch (_) {
      return null;
    }
  }

  function createPortal(options = {}) {
    const storage = options.storage !== undefined ? options.storage : defaultStorage();
    const now = options.now || (() => Date.now());
    let bridge = null;
    let wired = null;
    const state = {
      owner: "", active: false, screen: "home", filter: "all", legacyTab: "", contract: null, bundle: [], token: "",
      readPct: 0, read: false, loading: false, openError: "", readOnly: false,
      sign: emptySign(""), done: null
    };

    function contracts() {
      return Array.isArray(bridge?.contracts) ? bridge.contracts : [];
    }

    function findContract(id) {
      return contracts().find(item => String(item.id) === String(id)) || null;
    }

    function dueById() {
      const map = {};
      (bridge?.pendingSign || []).forEach(item => { if (item?.id && item.expires_at) map[item.id] = item.expires_at; });
      return map;
    }

    function model() {
      const list = contracts();
      return {
        screen: state.screen,
        legacyTab: state.legacyTab,
        filter: state.filter,
        state,
        contract: state.contract,
        contracts: list,
        sorted: sortContracts(list),
        counts: filterCounts(list),
        labels: bridge?.labels?.contractType || {},
        title: state.contract ? contractTitle(state.contract, bridge?.labels?.contractType || {}) : "",
        employeeName: bridge?.employeeName || "직원",
        branch: bridge?.branch || String(sortContracts(list)[0]?.branch || ""),
        readPct: state.readPct,
        dueById: dueById(),
        previewHtml: bridge?.contractPreviewHtml,
        tiles: {
          payroll: payrollTile(bridge?.payroll, bridge?.today, list),
          onboarding: onboardingTile(bridge?.onboardingDocs, bridge?.requiredDocTypes)
        }
      };
    }

    function html() {
      return screenHtml(model());
    }

    function saveResume() {
      if (!storage || !state.contract?.id) return;
      try { storage.setItem(RESUME_KEY, JSON.stringify({ contractId: state.contract.id, at: now() })); } catch (_) { /* 저장소 차단 */ }
    }

    function dropResume() {
      if (!storage) return;
      try { storage.removeItem(RESUME_KEY); } catch (_) { /* 저장소 차단 */ }
    }

    function takeResume() {
      if (!storage) return "";
      let raw = null;
      try { raw = storage.getItem(RESUME_KEY); } catch (_) { return ""; }
      if (!raw) return "";
      let saved = null;
      try { saved = JSON.parse(raw); } catch (_) { saved = null; }
      const age = now() - Number(saved?.at);
      if (!saved?.contractId || !Number.isFinite(age) || age < 0 || age > RESUME_TTL_MS) {
        try { storage.removeItem(RESUME_KEY); } catch (_) { /* 저장소 차단 */ }
        return "";
      }
      if (!findContract(saved.contractId)) return "";  // 목록이 아직 오는 중 — 다음 sync 에서 다시 본다
      try { storage.removeItem(RESUME_KEY); } catch (_) { /* 저장소 차단 */ }
      return saved.contractId;
    }

    function reset(owner) {
      Object.assign(state, {
        owner, screen: "home", filter: "all", legacyTab: "", contract: null, bundle: [], token: "",
        readPct: 0, read: false, loading: false, openError: "", sign: emptySign(""), done: null
      });
    }

    function sync(next) {
      bridge = next || bridge;
      if (!bridge) return;
      const active = Boolean(bridge.active);
      state.readOnly = Boolean(bridge.readOnly);
      const owner = active ? `${state.readOnly ? "preview:" : ""}${bridge.employeeEmail || bridge.employeeName || ""}` : state.owner;
      const reactivated = active && !state.active;
      state.active = active;
      if (active && owner !== state.owner) reset(owner);
      if (active) {
        if (reactivated && state.contract && ["read", "sign"].includes(state.screen)) {
          // 같은 화면에서 다시 로그인했다 — 보던 계약서·입력한 서명이 그대로 있다.
          dropResume();
          state.openError = "";
          state.sign.error = "";
        }
        if (state.screen === "legacy" && bridge.currentView) {
          const tab = TABS.find(item => item.view === bridge.currentView);
          if (tab) {
            state.legacyTab = tab.key;
          } else {
            // 기존 화면 안의 링크가 계약서 보기로 보냈다 — 포털 목록으로 받는다.
            state.screen = "list";
            state.legacyTab = "";
          }
        }
        const resumeId = !state.readOnly && (state.screen === "home" || state.screen === "list") ? takeResume() : "";
        if (resumeId) {
          openContract(findContract(resumeId));
          return;
        }
      }
      render(active);
    }

    function setScreen(screen) {
      state.screen = screen;
      if (screen !== "legacy") state.legacyTab = "";
      render(true);
      scrollTop();
    }

    function nav(key) {
      const tab = TABS.find(item => item.key === key);
      if (!tab) return;
      if (tab.view) {
        state.screen = "legacy";
        state.legacyTab = tab.key;
        render(true);
        bridge?.setView?.(tab.view);
        scrollTop();
        return;
      }
      setScreen(key === "contracts" ? "list" : "home");
    }

    function setFilter(filter) {
      if (!FILTERS.some(item => item.key === filter)) return;
      state.filter = filter;
      render(true);
    }

    // options.token: 서명 링크(yf_contract_token)로 연 경우 그 토큰. 목록에서 연 계약은 자기 sign_token 이 없으면
    // 기존 경로(/contracts/{id}/signing-view)로 받아 온다 — 다른 계약의 링크 토큰을 빌려 쓰지 않는다.
    function openContract(contract, bundle = [], options = {}) {
      if (!contract) return false;
      state.contract = contract;
      state.token = String(options.token || contract.sign_token || "");
      state.bundle = Array.isArray(bundle) ? bundle : [];
      state.readPct = 0;
      state.read = false;
      state.openError = "";
      state.loading = false;
      state.sign = emptySign(val(contract, "signerName", "signer_name") || val(contract, "employeeName", "employee_name") || bridge?.employeeName || "");
      state.screen = "read";
      const needsToken = contractCategory(contract) === "pending" && !state.token && !state.readOnly && bridge?.financeApi;
      if (needsToken) state.loading = true;
      render(true);
      scrollTop();
      if (needsToken) loadSigningView(contract.id);
      return true;
    }

    async function loadSigningView(contractId) {
      try {
        const payload = await bridge.financeApi(`/contracts/${encodeURIComponent(contractId)}/signing-view`);
        if (state.contract?.id !== contractId) return;
        if (payload?.contract) state.contract = payload.contract;
        state.token = String(payload?.contract?.sign_token || state.token || "");
        state.bundle = payload?.bundle || [];
        if (!state.token) state.openError = "서명 토큰이 없습니다. 관리자에게 서명 링크를 다시 요청하십시오.";
      } catch (error) {
        if (state.contract?.id !== contractId) return;
        if (Number(error?.status) === 401) {
          saveResume();
          state.openError = SESSION_EXPIRED;
          bridge?.handleExpiredServerSession?.();
        } else {
          state.openError = `이 계약서는 지금 서명할 수 없습니다: ${error?.message || error}`;
        }
      } finally {
        if (state.contract?.id === contractId) {
          state.loading = false;
          if (state.screen === "read") render(true);
        }
      }
    }

    function setReadProgress(pct) {
      const value = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
      if (value <= state.readPct) return;
      state.readPct = value;
      if (value >= 100) state.read = true;
      syncControls();
    }

    function goSign() {
      if (!canStartSigning(state)) return false;
      state.screen = "sign";
      render(true);
      return true;
    }

    function backToRead() {
      if (!state.contract) return;
      state.screen = "read";
      render(true);
    }

    function setName(name) {
      state.sign.name = String(name || "");
      syncControls();
    }

    function setConsent(which, checked) {
      if (which === "content") state.sign.consentContent = Boolean(checked);
      if (which === "esign") state.sign.consentEsign = Boolean(checked);
      syncControls();
    }

    function setInk(dataUri) {
      state.sign.ink = Boolean(dataUri);
      state.sign.dataUri = String(dataUri || "");
      syncControls();
    }

    function clearInk() {
      setInk("");
    }

    async function submit() {
      const sign = state.sign;
      if (state.readOnly) {
        sign.error = PREVIEW_SUBMIT_BLOCKED;
        render(true);
        return { ok: false, reason: "read_only" };
      }
      if (!canSubmit(state)) return { ok: false, reason: "incomplete" };
      const token = state.token || state.contract.sign_token || "";
      if (!token) {
        sign.error = "서명 토큰이 없습니다. 관리자에게 서명 링크를 다시 요청하십시오.";
        render(true);
        return { ok: false, reason: "no_token" };
      }
      sign.submitting = true;
      sign.error = "";
      syncControls();
      try {
        const payload = await bridge.financeApi("/contracts/signing", { method: "POST", body: JSON.stringify(signPayload(token, sign)) });
        const signed = payload?.contract || { ...state.contract, status: "signed" };
        bridge?.onSigned?.(payload || { contract: signed });
        const notifyStatus = String(payload?.notify?.status || "");
        state.done = {
          contract: signed,
          at: val(signed, "signedAt", "signed_at") || new Date(now()).toISOString(),
          pdfStored: payload?.signed_pdf ? payload.signed_pdf.status === "stored" : undefined,
          notified: Boolean(notifyStatus) && !["failed", "skipped", "none", "disabled"].includes(notifyStatus)
        };
        state.contract = signed;
        state.token = "";
        state.sign = emptySign(sign.name);
        state.screen = "done";
        render(true);
        scrollTop();
        return { ok: true };
      } catch (error) {
        // 서명·체크·이름은 그대로 둔다 — 다시 제출만 누르면 된다.
        sign.submitting = false;
        sign.failed = true;
        if (Number(error?.status) === 401) {
          saveResume();
          sign.error = SESSION_EXPIRED;
          render(true);
          bridge?.handleExpiredServerSession?.();
          return { ok: false, reason: "expired" };
        }
        sign.error = `서명 제출에 실패했습니다: ${error?.message || error}. 입력한 서명은 그대로 두었습니다. 다시 제출해 주십시오.`;
        render(true);
        return { ok: false, reason: "failed" };
      } finally {
        sign.submitting = false;
      }
    }

    function downloadPdf(id, button) {
      if (!id || !bridge?.downloadPdf) return;
      Promise.resolve(bridge.downloadPdf(id, button)).catch(error => bridge?.showToast?.(`서명본 PDF를 받지 못했습니다: ${error?.message || error}`));
    }

    // ── DOM ──
    function root() {
      return bridge?.root || null;
    }

    function scrollTop() {
      if (typeof window !== "undefined" && typeof window.scrollTo === "function") {
        try { window.scrollTo(0, 0); } catch (_) { /* jsdom 등 */ }
      }
    }

    function render(active) {
      const host = root();
      const doc = typeof document !== "undefined" ? document : null;
      const on = Boolean(active ?? bridge?.active);
      if (doc?.body?.classList) {
        doc.body.classList.toggle("ep-on", on);
        doc.body.classList.toggle("ep-legacy", on && state.screen === "legacy");
      }
      if (!host) return;
      host.classList?.toggle("hidden", !on);
      host.classList?.toggle("ep-no-tabbar", on && ["read", "sign"].includes(state.screen));
      if (!on) {
        host.innerHTML = "";
        return;
      }
      host.innerHTML = screenHtml(model());
      wire(host);
      if (state.screen === "read") measure();
      if (state.screen === "sign") setupPad(host);
    }

    function syncControls() {
      const host = root();
      if (!host?.querySelectorAll) return;
      host.querySelectorAll("[data-ep-progress]").forEach(el => { el.style.width = `${state.readPct}%`; });
      host.querySelectorAll("[data-ep-progress-text]").forEach(el => { el.textContent = `${state.readPct}%`; });
      host.querySelectorAll("[data-ep-go-sign]").forEach(el => { el.disabled = !canStartSigning(state); });
      host.querySelectorAll("[data-ep-submit]").forEach(el => {
        el.disabled = !canSubmit(state);
        el.textContent = state.sign.submitting ? "제출 중…" : (state.sign.failed ? "다시 제출" : "서명 제출");
      });
      host.querySelectorAll("[data-ep-consent]").forEach(el => {
        el.checked = el.dataset.epConsent === "content" ? state.sign.consentContent : state.sign.consentEsign;
      });
      host.querySelectorAll("[data-ep-pad-wrap]").forEach(el => el.classList.toggle("has-ink", state.sign.ink));
    }

    function measure() {
      const host = root();
      const paper = host?.querySelector?.("[data-ep-paper]");
      if (!paper) return;
      if (paper.scrollHeight > paper.clientHeight + 4) {
        setReadProgress(readPercent({ scrollTop: paper.scrollTop, scrollHeight: paper.scrollHeight, clientHeight: paper.clientHeight }));
        return;
      }
      // 모바일: 전문은 화면 전체와 함께 스크롤된다 — 전문 끝이 하단 고정 버튼 위로 올라왔는지 본다.
      const rect = paper.getBoundingClientRect();
      const sticky = host.querySelector(".ep-sticky");
      const stickyHeight = sticky && sticky.offsetParent !== null ? sticky.getBoundingClientRect().height : 0;
      const viewBottom = (window.innerHeight || 0) - stickyHeight;
      if (rect.height <= 0) return;
      setReadProgress(readPercent({ scrollTop: Math.max(0, -rect.top), scrollHeight: rect.height, clientHeight: Math.max(0, Math.min(rect.height, viewBottom - Math.max(0, rect.top))) }));
    }

    function setupPad(host) {
      const canvas = host.querySelector("[data-ep-pad]");
      if (!canvas?.getContext) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      let drawing = false;
      let drew = false;
      const fit = () => {
        const rect = canvas.getBoundingClientRect();
        const ratio = Math.max(1, window.devicePixelRatio || 1);
        canvas.width = Math.max(1, Math.round(rect.width * ratio));
        canvas.height = Math.max(1, Math.round(rect.height * ratio));
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.lineWidth = 2.4;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.strokeStyle = "#0f172a";
        if (state.sign.dataUri && typeof Image !== "undefined") {
          // 다시 그려진 화면(제출 실패·회전)에서도 입력한 서명을 그대로 보여 준다.
          const image = new Image();
          image.onload = () => ctx.drawImage(image, 0, 0, rect.width, rect.height);
          image.src = state.sign.dataUri;
        }
      };
      fit();
      const point = event => {
        const rect = canvas.getBoundingClientRect();
        return [event.clientX - rect.left, event.clientY - rect.top];
      };
      canvas.addEventListener("pointerdown", event => {
        if (state.sign.submitting) return;
        drawing = true;
        drew = false;
        canvas.setPointerCapture?.(event.pointerId);
        const [x, y] = point(event);
        ctx.beginPath();
        ctx.moveTo(x, y);
        event.preventDefault();
      });
      canvas.addEventListener("pointermove", event => {
        if (!drawing) return;
        const [x, y] = point(event);
        ctx.lineTo(x, y);
        ctx.stroke();
        drew = true;
        event.preventDefault();
      });
      const end = () => {
        if (!drawing) return;
        drawing = false;
        if (drew) setInk(canvas.toDataURL("image/png"));
      };
      canvas.addEventListener("pointerup", end);
      canvas.addEventListener("pointercancel", end);
      canvas.addEventListener("pointerleave", end);
      wired.fitPad = fit;
      wired.clearPad = () => {
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.restore();
      };
    }

    function wire(host) {
      if (wired?.host === host) {
        const paper = host.querySelector?.("[data-ep-paper]");
        paper?.addEventListener?.("scroll", measure, { passive: true });
        return;
      }
      wired = { host, fitPad: null, clearPad: null };
      host.addEventListener("click", event => {
        const target = event.target?.closest?.("[data-ep-nav],[data-ep-open],[data-ep-filter],[data-ep-go-sign],[data-ep-back-read],[data-ep-submit],[data-ep-clear],[data-ep-pdf],[data-ep-refresh],[data-ep-logout]");
        if (!target || target.disabled) return;
        const data = target.dataset || {};
        if (data.epNav) nav(data.epNav);
        else if (data.epOpen) openContract(findContract(data.epOpen));
        else if (data.epFilter) setFilter(data.epFilter);
        else if ("epGoSign" in data) goSign();
        else if ("epBackRead" in data) backToRead();
        else if ("epSubmit" in data) submit();
        else if ("epClear" in data) { wired.clearPad?.(); clearInk(); }
        else if (data.epPdf) downloadPdf(data.epPdf, target);
        else if ("epRefresh" in data) bridge?.reloadContracts?.();
        else if ("epLogout" in data) bridge?.logout?.();
      });
      host.addEventListener("keydown", event => {
        if ((event.key === "Enter" || event.key === " ") && event.target?.matches?.('[role="button"][data-ep-nav]')) {
          event.preventDefault();
          nav(event.target.dataset.epNav);
        }
      });
      host.addEventListener("change", event => {
        const consent = event.target?.dataset?.epConsent;
        if (consent) setConsent(consent, event.target.checked);
      });
      host.addEventListener("input", event => {
        if (event.target?.matches?.("[data-ep-name]")) setName(event.target.value);
      });
      if (typeof window !== "undefined" && window.addEventListener) {
        window.addEventListener("scroll", () => { if (state.screen === "read") measure(); }, { passive: true });
        window.addEventListener("resize", () => {
          if (state.screen === "read") measure();
          if (state.screen === "sign") wired.fitPad?.();
        });
      }
      host.querySelector?.("[data-ep-paper]")?.addEventListener?.("scroll", measure, { passive: true });
    }

    return {
      state, sync, nav, setFilter, openContract, setReadProgress, goSign, backToRead,
      setName, setConsent, setInk, clearInk, submit, html, model,
      isActive: () => Boolean(bridge?.active)
    };
  }

  const core = {
    TABS, FILTERS, CONSENT_VERSION, TODO_TITLE, DONE_TITLE, PREVIEW_SUBMIT_BLOCKED, EDIT_REQUEST_UNAVAILABLE, EMPTY_CONTRACTS,
    contractCategory, sortContracts, filterCounts, filterContracts, contractTitle, documentKind, contractSummary, wageText,
    readPercent, canStartSigning, canSubmit, signPayload, payrollTile, onboardingTile, screenHtml, kstDateTime
  };
  const portal = createPortal();
  global.obysEmployeePortal = {
    core,
    createPortal,
    sync: bridge => portal.sync(bridge),
    openContract: (contract, bundle, options) => portal.openContract(contract, bundle, options),
    isActive: () => portal.isActive(),
    instance: portal
  };
})(typeof window !== "undefined" ? window : globalThis);
