/* 오비서 계약서 도장 — 법인/개인사업자 도장을 SVG 로 만든다(이미지 파일 없음).
 *
 * 양식(대표님 기준 도장 사진 판독):
 *   붉은 인주색 원형 도장. 바깥 원 + 안쪽 원 사이 띠에 글자가 시계 방향으로 둘러 있고(글자 윗부분이 바깥쪽),
 *   띠의 시작·끝 사이에 둥근 점(●) 하나가 있다. 안쪽 원 안에는 글자가 세로 두 줄(오른쪽 줄 먼저)로 들어간다.
 *   법인은 띠에 회사명, 안쪽에 "代表理事". 개인사업자는 띠에 상호, 안쪽에 "대표자 이름 + 인".
 * 글꼴: 띠·개인 안쪽 한글은 Gugi(OFL), 법인 안쪽 한자는 Noto Serif KR Black(OFL) 서브셋을 자체 호스팅한다.
 *   <img src=data:image/svg> 안의 SVG 는 페이지 @font-face 를 쓰지 못하므로 계약서에는 인라인 SVG(inline:true)로 넣는다.
 * 같은 기하(layout)를 서버 PDF 가 app/services/yeoljeong_company_seal.py 에서 그대로 쓴다.
 * 두 구현이 어긋나지 않는지는 tests/unit/test_obys_company_seal.py 가 좌표를 비교해 검사한다.
 */
