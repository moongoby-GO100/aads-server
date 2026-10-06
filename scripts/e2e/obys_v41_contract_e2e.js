// V4.1 현재 관리자 화면 계약 흐름 E2E (모의 API). 실제 운영 데이터·운영 서버를 쓰지 않는다.
// node e2e.js <worktree> <evidenceDir>
const fs = require("fs");
const path = require("path");
const { chromium } = require("/root/aads/aads-server/node_modules/playwright-core");

const [wt, outDir] = process.argv.slice(2);
const STATIC = path.join(wt, "app/static");
fs.mkdirSync(outDir, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => { results.push({ name, ok: !!ok, detail }); console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`); };

function makeServer() {
  const db = {
    employees: [{ id: "req-yang", name: "양시험", email: "yang@example.com", email_masked: "y***@example.com", phone: "010-1111-2222", birth_date: "1995-03-04", address: "", business_id: "biz-mia", branch: "미아점", onboarding_document_count: 2, status: "approved" },
      { id: "req-kim", name: "김시험", email: "kim@example.com", email_masked: "k***@example.com", phone: "010-3333-4444", birth_date: "", address: "", business_id: "biz-mia", branch: "미아점", onboarding_document_count: 0, status: "approved" }],
    contracts: [],
    docs: [], docPosts: [],
    posts: 0, requests: 0, pdfCalls: 0
  };
  return db;
}

async function run(viewport, label) {
  const db = makeServer();
  const browser = await chromium.launch({ executablePath: process.env.CHROME_BIN || undefined });
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("pageerror", e => errors.push(String(e.stack || e).slice(0, 400)));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.startsWith("/static/")) {
      const file = path.join(STATIC, decodeURIComponent(url.pathname.replace(/^\/static\//, "")));
      if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: "" });
      const type = file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".png") ? "image/png" : "text/html";
      return route.fulfill({ status: 200, contentType: type, body: fs.readFileSync(file) });
    }
    if (url.pathname === "/api/v1/auth/tenants") return json({ tenants: [{ tenant_id: "t-test", name: "시험 조직" }], current_tenant_id: "t-test" });
    if (url.pathname === "/api/v1/workspaces/businesses") return json({ businesses: [{ id: "biz-mia", name: "열정국밥 미아점" }] });
    if (/^\/api\/v1\/workspaces\/[^/]+\/[^/]+\/(summary|records)$/.test(url.pathname)) return json({ metrics: [], records: [] });
    const p = url.pathname.replace("/api/v1/yeoljeong-finance", "");
    if (p === "/employees/approved") return json({ employees: db.employees });
    if (p === "/employees/join-requests") return json({ requests: [] });
    if (p === "/employees/invites") return json({ invites: [] });
    if (p === "/tenant-registry/businesses") return json({ businesses: [{ id: "biz-mia", name: "열정국밥 미아점", branches: [{ name: "미아점" }] }] });
    if (p === "/contracts" && method === "GET") return json({ contracts: db.contracts });
    if (p === "/contracts" && method === "POST") {
      db.posts += 1;
      const body = JSON.parse(route.request().postData() || "{}");
      if (db.posts === 1) return json({ detail: "계약서 필수 입력값을 확인하십시오: 근로자 주소" }, 400);
      const now = new Date().toISOString();
      const saved = { ...body, id: body.id || "ct-0001", status: "draft", employee_email_masked: "y***@example.com", employer_name: body.employer_name || "열정국밥 미아점", employer_registration_no: body.employer_registration_no || "123-45-67890", employer_representative: body.employer_representative || "대표", employer_address: body.employer_address || "서울 강북구", created_at: now, updated_at: now };
      db.contracts = [saved, ...db.contracts.filter(c => c.id !== saved.id)];
      return json({ contract: saved });
    }
    let m = p.match(/^\/contracts\/([^/]+)\/request-signature$/);
    if (m) {
      db.requests += 1;
      const c = db.contracts.find(x => x.id === m[1]);
      Object.assign(c, { status: "requested", sign_token: "tok-test", requested_at: new Date().toISOString() });
      return json({ contract: c, notify: { event: "signature_requested", status: "failed", channels: [{ channel: "email", status: "failed", target_masked: "y***@example.com", error_detail: "SMTP not configured" }], logged: true } });
    }
    m = p.match(/^\/contracts\/([^/]+)\/resend-signature-notice$/);
    if (m) return json({ detail: "서명 요청 알림은 5분에 한 번만 보낼 수 있습니다. 280초 후 다시 시도하십시오" }, 429);
    m = p.match(/^\/contracts\/([^/]+)\/signed-pdf$/);
    if (m) { db.pdfCalls += 1; return route.fulfill({ status: 200, contentType: "application/pdf", headers: { "content-disposition": 'attachment; filename="contract-test.pdf"' }, body: Buffer.from("%PDF-1.4\n%test\n") }); }
    if (p === "/onboarding/document-types") return json({ document_types: [
      { type: "resident_register", label: "주민등록등본", requirement: "필수", notice: "마스킹본" },
      { type: "health_certificate", label: "보건증", requirement: "필수", notice: "" },
      { type: "bank_account", label: "통장사본", requirement: "선택", notice: "" }] });
    if (p === "/onboarding/documents" && method === "GET") return json({ documents: db.docs });
    if (p === "/onboarding/documents" && method === "POST") {
      const raw = route.request().postDataBuffer()?.toString("latin1") || "";
      const field = name => (raw.match(new RegExp(`name="${name}"\\r\\n\\r\\n([^\\r]*)`)) || [])[1] || "";
      const doc = { id: `doc-${db.docs.length + 1}`, employee_email: field("employee_email"), business_id: "biz-mia", document_type: field("document_type"), document_label: field("document_type") === "health_certificate" ? "보건증" : "주민등록등본", original_filename: "test.pdf", status: "uploaded", status_label: "검토 대기", issue_date: field("issue_date"), expires_at: field("expires_at") };
      db.docPosts.push(doc);
      db.docs.push(doc);
      return json({ document: doc });
    }
    if (url.pathname.startsWith("/api/")) return json({});
    return route.fulfill({ status: 404, body: "" });
  });

  await page.goto("https://fb.test/static/apps/obys/mockup-v4-1.html#employees");
  await page.waitForSelector("#contractV41 [data-cv41-employee]", { timeout: 15000 });
  check(`${label}: 직원 현황에 승인 직원 계약 섹션·계약 작성 버튼`, await page.isVisible("#contractV41 >> text=계약 작성"));
  await page.screenshot({ path: path.join(outDir, `${label}-01-employees.png`), fullPage: false });
  await page.locator("#contractV41").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(outDir, `${label}-01b-employees-section.png`) });

  await page.click("#contractV41 [data-cv41-employee]");
  await page.waitForSelector("#contractEditorV41[open]");
  check(`${label}: 기존 화면 이동 없이 같은 페이지에서 편집기 열림`, page.url().includes("mockup-v4-1.html") && page.url().includes("cv41_request=req-yang"), page.url());
  check(`${label}: 직원·회사·점포 문맥 고정 표시`, (await page.textContent("#contractEditorV41 .cv41-head")).includes("양시험") && (await page.textContent("#contractEditorV41 .cv41-head")).includes("미아점"));

  // 금액 미입력 상태로 미리보기 → 차단
  await page.click("[data-cv41-preview]");
  const err = await page.textContent("#cv41Error");
  check(`${label}: 금액·입사일 비어 있으면 미리보기 차단`, err.includes("확정 임금") && err.includes("입사일"), err.replace(/\s+/g, " ").slice(0, 160));
  await page.screenshot({ path: path.join(outDir, `${label}-02-validation.png`) });

  await page.fill("#cv41_startDate", "2026-10-07");
  await page.fill("#cv41_wage", "10320");
  await page.fill("#cv41_workTime", "10:00-15:00");
  await page.fill("#cv41_restTime", "12:00-12:30");
  await page.fill("#cv41_weeklyHours", "주 18시간");
  await page.fill("#cv41_workDays", "월/화/수/목");
  await page.fill("#cv41_dailyWorkSchedule", "월~목 10:00-15:00 (휴게 30분)");
  // 주소는 비워 두었다 — 클라이언트 검증이 먼저 잡아야 한다
  await page.click("[data-cv41-preview]");
  check(`${label}: 근로자 주소 누락을 칸으로 안내`, await page.getAttribute("#cv41_employeeAddress", "aria-invalid") === "true");
  await page.fill("#cv41_employeeAddress", "서울시 강북구 시험로 1");
  await page.click("[data-cv41-preview]");
  await page.waitForSelector(".cv41-paper");
  const paper = await page.textContent(".cv41-paper");
  check(`${label}: 미리보기에 입력 조건 반영`, paper.includes("양시험") && paper.includes("10,320원") && paper.includes("월~목 10:00-15:00"));
  await page.screenshot({ path: path.join(outDir, `${label}-03-preview.png`) });

  // 서버 400(모의) → 입력 보존 + 해당 칸 표시
  await page.click("[data-cv41-save]");
  await page.waitForSelector("#cv41Error");
  check(`${label}: 서버 400 시 입력 보존·해당 칸 표시`, (await page.inputValue("#cv41_wage")) === "10320" && (await page.getAttribute("#cv41_employeeAddress", "aria-invalid")) === "true");
  await page.click("[data-cv41-preview]");
  await page.click("[data-cv41-save]");
  await page.waitForSelector("[data-cv41-request]");
  const saved = await page.textContent(".cv41-body");
  check(`${label}: 저장 후 서버 계약 번호·상태 표시`, saved.includes("ct-0001") && saved.includes("작성중"));
  check(`${label}: 저장 본문이 기존 API 형식(snake_case)`, db.contracts[0]?.employee_request_id === "req-yang" && db.contracts[0]?.wage === 10320 && db.contracts[0]?.business_id === "biz-mia");

  // 서명요청 — 연속 클릭에도 1회
  await page.click("[data-cv41-request]");
  await page.click("[data-cv41-request]").catch(() => {});
  await page.waitForSelector("[data-cv41-resend]");
  check(`${label}: 서명요청 연속 클릭 시 1회만 호출`, db.requests === 1, `requests=${db.requests}`);
  const body = await page.textContent(".cv41-body");
  check(`${label}: 알림 실패를 전송 완료로 표시하지 않음`, body.includes("알림 전송은 확인되지 않았습니다") && !body.includes("서명 알림을 보냈습니다"));
  await page.screenshot({ path: path.join(outDir, `${label}-04-requested-notify-failed.png`) });
  await page.click("[data-cv41-resend]");
  await page.waitForSelector("#cv41Error");
  check(`${label}: 재알림 429 안내`, (await page.textContent("#cv41Error")).includes("5분에 한 번"));

  // 새로고침 후 문맥 복구
  await page.reload();
  await page.waitForSelector("#contractEditorV41[open]", { timeout: 15000 });
  check(`${label}: 새로고침 후 같은 계약 다시 열림`, (await page.textContent(".cv41-body")).includes("ct-0001"));
  await page.click("[data-cv41-close]");

  // 서명 완료(모의) → 계약·인사증빙에서 PDF
  db.contracts[0].status = "signed"; db.contracts[0].signed_at = new Date().toISOString(); db.contracts[0].signed_pdf_path = "x.pdf";
  // 메뉴로 이동(실제 사용 경로). 모바일은 메뉴가 서랍 안이라 DOM 클릭으로 누른다.
  await page.evaluate(() => document.querySelector('[data-route="hr-docs"]').click());
  await page.waitForSelector("#contractV41 [data-cv41-pdf]", { timeout: 15000 });
  await page.locator("#contractV41").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(outDir, `${label}-05-hr-docs-signed.png`) });
  const [download] = await Promise.all([page.waitForEvent("download"), page.click("#contractV41 [data-cv41-pdf]")]);
  const pdfPath = path.join(outDir, `${label}-signed.pdf`);
  await download.saveAs(pdfPath);
  check(`${label}: 서명본 PDF 실제 파일 수신`, fs.readFileSync(pdfPath).slice(0, 5).toString() === "%PDF-", download.suggestedFilename());
  // 직원 입사서류 등록(관리자)
  await page.evaluate(() => document.querySelector('[data-route="employees"]').click());
  await page.waitForSelector('#contractV41 [data-cv41-docs="req-kim"]', { timeout: 15000 });
  const listText = await page.textContent("#contractV41");
  check(`${label}: 직원 목록에 빈 필수값 개수·보류 건수 표시`, listText.includes("빈 필수값") && listText.includes("계약 보류"), listText.replace(/\s+/g, " ").slice(0, 160));
  await page.click('#contractV41 [data-cv41-docs="req-kim"]');
  await page.waitForSelector("#staffDocsV41[open] #cv41DocType");
  const docsText = await page.textContent("#staffDocsV41");
  check(`${label}: 서류 창에 미제출 필수서류 표시`, docsText.includes("미제출 필수서류 2개") && docsText.includes("보건증"));
  await page.click('#staffDocsV41 [data-cv41-doc-pick="health_certificate"]');
  await page.setInputFiles("#cv41DocFile", { name: "health.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%doc\n") });
  await page.click("#staffDocsV41 [data-cv41-doc-upload]");
  await page.waitForSelector("#staffDocsV41 .cv41-alert.bad");
  check(`${label}: 보건증 만료일 비면 업로드 전에 차단`, (await page.textContent("#staffDocsV41 .cv41-alert.bad")).includes("만료일") && db.docPosts.length === 0);
  await page.selectOption("#cv41DocType", "health_certificate");
  await page.fill("#cv41DocExpire", "2027-10-06");
  await page.setInputFiles("#cv41DocFile", { name: "health.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%doc\n") });
  await page.click("#staffDocsV41 [data-cv41-doc-upload]");
  await page.waitForFunction(() => (document.querySelector("#staffDocsV41")?.textContent || "").includes("등록된 서류 1건"), null, { timeout: 10000 });
  check(`${label}: 관리자 대리 업로드가 직원 이메일·종류로 저장`, db.docPosts.length === 1 && db.docPosts[0].employee_email === "kim@example.com" && db.docPosts[0].document_type === "health_certificate", JSON.stringify(db.docPosts[0] || {}).slice(0, 160));
  await page.screenshot({ path: path.join(outDir, `${label}-06-staff-docs.png`) });
  await page.click("#staffDocsV41 .cv41-foot [data-cv41-docs-close]");

  // 빈 필수값 표시·추천값
  await page.click('#contractV41 [data-cv41-employee="req-kim"]');
  await page.waitForSelector("#contractEditorV41[open] #cv41Missing");
  const panel = await page.textContent("#cv41Missing");
  check(`${label}: 입력 단계에서 빈 필수값 즉시 표시`, panel.includes("비어 있는 필수값") && panel.includes("입사일") && panel.includes("근로자 주소"), panel.replace(/\s+/g, " ").slice(0, 160));
  check(`${label}: 빈 칸이 미리보기 전부터 빨갛게 표시`, (await page.getAttribute("#cv41_startDate", "aria-invalid")) === "true");
  await page.screenshot({ path: path.join(outDir, `${label}-07-required-missing.png`) });
  await page.click("[data-cv41-suggest-all]");
  const start = await page.inputValue("#cv41_startDate");
  const wage = await page.inputValue("#cv41_wage");
  check(`${label}: 추천값 모두 넣기 — 입사일·최저시급 채움`, /^\d{4}-\d{2}-\d{2}$/.test(start) && wage === "10320", `start=${start} wage=${wage}`);
  const after = await page.textContent("#cv41Missing");
  check(`${label}: 추정 금지 항목(주소·생년월일)은 직접 입력·서류 확인으로 남김`, after.includes("근로자 주소") && after.includes("근로자 생년월일") && !after.includes("입사일") && (await page.$("#cv41Missing [data-cv41-docs-open]")) !== null);
  await page.fill("#cv41_employeeAddress", "서울시 강북구 시험로 2");
  await page.locator("#cv41_employeeAddress").blur();
  check(`${label}: 칸을 채우고 나가면 표시 즉시 해제`, (await page.getAttribute("#cv41_employeeAddress", "aria-invalid")) === null && !(await page.textContent("#cv41Missing")).includes("근로자 주소"));
  await page.screenshot({ path: path.join(outDir, `${label}-08-suggested.png`) });
  page.once("dialog", d => d.accept());
  await page.click("#contractEditorV41 .cv41-foot [data-cv41-close]");

  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  check(`${label}: 가로 넘침 없음`, overflow <= 1, `overflow=${overflow}px`);
  check(`${label}: 페이지 스크립트 오류 없음`, errors.length === 0, errors.join(" | ").slice(0, 300));
  await browser.close();
}

(async () => {
  await run({ width: 1440, height: 1000 }, "desktop");
  await run({ width: 390, height: 844 }, "mobile");
  fs.writeFileSync(path.join(outDir, "e2e-results.json"), JSON.stringify(results, null, 2));
  const failed = results.filter(r => !r.ok).length;
  console.log(`TOTAL ${results.length} PASS ${results.length - failed} FAIL ${failed}`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
