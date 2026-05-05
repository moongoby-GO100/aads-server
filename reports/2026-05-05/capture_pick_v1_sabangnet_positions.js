const fs = require('fs');
const path = require('path');
const puppeteer = require('/tmp/ntv2-pick-capture/node_modules/puppeteer-core');

const OUT_DIR = path.resolve(__dirname, 'screenshots');
const BASE = 'https://pick.newtalk.kr';
const USER = process.env.PICK_USER;
const PASS = process.env.PICK_PASS;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

if (!USER || !PASS) {
  console.error('PICK_USER and PICK_PASS are required.');
  process.exit(2);
}

fs.mkdirSync(OUT_DIR, { recursive: true });

async function safeGoto(page, url) {
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 45000 });
  await sleep(1200);
}

async function save(page, name) {
  await page.screenshot({ path: path.join(OUT_DIR, `${name}.png`), fullPage: true });
  fs.writeFileSync(path.join(OUT_DIR, `${name}.html`), await page.content());
}

async function clickText(page, text) {
  const clicked = await page.evaluate((needle) => {
    const nodes = Array.from(document.querySelectorAll('button, a, span, div, li'));
    const el = nodes.find((node) => (node.innerText || '').includes(needle));
  if (!el) return false;
    const target = el.closest('button, a, li') || el;
    target.scrollIntoView({ block: 'center', inline: 'center' });
    target.click();
    return true;
  }, text);
  await sleep(600);
  return clicked;
}

async function annotate(page, title) {
  await page.evaluate((label) => {
    const style = document.createElement('style');
    style.textContent = `
      .aads-mark { position:absolute; z-index:2147483647; border:4px solid #ef4444; background:rgba(239,68,68,.08); pointer-events:none; }
      .aads-note { position:absolute; z-index:2147483647; max-width:360px; padding:10px 12px; border-radius:6px; background:#111827; color:#fff; font:700 14px/1.45 Arial,sans-serif; box-shadow:0 8px 24px rgba(0,0,0,.25); pointer-events:none; }
      .aads-note small { display:block; margin-top:4px; color:#fde68a; font-weight:600; }
    `;
    document.head.appendChild(style);

    const byText = (needle) => Array.from(document.querySelectorAll('button, a, span, li, label, option, td, th, div'))
      .find((node) => (node.innerText || node.textContent || '').trim().includes(needle));
    const mark = (el, text, dx = 0, dy = 0) => {
      if (!el) return false;
      const target = el.closest('button, a, li, tr, .btn-group') || el;
      target.scrollIntoView({ block: 'center', inline: 'center' });
      const r = target.getBoundingClientRect();
      const top = Math.max(0, r.top + window.scrollY - 8);
      const left = Math.max(0, r.left + window.scrollX - 8);
      const box = document.createElement('div');
      box.className = 'aads-mark';
      box.style.left = `${left}px`;
      box.style.top = `${top}px`;
      box.style.width = `${Math.max(80, r.width + 16)}px`;
      box.style.height = `${Math.max(36, r.height + 16)}px`;
      document.body.appendChild(box);

      const note = document.createElement('div');
      note.className = 'aads-note';
      note.innerHTML = text;
      note.style.left = `${Math.min(left + r.width + 30 + dx, window.scrollX + window.innerWidth - 390)}px`;
      note.style.top = `${Math.max(8, top + dy)}px`;
      document.body.appendChild(note);
      return true;
    };

    const menu = byText('선택형 매뉴') || byText('선택형 메뉴');
    const sabang = byText('사방넷등록엑셀파일다운');
    const waitStatus = byText('사방넷등록대기');
    const doneStatus = byText('사방넷등록완료');
    const productCode = byText('상품코드') || byText('상품명');

    mark(menu, '① 선택형 매뉴<br><small>기존 선택상품 처리 진입점</small>');
    mark(sabang, '② 기존 사방넷 엑셀 다운로드<br><small>바로 아래에 “사방넷 자동업로드/전송” 추가</small>', 0, 42);
    mark(waitStatus, '③ 상태값 연동<br><small>자동업로드 전: 사방넷등록대기</small>', -120, 0);
    mark(doneStatus, '④ 완료 상태 반영<br><small>업로드 성공 후: 사방넷등록완료</small>', -120, 42);
    mark(productCode, '⑤ 상품 목록 체크박스/상품 행<br><small>선택 상품 기준으로 엑셀 생성 및 업로드 작업 등록</small>', 0, -72);

    const banner = document.createElement('div');
    banner.className = 'aads-note';
    banner.style.position = 'fixed';
    banner.style.left = '18px';
    banner.style.top = '18px';
    banner.style.maxWidth = '520px';
    banner.innerHTML = `${label}<small>NewTalk V1 사방넷 자동업로드 반영 위치 주석</small>`;
    document.body.appendChild(banner);
  }, title);
  await sleep(800);
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: '/usr/bin/chromium-browser',
    headless: 'new',
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-blink-features=AutomationControlled',
      '--window-size=1440,1200',
    ],
    defaultViewport: { width: 1440, height: 1200 },
  });
  const page = await browser.newPage();
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36');
  await page.setExtraHTTPHeaders({ 'accept-language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7' });
  await page.evaluateOnNewDocument(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });
  page.setDefaultTimeout(20000);

  await safeGoto(page, `${BASE}/auth/login/`);
  await save(page, '00_login_page_before_submit');
  const loginSelector = await page.evaluate(() => {
    if (document.querySelector('#login')) return '#login';
    if (document.querySelector('input[name="login"]')) return 'input[name="login"]';
    return '';
  });
  const passwordSelector = await page.evaluate(() => {
    if (document.querySelector('#password')) return '#password';
    if (document.querySelector('input[name="password"]')) return 'input[name="password"]';
    return '';
  });
  if (!loginSelector || !passwordSelector) {
    throw new Error(`login form not found at ${page.url()}`);
  }
  await page.type(loginSelector, USER, { delay: 10 });
  await page.type(passwordSelector, PASS, { delay: 10 });
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 45000 }).catch(() => null),
    page.click('button[type="submit"]'),
  ]);
  await sleep(1500);
  await save(page, '00_after_login');

  const urls = [
    ['01_root_home', `${BASE}/root/home`, '관리자 홈: 좌측 메뉴 반영 위치'],
    ['02_product_mng', `${BASE}/Product/product_mng`, '신규 관리자 상품관리: 메뉴 진입 및 상태 연동 위치'],
    ['03_legacy_products_index', `${BASE}/products/index`, '구형 상품관리: 선택형 매뉴 사방넷 버튼 반영 위치'],
    ['04_product_store_list', `${BASE}/root/product_store_list`, '입고 상세 조회: 상품/입고 상태 참고 위치'],
    ['05_product_store', `${BASE}/root/product_store`, '입고 등록: 등록 후 사방넷등록대기 상태 연동 위치'],
  ];

  const summary = [];
  for (const [name, url, title] of urls) {
    try {
      await safeGoto(page, url);
      await clickText(page, '선택형');
      await clickText(page, '사방넷등록엑셀파일다운');
      await save(page, `${name}_raw`);
      if (process.env.NO_DOM_ANNOTATE !== '1') {
        await annotate(page, title);
      }
      await save(page, name);
      summary.push({ name, url: page.url(), title, ok: true });
    } catch (err) {
      await save(page, `${name}_error`);
      summary.push({ name, url: page.url(), title, ok: false, error: err.message });
    }
  }

  fs.writeFileSync(path.join(OUT_DIR, 'capture-summary.json'), JSON.stringify(summary, null, 2));
  await browser.close();
  console.log(JSON.stringify(summary, null, 2));
})();