(function (root) {
  "use strict";
  const SIZE = 200;
  const CENTER = 100;
  const OUTER_R = 96;
  const INNER_R = 54;
  const RING_MID_R = 75;
  const DOT_ANGLE = -42;
  const DOT_R = 5.5;
  const RING_MAX_FS = 34;
  const INNER_MAX_FS = 36;
  const INNER_BOX = 76;
  const OUTER_W = 2;
  const INNER_W = 2.6;
  const TEXT_STROKE = 0.3;
  const CORP_TITLE = "代表理事";
  const INK = "#c1272d";
  const FONT_HANGUL = "ObysSealHangul";
  const FONT_HANJA = "ObysSealHanja";
  const FONT_BASE = "/static/apps/obys/assets/fonts/";
  const FONT_FALLBACK = "'Noto Serif KR','Nanum Myeongjo','Batang','AppleMyungjo','Noto Serif CJK KR','Noto Serif CJK',serif";
  const FONT = `'${FONT_HANGUL}','${FONT_HANJA}',${FONT_FALLBACK}`;
  const FONT_CSS = `@font-face{font-family:'${FONT_HANGUL}';src:url('${FONT_BASE}obys-seal-hangul-gugi.woff2') format('woff2');font-display:swap}`
    + `@font-face{font-family:'${FONT_HANJA}';src:url('${FONT_BASE}obys-seal-hanja-notoserifkr-black.woff2') format('woff2');font-display:swap}`;
  const CORP_PATTERN = /주식회사|\(주\)|㈜|유한회사|유한책임회사|합자회사|합명회사|법인/;

  function clean(value) {
    return Array.from(String(value ?? "").replace(/\s+/g, ""));
  }
  function round(value) {
    return Math.round(value * 100) / 100;
  }
  function escapeXml(value) {
    return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  function kindFor(entityType, companyName) {
    const text = String(entityType ?? "").trim().toLowerCase();
    if (["corporation", "corporate", "법인", "법인사업자"].includes(text)) return "corporation";
    if (["individual", "개인", "개인사업자"].includes(text)) return "individual";
    return CORP_PATTERN.test(String(companyName ?? "")) ? "corporation" : "individual";
  }

  // 원 안에 들어가는 글자 크기·위치를 계산한다. 이름·회사명이 비면 null.
  function layout(options) {
    const kind = options?.kind === "corporation" ? "corporation" : "individual";
    const company = clean(options?.companyName);
    const rep = clean(options?.representative);
    const ring = kind === "corporation" ? company : (company.length ? company : rep);
    const inner = kind === "corporation" ? clean(CORP_TITLE) : (rep.length ? [...rep, ...clean("인")] : []);
    if (!ring.length || !inner.length) return null;

    const slots = ring.length + 1;
    const step = 360 / slots;
    const arc = RING_MID_R * (step * Math.PI / 180);
    const ringFs = round(Math.min(RING_MAX_FS, arc * 0.8));
    const ringChars = ring.map((ch, i) => {
      const angle = round(DOT_ANGLE + step * (i + 1));
      return { ch, angle, x: CENTER, y: round(CENTER - RING_MID_R + ringFs * 0.35), fs: ringFs };
    });

    const rightCount = Math.ceil(inner.length / 2);
    const innerFs = round(Math.min(INNER_MAX_FS, INNER_BOX / rightCount));
    const gap = round(innerFs * 1.05);
    const columns = [inner.slice(0, rightCount), inner.slice(rightCount)];
    const innerChars = [];
    columns.forEach((column, columnIndex) => {
      const x = round(CENTER + (columnIndex === 0 ? gap / 2 : -gap / 2));
      const top = CENTER - (column.length * innerFs) / 2;
      column.forEach((ch, row) => {
        innerChars.push({ ch, x, y: round(top + row * innerFs + innerFs * 0.82), fs: innerFs });
      });
    });

    return {
      kind, size: SIZE, center: CENTER, outerR: OUTER_R, innerR: INNER_R, ringMidR: RING_MID_R,
      dot: { angle: DOT_ANGLE, r: DOT_R, x: round(CENTER + RING_MID_R * Math.sin(DOT_ANGLE * Math.PI / 180)), y: round(CENTER - RING_MID_R * Math.cos(DOT_ANGLE * Math.PI / 180)) },
      ring: ringChars, inner: innerChars
    };
  }

  // 도장을 그리는 화면에서만 글꼴을 요청한다. font-display:swap 이라 로드 전에는 대체 명조로 그려졌다가 로드되면 인라인 SVG 가 다시 그려진다.
  function ensureFonts() {
    if (typeof document === "undefined" || !document.head) return;
    if (!document.getElementById("obys-seal-fonts")) {
      const style = document.createElement("style");
      style.id = "obys-seal-fonts";
      style.textContent = FONT_CSS;
      document.head.appendChild(style);
    }
    if (document.fonts && typeof document.fonts.load === "function") {
      for (const [family, sample] of [[FONT_HANGUL, "대표자인"], [FONT_HANJA, CORP_TITLE]]) {
        Promise.resolve(document.fonts.load(`34px '${family}'`, sample)).catch(() => {});
      }
    }
  }

  function svg(options) {
    const plan = layout(options);
    if (!plan) return null;
    ensureFonts();
    const box = options?.inline ? "" : ` width="${SIZE}" height="${SIZE}"`;
    const rotated = plan.ring.map(c =>
      `<text x="${c.x}" y="${c.y}" font-size="${c.fs}" transform="rotate(${c.angle} ${CENTER} ${CENTER})">${escapeXml(c.ch)}</text>`).join("");
    const upright = plan.inner.map(c => `<text x="${c.x}" y="${c.y}" font-size="${c.fs}">${escapeXml(c.ch)}</text>`).join("");
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}"${box}${options?.inline ? ' class="contract-stamp"' : ""} role="img" aria-label="${escapeXml(options?.label || "도장")}">`
      + `<defs><filter id="seal-ink" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="7" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="1.6"/></filter></defs>`
      + `<g fill="${INK}" filter="url(#seal-ink)" font-family="${FONT}" font-weight="400" text-anchor="middle">`
      + `<circle cx="${CENTER}" cy="${CENTER}" r="${OUTER_R}" fill="none" stroke="${INK}" stroke-width="${OUTER_W}"/>`
      + `<circle cx="${CENTER}" cy="${CENTER}" r="${INNER_R}" fill="none" stroke="${INK}" stroke-width="${INNER_W}"/>`
      + `<circle cx="${plan.dot.x}" cy="${plan.dot.y}" r="${DOT_R}"/>`
      + `<g stroke="${INK}" stroke-width="${TEXT_STROKE}">${rotated}${upright}</g>`
      + `</g></svg>`;
  }

  // 글꼴이 들어가지 않는 data URI 판 — 대체 명조로만 그려진다. 계약서 화면은 svg({inline:true}) 를 쓴다.
  function dataUri(options) {
    const markup = svg(options);
    return markup ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(markup)}` : "";
  }

  // 계약서 값 → 사업장 정보 순으로 유형·대표자·상호를 찾는다.
  function optionsFor(contract, business) {
    const pick = (camel, snake) => String(contract?.[camel] ?? contract?.[snake] ?? "").trim();
    const biz = business || {};
    const companyName = pick("employerName", "employer_name") || String(biz.name ?? "").trim();
    const representative = pick("employerRepresentative", "employer_representative") || String(biz.representative ?? "").trim();
    const entityType = pick("employerEntityType", "employer_entity_type") || String(biz.entityType ?? biz.entity_type ?? "").trim();
    return { kind: kindFor(entityType, companyName), companyName, representative, label: `${companyName || "사용자"} 도장` };
  }

  const api = { layout, svg, dataUri, optionsFor, kindFor, ensureFonts, INK };
  root.ObysCompanySeal = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
