(function () {
  "use strict";

  const API = "/api/v1/obys-collections";
  const DAYS = 30;
  const STALE_HOURS = 48;
  const KIND_LABEL = { bank_transaction: "은행 거래", tax_invoice: "세금계산서", cash_receipt: "현금영수증", card_approval: "카드 승인", channel_revenue: "판매채널 매출" };
  const KIND_BAD = { failed: "수집 실패", needs_reauth: "재연결 필요", partial: "일부만 수신" };
  const ERROR_TEXT = {
    role_not_allowed: "이 계정에는 이 자료를 볼 권한이 없습니다.",
    tenant_not_in_collection_scope: "이 사업장은 아직 자료 수집 대상이 아닙니다.",
    business_not_found: "선택한 사업자를 찾을 수 없습니다.",
    invalid_period: "조회 기간을 확인해 주세요."
  };

  const ui = {
    glance: { loading: false, data: null, status: null, error: null, statusError: null, business: "" },
    delivery: { loading: false, data: null, error: null, business: "", from: "", to: "", tab: "own", clobe: null, clobeError: null, clobeLoading: false, businesses: [] }
  };

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }

  function readCookie(name) {
    return document.cookie.split(";").map(item => item.trim()).find(item => item.startsWith(`${name}=`))?.slice(name.length + 1) || "";
  }

  function token() {
    return localStorage.getItem("aads_token") || localStorage.getItem("fb_access_token") || readCookie("aads_token") || readCookie("fb_access_token");
  }

  async function request(path) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(`${API}${path}`, {
        headers: { "Content-Type": "application/json", ...(token() ? { Authorization: `Bearer ${token()}` } : {}) },
        signal: controller.signal
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error("http");
        error.status = response.status;
        error.code = typeof payload.detail === "string" ? payload.detail : "";
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.status) throw error;
      const wrapped = new Error("network");
      wrapped.network = true;
      throw wrapped;
    } finally {
      clearTimeout(timer);
    }
  }

  function errorText(error) {
    if (!error) return "";
    if (error.network) return "서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요.";
    if (error.status === 401) return "로그인이 만료되었습니다. 다시 로그인해 주세요.";
    return ERROR_TEXT[error.code] || "자료를 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.";
  }

  function won(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "-";
    return `${Math.round(number).toLocaleString("ko-KR")}원`;
  }

  function count(value) {
    return `${Number(value || 0).toLocaleString("ko-KR")}건`;
  }

  function dateText(value) {
    return String(value || "").slice(0, 10);
  }

  function shortDate(value) {
    const text = dateText(value);
    return text.length === 10 ? `${Number(text.slice(5, 7))}월 ${Number(text.slice(8, 10))}일` : "-";
  }

  function dateTimeText(value) {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return dateText(value);
    return d.toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function toIso(date) {
    const pad = n => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function defaultRange() {
    const end = new Date();
    const start = new Date(end.getTime() - (DAYS - 1) * 86400000);
    return { from: toIso(start), to: toIso(end) };
  }

  const LEGACY_URL = "/static/apps/obys/index.html?legacy=1&view=legacy";
  const V41_ROUTE = { sales: "sales", integrations: "integrations" };

  function isV41() {
    return Boolean(document.getElementById("nav") && document.getElementById("page") && document.getElementById("organizationSelect"));
  }

  function goView(view) {
    if (isV41()) {
      const item = V41_ROUTE[view] && document.querySelector(`#nav [data-route="${V41_ROUTE[view]}"]`);
      if (item) item.click();
      else window.location.href = `${LEGACY_URL}&goto=${encodeURIComponent(view)}`;
      return;
    }
    const tab = document.querySelector(`.side-nav .tab[data-view="${view}"]`) || document.querySelector(`.tab[data-view="${view}"]`);
    if (tab) tab.click();
  }

  function stateBox(kind, title, body, actions) {
    return `<div class="od-state ${esc(kind)}" role="${kind === "error" ? "alert" : "status"}"><strong>${esc(title)}</strong>${body ? `<span>${esc(body)}</span>` : ""}${actions || ""}</div>`;
  }

  // ── 오늘 한눈에 (첫 화면) ───────────────────────────────
  function glanceEl() {
    return document.getElementById("obysGlance");
  }

  function homeBusiness() {
    const legacy = document.getElementById("homeBusinessSelect")?.value;
    if (legacy) return legacy;
    return isV41() ? (localStorage.getItem("obys_v41_business_id") || "") : "";
  }

  function companiesFor(status, business) {
    const list = (status?.companies || []).filter(item => item.link_status === "linked" && item.business_id);
    return business ? list.filter(item => item.business_id === business) : list;
  }

  function collectionSummary(companies) {
    let last = "";
    const bad = [];
    let neverRun = true;
    companies.forEach(company => {
      Object.entries(company.kinds || {}).forEach(([kind, info]) => {
        if (info.last_success_at) { neverRun = false; if (info.last_success_at > last) last = info.last_success_at; }
        if (KIND_BAD[info.status]) bad.push({ kind, label: KIND_LABEL[kind] || "자료", reason: KIND_BAD[info.status] });
      });
    });
    const stale = Boolean(last) && (Date.now() - new Date(last).getTime()) > STALE_HOURS * 3600 * 1000;
    return { last, bad, stale, neverRun };
  }

  function glanceCards(data, status) {
    const delivery = data.delivery || {};
    const services = delivery.services || [];
    const staleServices = services.filter(item => item.stale);
    const card = data.card_sales;
    const settlement = data.settlement || {};
    const waiting = data.review_waiting || { total: 0, review_box: 0, reviewed: 0 };
    const collect = collectionSummary(companiesFor(status, ui.glance.business));

    const deliveryLines = services.length
      ? services.map(item => `<li><span>${esc(item.label)}</span><b>${esc(won(item.gross))}</b><small>${esc(count(item.orders))}${item.stale ? ` · ${esc(item.last_date ? `${shortDate(item.last_date)} 이후 수집 멈춤` : "수집 멈춤")}` : ""}</small></li>`).join("")
      : `<li class="od-none">최근 ${DAYS}일 배달앱 매출이 없습니다.</li>`;
    const settleLine = (label, bucket, tone) => `<li class="${tone}"><span>${esc(label)}</span><b>${esc(won(bucket?.amount))}</b><small>${esc(count(bucket?.count))}</small></li>`;
    const collectBody = collect.neverRun && !companiesFor(status, ui.glance.business).length
      ? `<li class="od-none">연결된 자료 수집 회사가 없습니다.</li>`
      : `<li><span>마지막 수집</span><b>${esc(collect.last ? dateTimeText(collect.last) : "수집 전")}</b>${collect.stale ? `<small class="warn">48시간 넘게 새 자료가 없습니다</small>` : ""}</li>${collect.bad.length ? collect.bad.map(item => `<li class="bad"><span>${esc(item.label)}</span><b>${esc(item.reason)}</b></li>`).join("") : `<li class="ok"><span>수집 오류</span><b>없음</b></li>`}`;

    return `<div class="od-grid">
      <article class="od-card"><h3>배달앱 매출 <small>최근 ${DAYS}일</small></h3>
        <p class="od-big">${esc(won(delivery.gross))}</p><p class="od-sub">${esc(count(delivery.orders))} · 자체 수집 기준</p>
        <ul class="od-lines">${deliveryLines}</ul>
        ${staleServices.length ? `<button type="button" data-od-action="open-delivery">수집 현황 보기</button>` : `<button type="button" data-od-action="open-delivery">배달앱 현황 보기</button>`}
      </article>
      <article class="od-card"><h3>카드·현금영수증 매출 <small>클로브 기준</small></h3>
        <ul class="od-lines">
          <li><span>카드 매출</span><b>${card ? esc(won(card.gross)) : "연결 필요"}</b>${card ? `<small>${esc(count(card.rows))} 집계</small>` : `<small>클로브에서 판매채널 연결이 필요합니다</small>`}</li>
          <li><span>현금영수증 매출</span><b>${esc(won(data.cash_receipt_sales?.amount))}</b><small>${esc(count(data.cash_receipt_sales?.count))}</small></li>
        </ul>
        <p class="od-sub">두 금액은 서로 겹칠 수 있어 합치지 않습니다.</p>
      </article>
      <article class="od-card"><h3>정산 <small>앞으로 14일 포함</small></h3>
        <ul class="od-lines">${settleLine("입금 예정", settlement.scheduled, "warn")}${settleLine("입금 완료", settlement.paid, "ok")}${Number(settlement.unknown?.count || 0) ? settleLine("상태 확인 필요", settlement.unknown, "") : ""}</ul>
      </article>
      <article class="od-card"><h3>자료 수집 현황</h3><ul class="od-lines">${collectBody}</ul>
        <button type="button" data-od-action="open-overview">자료 현황 열기</button>
      </article>
      <article class="od-card od-review ${waiting.total ? "has" : ""}"><h3>검토 대기</h3>
        <p class="od-big">${esc(count(waiting.total))}</p>
        <p class="od-sub">검토 전 ${esc(count(waiting.review_box))} · 확정 대기 ${esc(count(waiting.reviewed))}</p>
        <button type="button" class="primary" data-od-action="open-review">검토하러 가기</button>
      </article>
    </div>`;
  }

  function renderGlance() {
    const root = glanceEl();
    if (!root) return;
    const g = ui.glance;
    let body;
    if (!token()) body = stateBox("empty", "로그인 후 최근 자료를 보여드립니다", "");
    else if (g.loading && !g.data) body = stateBox("loading", "최근 자료를 불러오는 중입니다", "");
    else if (g.error) body = stateBox("error", errorText(g.error), "", `<button type="button" data-od-action="reload-glance">다시 불러오기</button>`);
    else if (!g.data) body = "";
    else if (!(g.data.businesses || []).length) body = stateBox("empty", "표시할 사업자가 없습니다", "사업자를 먼저 등록해 주세요.");
    else body = glanceCards(g.data, g.status) + (g.statusError ? `<p class="od-note">자료 수집 상태는 지금 불러오지 못했습니다.</p>` : "");
    const range = g.data?.period ? `${shortDate(g.data.period.date_from)} ~ ${shortDate(g.data.period.date_to)}` : "";
    root.innerHTML = `<div class="od-head"><h2>오늘 한눈에</h2><span class="od-range">${esc(range)}</span></div>${body}`;
  }

  async function loadGlance() {
    const g = ui.glance;
    if (!token() || g.loading) { renderGlance(); return; }
    g.loading = true;
    g.attempted = true;
    g.error = null;
    g.business = homeBusiness();
    renderGlance();
    const query = g.business ? `?business_id=${encodeURIComponent(g.business)}` : "";
    const [overview, status] = await Promise.allSettled([request(`/today-overview${query}`), request("/status")]);
    g.loading = false;
    if (overview.status === "fulfilled") { g.data = overview.value; g.error = null; } else { g.error = overview.reason; g.data = null; }
    if (status.status === "fulfilled") { g.status = status.value; g.statusError = null; } else { g.statusError = status.reason; }
    renderGlance();
  }

  // ── 배달앱 화면 ─────────────────────────────────────────
  function deliveryEl() {
    return document.getElementById("obysDeliveryPanel");
  }

  function rangeQuery(d) {
    const params = new URLSearchParams();
    if (d.from) params.set("date_from", d.from);
    if (d.to) params.set("date_to", d.to);
    const text = params.toString();
    return text ? `?${text}` : "";
  }

  async function loadBusinesses() {
    const d = ui.delivery;
    if (d.businesses.length) return;
    const data = await request("/today-overview");
    d.businesses = data.businesses || [];
    if (!d.business || !d.businesses.some(item => item.business_id === d.business)) d.business = homeBusiness() || d.businesses[0]?.business_id || "";
  }

  async function loadDelivery() {
    const d = ui.delivery;
    if (!token()) { renderDelivery(); return; }
    if (!d.from || !d.to) Object.assign(d, defaultRange());
    d.loading = true;
    d.error = null;
    renderDelivery();
    try {
      await loadBusinesses();
      if (!d.business) { d.data = null; } else {
        d.data = await request(`/businesses/${encodeURIComponent(d.business)}/delivery-overview${rangeQuery(d)}`);
      }
    } catch (error) {
      d.error = error;
      d.data = null;
    } finally {
      d.loading = false;
    }
    renderDelivery();
    if (d.tab === "clobe") loadClobeTab();
  }

  async function loadClobeTab() {
    const d = ui.delivery;
    if (!d.business || d.clobeLoading) return;
    d.clobeLoading = true;
    d.clobeError = null;
    renderDelivery();
    try {
      d.clobe = await request(`/businesses/${encodeURIComponent(d.business)}/channel-revenue${rangeQuery(d)}${rangeQuery(d) ? "&" : "?"}limit=1`);
    } catch (error) {
      d.clobeError = error;
      d.clobe = null;
    } finally {
      d.clobeLoading = false;
    }
    renderDelivery();
  }

  function staleNotice(service) {
    const since = service.last_data_date ? `${shortDate(service.last_data_date)}까지 수집됨` : "수집된 자료가 없음";
    const actions = [`<button type="button" class="primary" data-od-action="reconnect">다시 연결</button>`];
    if (service.csv_upload_available) actions.push(`<button type="button" data-od-action="upload-csv">정산 CSV 올리기</button>`);
    return `<div class="od-stale" role="alert"><div><strong>${esc(service.label)} 수집이 멈춤</strong><span>${esc(since)}${service.stale_days != null ? ` · ${service.stale_days}일 전` : ""}. 화면의 숫자는 마지막으로 수집된 자료입니다.</span></div><div class="od-actions">${actions.join("")}</div></div>`;
  }

  function trendHtml(trend) {
    if (!trend?.length) return "";
    const totals = trend.map(row => Object.values(row.by_service || {}).reduce((sum, v) => sum + Number(v || 0), 0));
    const max = Math.max(...totals, 1);
    const bars = trend.map((row, i) => `<li title="${esc(`${shortDate(row.date)} ${won(totals[i])}`)}"><i style="height:${Math.max(4, Math.round(totals[i] / max * 100))}%"></i><small>${esc(String(Number(row.date.slice(8, 10))))}</small></li>`).join("");
    const best = totals.indexOf(max);
    return `<section class="od-block"><h3>일별 매출 추이</h3><ol class="od-trend" aria-label="일별 배달앱 매출">${bars}</ol><p class="od-sub">가장 높았던 날 ${esc(shortDate(trend[best].date))} · ${esc(won(max))}</p></section>`;
  }

  function serviceTable(services) {
    if (!services.length) return "";
    const rows = services.map(s => {
      const st = s.settlements;
      return `<tr><th scope="row">${esc(s.label)}${s.stale ? `<em class="od-tag warn">수집 멈춤</em>` : ""}</th>
        <td data-label="매출" class="num">${esc(won(s.sales.gross))}<small>${esc(count(s.sales.orders))}</small></td>
        <td data-label="수수료" class="num">${esc(won(st.fee))}<small>${st.fee_rate != null ? `${esc(String(st.fee_rate))}%` : ""}</small></td>
        <td data-label="정산액" class="num">${esc(won(st.payout))}<small>예정 ${esc(won(st.scheduled))} · 입금 ${esc(won(st.paid))}</small></td>
        <td data-label="리뷰">${s.reviews ? `${esc(count(s.reviews.count))}<small>${s.reviews.rating != null ? `평점 ${esc(String(s.reviews.rating))}` : ""}${s.reviews.waiting ? ` · 답글 대기 ${esc(count(s.reviews.waiting))}` : ""}</small>` : "-"}</td>
        <td data-label="광고">${s.ads ? `${esc(won(s.ads.cost))}<small>${esc(count(s.ads.count))}</small>` : "-"}</td>
        <td data-label="마지막 자료">${esc(s.last_data_date ? shortDate(s.last_data_date) : "없음")}<small>${s.last_collected_at ? `수집 ${esc(dateTimeText(s.last_collected_at))}` : ""}</small></td></tr>`;
    }).join("");
    return `<section class="od-block"><h3>앱별 현황</h3><div class="od-table-wrap"><table class="od-table"><thead><tr><th>배달앱</th><th class="num">매출</th><th class="num">수수료</th><th class="num">정산액</th><th>리뷰</th><th>광고비</th><th>마지막 자료</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  }

  function branchTable(branches) {
    if (!branches.length) return "";
    const rows = branches.slice(0, 60).map(b => `<tr><th scope="row">${esc(b.branch || b.business_name || "매장")}</th><td data-label="배달앱">${esc(b.label)}</td><td data-label="매출" class="num">${esc(won(b.gross))}<small>${esc(count(b.orders))}</small></td><td data-label="수수료" class="num">${esc(won(b.fee))}</td><td data-label="정산액" class="num">${esc(won(b.payout))}</td></tr>`).join("");
    return `<section class="od-block"><h3>매장·앱별 현황</h3><div class="od-table-wrap"><table class="od-table"><thead><tr><th>매장</th><th>배달앱</th><th class="num">매출</th><th class="num">수수료</th><th class="num">정산액</th></tr></thead><tbody>${rows}</tbody></table></div>${branches.length > 60 ? `<p class="od-sub">상위 60개 행만 보입니다. 기간을 줄이면 더 자세히 볼 수 있습니다.</p>` : ""}</section>`;
  }

  function ownTab(data) {
    if (!data.has_data) {
      return stateBox("empty", "배달앱에서 수집된 자료가 아직 없습니다", "배달앱 연동을 연결하면 매출·정산·리뷰·광고가 여기에 모입니다.", `<div class="od-actions"><button type="button" class="primary" data-od-action="reconnect">배달앱 연결하기</button><button type="button" data-od-action="upload-csv">정산 CSV 올리기</button></div>`);
    }
    const stale = (data.services || []).filter(s => s.stale || s.collection_failed).map(staleNotice).join("");
    const t = data.totals || {};
    return `${stale}
      <div class="od-summary"><div><small>매출</small><b>${esc(won(t.gross))}</b><span>${esc(count(t.orders))}</span></div><div><small>수수료</small><b>${esc(won(t.fee))}</b></div><div><small>정산액</small><b>${esc(won(t.payout))}</b></div></div>
      ${serviceTable(data.services || [])}${trendHtml(data.trend)}${branchTable(data.branches || [])}`;
  }

  function basisLabel(basis) {
    const text = String(basis || "").toUpperCase();
    if (text.includes("SETTLE")) return "정산일 기준";
    if (text.includes("SALE") || text.includes("ORDER")) return "매출일 기준";
    return "집계 기준 미상";
  }

  function clobeTab() {
    const d = ui.delivery;
    if (d.clobeLoading) return stateBox("loading", "클로브 자료를 불러오는 중입니다", "");
    if (d.clobeError) return stateBox("error", errorText(d.clobeError), "", `<button type="button" data-od-action="reload-clobe">다시 불러오기</button>`);
    const c = d.clobe;
    if (!c) return "";
    const channels = c.summary?.by_channel || [];
    const delivery = channels.filter(item => item.channel === "DELIVERY");
    if (c.connect_required || !c.total || !delivery.length) {
      return stateBox("empty", "클로브에서 배달앱 연결 필요", c.connect_required ? "클로브에 배달앱(판매채널)이 연결되어 있지 않아 이 탭에 보여줄 자료가 없습니다." : "선택한 기간에 클로브가 받은 배달앱 매출이 없습니다.", `<div class="od-actions"><button type="button" data-od-action="open-settings">자료 연결 보기</button></div>`);
    }
    const lines = (c.summary.by_institution || []).filter(item => item.channel === "DELIVERY").map(item => `<tr><th scope="row">${esc(item.institution || "배달앱")}</th><td data-label="기준">${esc(basisLabel(item.date_basis))}</td><td data-label="매출" class="num">${esc(won(item.gross))}</td><td data-label="수수료" class="num">${esc(won(item.fee))}</td><td data-label="입금액" class="num">${esc(won(item.net))}</td><td data-label="건수" class="num">${esc(count(item.count))}</td></tr>`).join("");
    return `<section class="od-block"><h3>클로브 기준 배달앱 매출</h3><div class="od-table-wrap"><table class="od-table"><thead><tr><th>배달앱</th><th>기준</th><th class="num">매출</th><th class="num">수수료</th><th class="num">입금액</th><th class="num">건수</th></tr></thead><tbody>${lines}</tbody></table></div><p class="od-sub">클로브가 받은 판매채널 집계입니다. 자체 수집 숫자와 기준이 달라 차이가 날 수 있습니다.</p></section>`;
  }

  function renderDelivery() {
    const root = deliveryEl();
    if (!root) return;
    const d = ui.delivery;
    const options = d.businesses.map(item => `<option value="${esc(item.business_id)}"${item.business_id === d.business ? " selected" : ""}>${esc(item.name || "사업자")}</option>`).join("");
    let body;
    if (!token()) body = stateBox("empty", "로그인 후 배달앱 현황을 볼 수 있습니다", "");
    else if (d.loading) body = stateBox("loading", "배달앱 자료를 불러오는 중입니다", "");
    else if (d.error) body = stateBox("error", errorText(d.error), "", `<button type="button" data-od-action="reload-delivery">다시 불러오기</button>`);
    else if (!d.business) body = stateBox("empty", "표시할 사업자가 없습니다", "");
    else body = d.tab === "clobe" ? clobeTab() : (d.data ? ownTab(d.data) : "");
    root.innerHTML = `<div class="panel-head"><h2 class="panel-title">배달앱 매출·정산 현황</h2><button type="button" class="v2-touch" data-od-action="reload-delivery">새로고침</button></div>
      <div class="panel-body od-delivery">
        <form class="od-filters" onsubmit="return false">
          <label>사업자<select data-od-field="business">${options || `<option value="">사업자 없음</option>`}</select></label>
          <label>시작일<input type="date" data-od-field="from" value="${esc(d.from)}"></label>
          <label>종료일<input type="date" data-od-field="to" value="${esc(d.to)}"></label>
        </form>
        <div class="od-tabs" role="tablist" aria-label="집계 기준">
          <button type="button" role="tab" aria-selected="${d.tab === "own"}" class="${d.tab === "own" ? "active" : ""}" data-od-tab="own">자체 수집</button>
          <button type="button" role="tab" aria-selected="${d.tab === "clobe"}" class="${d.tab === "clobe" ? "active" : ""}" data-od-tab="clobe">클로브 기준</button>
        </div>
        ${body}
      </div>`;
  }

  function openDelivery() {
    renderDelivery();
    loadDelivery();
  }

  function uploadCsv() {
    if (isV41()) { document.getElementById("quickRegister")?.click(); return; }
    const input = document.querySelector('[data-ledger-upload="sales"] [data-upload-file]');
    if (!input) return;
    input.scrollIntoView({ behavior: "smooth", block: "center" });
    input.focus({ preventScroll: true });
  }

  function onAction(name) {
    switch (name) {
      case "reload-glance": loadGlance(); break;
      case "reload-delivery": ui.delivery.businesses = []; loadDelivery(); break;
      case "reload-clobe": loadClobeTab(); break;
      case "open-delivery": goView("sales"); break;
      case "open-review": goView("clobeReview"); break;
      case "open-overview": goView("clobeOverview"); break;
      case "open-settings": goView("clobeSettings"); break;
      case "reconnect": goView("integrations"); break;
      case "upload-csv": uploadCsv(); break;
      default: break;
    }
  }

  function bind() {
    document.addEventListener("click", event => {
      const action = event.target.closest?.("[data-od-action]");
      if (action && !action.disabled) { onAction(action.dataset.odAction); return; }
      const tab = event.target.closest?.("[data-od-tab]");
      if (tab) {
        ui.delivery.tab = tab.dataset.odTab;
        renderDelivery();
        if (ui.delivery.tab === "clobe" && !ui.delivery.clobe) loadClobeTab();
        return;
      }
      if (event.target.closest?.("#homeRefreshBtn")) setTimeout(loadGlance, 0);
    });
    document.addEventListener("change", event => {
      const el = event.target;
      if (el.id === "homeBusinessSelect") { loadGlance(); return; }
      const field = el.dataset?.odField;
      if (!field) return;
      ui.delivery[field] = el.value;
      ui.delivery.clobe = null;
      loadDelivery();
    });
  }

  function v41Host(id, className) {
    const page = document.getElementById("page");
    let el = document.getElementById(id);
    if (el && page.contains(el)) return el;
    el = document.createElement("section");
    el.id = id;
    el.className = className;
    const hero = page.querySelector(".hero");
    if (hero) hero.after(el); else page.prepend(el);
    return el;
  }

  function syncV41() {
    const route = document.querySelector("#nav [data-route].active")?.dataset.route || "";
    const business = homeBusiness();
    if (!business || (route !== "home" && route !== "sales")) return;
    if (route === "home") {
      v41Host("obysGlance", "od-v41 od-v41-glance");
      if (!ui.glance.attempted || ui.glance.business !== business) loadGlance(); else renderGlance();
      return;
    }
    v41Host("obysDeliveryPanel", "od-v41 od-v41-delivery");
    const d = ui.delivery;
    if (d.v41Business !== business) {
      d.v41Business = business;
      d.business = business;
      d.data = null;
      d.clobe = null;
      loadDelivery();
    } else {
      renderDelivery();
    }
  }

  function initV41() {
    syncV41();
    new MutationObserver(syncV41).observe(document.getElementById("page"), { childList: true });
  }

  function init() {
    bind();
    if (isV41()) { initV41(); return; }
    const goto = new URLSearchParams(location.search).get("goto");
    if (/^[A-Za-z]+$/.test(goto || "")) setTimeout(() => goView(goto), 700);
    renderGlance();
    renderDelivery();
    const visible = id => { const el = document.getElementById(id); return Boolean(el) && !el.classList.contains("hidden"); };
    if (token() && visible("dashboardView")) setTimeout(loadGlance, 400);
    if (token() && visible("salesView")) setTimeout(openDelivery, 400);
  }

  window.obysDataOverview = {
    openGlance: loadGlance,
    openDelivery,
    glanceCards,
    collectionSummary
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
