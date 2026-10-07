#!/usr/bin/env python3
"""NT-BENCH-VISION-SECTIONS-20261007 — 상세페이지 벤치 캡처 AI 비전 섹션 판독.

세로로 긴 캡처를 1600px 조각으로 잘라 폭 800px JPEG 로 줄인 뒤, 조각마다
call_llm_with_fallback(images=[...]) 로 섹션 유형을 판독하고, 페이지(캡처 묶음)별
섹션 순서표로 집계한다. 분석 전용 — 운영 서비스·DB 설정은 건드리지 않는다.

  --dry-run     조각 수와 예상 비용만 출력 (LLM 호출 없음)
  --pilot       쇼핑몰별 고르게 20조각을 Haiku/Sonnet 로 판독해 dominant 일치율로 모델 선택
  --run         전수 판독 (파일럿이 고른 모델, --model 로 덮어쓰기). tiles.jsonl 에 누적·재개 가능
  --summarize   tiles.jsonl → sections_vision.json + 섹션순서표_20261007.md

호출은 app.core.anthropic_client.call_llm_with_fallback 만 쓴다 (R-AUTH). 그 함수는
Claude 실패 시 LiteLLM 으로 조용히 폴백하므로, 폴백 로그가 보이면 그 조각은 모델이
섞인 것으로 보고 결과에서 제외한다. 컨테이너 안에서 실행해야 한다 (anthropic 모듈, DB 토큰).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import io
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("NTV2_BENCH_ROOT", "/root/aads/data/ntv2-bench/20261006"))
SHOTS = ROOT / "shots"
OCR = ROOT / "ocr"
VISION = ROOT / "vision"
TILES_JSONL = VISION / "tiles.jsonl"
PILOT_JSONL = VISION / "pilot.jsonl"
PILOT_RESULT = VISION / "pilot_result.json"
SUMMARY_JSON = VISION / "sections_vision.json"
SUMMARY_MD = VISION / "섹션순서표_20261007.md"

TILE_H = 1600
TILE_W = 800
JPEG_Q = 80
MAX_TOKENS = 200
CONCURRENCY = 4
BUDGET_USD = 5.0
PILOT_N = 20
PILOT_THRESHOLD = 0.80
MAX_CONSEC_FAIL = 12
DEFAULT_MAX_SECONDS = 3500  # 바깥 `timeout 3600` 보다 먼저 스스로 멈춘다

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-5"  # app/api/visual_qa.py, ceo_chat.py 에서 쓰는 실제 id

# 코드(app/api/ceo_chat.py)의 단가표는 Haiku 0.80/4.0, Sonnet 3/15. 지시서 가정치는 Haiku 1/5.
# 어느 쪽이든 예산 통제는 보수적이어야 하므로 Haiku 는 지시서 가정치(1/5)를 쓴다.
PRICE = {HAIKU: (1.0, 5.0), SONNET: (3.0, 15.0)}  # USD per 1M tokens (in, out)

VOCAB = [
    "intro_banner", "model_full", "model_closeup", "product_flat", "detail_closeup",
    "color_option", "size_table", "fabric_info", "coordi", "point_copy",
    "notice", "review", "video_frame", "other",
]
LABEL_KO = {
    "intro_banner": "인트로/배너", "model_full": "모델 전신컷", "model_closeup": "모델 클로즈업",
    "product_flat": "제품 단독(누끼)", "detail_closeup": "디테일 클로즈업", "color_option": "컬러 옵션",
    "size_table": "사이즈표", "fabric_info": "소재·세탁 정보", "coordi": "코디 제안",
    "point_copy": "설득 문구", "notice": "배송·공지", "review": "리뷰",
    "video_frame": "영상 프레임", "other": "기타",
}

PROMPT = (
    "This is one vertical slice of a Korean fashion e-commerce product detail page. "
    "Classify what the slice shows. Allowed types (use only these exact words): "
    "intro_banner, model_full (full-body model wearing the item), model_closeup, "
    "product_flat (product alone / cut-out), detail_closeup (fabric or stitching detail), "
    "color_option, size_table, fabric_info (material/wash/thickness info table), "
    "coordi (styling suggestion / other products), point_copy (mostly persuasive text), "
    "notice (shipping/announcements), review, video_frame, other.\n"
    'Reply with ONE line of JSON only, no markdown: {"types":[...],"dominant":"<one type>",'
    '"text_lines":[up to 3 short key phrases visible in the image, original language],'
    '"has_person":true|false}'
)

log = logging.getLogger("ntv2_vision")

_CUR_TILE: contextvars.ContextVar[str | None] = contextvars.ContextVar("cur_tile", default=None)
_FALLBACK_TILES: set[str] = set()


class _FallbackSpy(logging.Handler):
    """call_llm_with_fallback 이 LiteLLM 으로 넘어간 조각을 표시한다."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith("bg_llm_last_resort_ok") or msg.startswith("litellm_bg_error"):
            tid = _CUR_TILE.get()
            if tid:
                _FALLBACK_TILES.add(tid)


def _install_spy() -> None:
    lg = logging.getLogger("app.core.anthropic_client")
    lg.setLevel(logging.INFO)
    lg.addHandler(_FallbackSpy())


# ───────────────────────── 조각 생성 ─────────────────────────

_SHOT_RE = re.compile(r"^(?P<page>.+)_(?P<idx>\d+)\.jpe?g$", re.I)


def list_tiles() -> list[dict]:
    """모든 캡처를 TILE_H 단위로 나눈 조각 메타. 페이지 전체 높이 기준 위치(page_y0)를 포함한다."""
    from PIL import Image

    shots: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for p in sorted(SHOTS.glob("*.jpg")):
        m = _SHOT_RE.match(p.name)
        if not m:
            log.warning("파일명 규칙 불일치, 건너뜀: %s", p.name)
            continue
        shots[m.group("page")].append((int(m.group("idx")), p))

    tiles: list[dict] = []
    for page in sorted(shots):
        ordered = sorted(shots[page])
        sizes = []
        for _, p in ordered:
            with Image.open(p) as im:
                sizes.append(im.size)
        page_h = sum(h for _, h in sizes)
        shop = page.split("_")[0]
        y_off = 0
        for (sidx, p), (w, h) in zip(ordered, sizes):
            n = max(1, -(-h // TILE_H))
            for t in range(n):
                y0, y1 = t * TILE_H, min(h, (t + 1) * TILE_H)
                tiles.append({
                    "tile_id": f"{p.stem}#{t:02d}", "shop": shop, "page": page,
                    "shot": p.name, "shot_idx": sidx, "tile_idx": t,
                    "y0": y0, "y1": y1, "shot_w": w, "shot_h": h,
                    "page_y0": y_off + y0, "page_y1": y_off + y1, "page_h": page_h,
                })
            y_off += h
    return tiles


def render_tile(tile: dict) -> tuple[dict, int, int]:
    """조각을 잘라 폭 TILE_W 로 줄인 JPEG(q80) image block 과 (w,h) 를 만든다."""
    from PIL import Image

    with Image.open(SHOTS / tile["shot"]) as im:
        im = im.convert("RGB")
        crop = im.crop((0, tile["y0"], im.width, tile["y1"]))
    new_h = max(1, round(crop.height * TILE_W / crop.width))
    small = crop.resize((TILE_W, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=JPEG_Q)
    block = {"type": "image", "source": {
        "type": "base64", "media_type": "image/jpeg",
        "data": base64.b64encode(buf.getvalue()).decode("ascii")}}
    return block, TILE_W, new_h


# ───────────────────────── 판독 ─────────────────────────


def parse_reply(text: str | None) -> dict | None:
    """응답에서 JSON 을 꺼내 어휘를 검증한다. 어휘 밖 dominant 는 실패로 본다."""
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    dom = obj.get("dominant")
    types = obj.get("types")
    if dom not in VOCAB or not isinstance(types, list):
        return None
    types = [t for t in types if t in VOCAB]
    if dom not in types:
        types.insert(0, dom)
    lines = obj.get("text_lines")
    lines = [str(x)[:80] for x in lines[:3]] if isinstance(lines, list) else []
    return {"types": types, "dominant": dom, "text_lines": lines,
            "has_person": bool(obj.get("has_person"))}


def est_cost(model: str, w: int, h: int, reply: str | None) -> tuple[int, int, float]:
    """입력 (w*h/750 + 프롬프트 길이/3) 토큰 근사. 출력은 응답 글자수/2 (최소 20)."""
    tin = int(w * h / 750 + len(PROMPT) / 3)
    tout = max(20, int(len(reply or "") / 2)) if reply else MAX_TOKENS // 2
    pin, pout = PRICE[model]
    return tin, tout, (tin * pin + tout * pout) / 1e6


def read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    log.warning("깨진 행 무시: %s", line[:80])
    return rows


class Runner:
    def __init__(self, model: str, out_path: Path, budget_left: float, deadline: float):
        self.model = model
        self.out_path = out_path
        self.budget_left = budget_left
        self.deadline = deadline
        self.spent = 0.0
        self.done = 0
        self.stop_reason: str | None = None
        self.consec_fail = 0
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.fh = out_path.open("a", encoding="utf-8")

    def _check_stop(self) -> bool:
        if self.stop_reason:
            return True
        if self.spent >= self.budget_left:
            self.stop_reason = f"비용 상한 도달: 이번 실행 추정 ${self.spent:.3f} ≥ 잔여 ${self.budget_left:.3f}"
        elif time.monotonic() >= self.deadline:
            self.stop_reason = "시간 상한 도달"
        return bool(self.stop_reason)

    async def one(self, tile: dict) -> dict | None:
        from app.core.anthropic_client import call_llm_with_fallback

        async with self.sem:
            if self._check_stop():
                return None
            block, w, h = await asyncio.to_thread(render_tile, tile)
            _CUR_TILE.set(tile["tile_id"])
            parsed, reply, cost, tin, tout, err = None, None, 0.0, 0, 0, None
            for attempt in range(2):  # 호출 실패·파싱 실패는 1회 재시도
                if attempt and self._check_stop():
                    break
                _FALLBACK_TILES.discard(tile["tile_id"])
                try:
                    reply = await call_llm_with_fallback(
                        PROMPT, model=self.model, images=[block], max_tokens=MAX_TOKENS)
                except Exception as e:  # noqa: BLE001
                    reply, err = None, f"{type(e).__name__}: {str(e)[:100]}"
                tin, tout, c = est_cost(self.model, w, h, reply)
                cost += c
                self.spent += c
                if tile["tile_id"] in _FALLBACK_TILES:
                    parsed, err = None, "fallback_model_used"
                    continue
                parsed = parse_reply(reply)
                if parsed:
                    err = None
                    break
                err = err or ("empty_reply" if not reply else "parse_failed")
            row = {**tile, "model": self.model, "ok": bool(parsed), "error": err,
                   "raw": None if parsed else (reply or "")[:300],
                   "est_in_tokens": tin, "est_out_tokens": tout, "est_cost_usd": round(cost, 6),
                   **(parsed or {})}
            self.fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.fh.flush()
            self.done += 1
            self.consec_fail = 0 if parsed else self.consec_fail + 1
            if self.consec_fail >= MAX_CONSEC_FAIL and not self.stop_reason:
                self.stop_reason = f"연속 {self.consec_fail}조각 실패(마지막 오류: {err}) — 호출 경로 이상으로 중단"
            if self.done % 25 == 0:
                log.info("진행 %d 조각 · 이번 실행 추정 $%.3f · 모델 %s", self.done, self.spent, self.model)
            return row

    async def run(self, tiles: list[dict]) -> list[dict]:
        res = await asyncio.gather(*(self.one(t) for t in tiles))
        self.fh.close()
        return [r for r in res if r]


def compact(path: Path) -> int:
    """같은 tile_id 는 마지막 행만 남긴다(재시도 중복 제거). 행 수를 돌려준다."""
    rows = read_rows(path)
    last: dict[str, dict] = {}
    for r in rows:
        last[r["tile_id"]] = r
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in last.values()), encoding="utf-8")
    tmp.replace(path)
    return len(last)


def pick_pilot(tiles: list[dict], n: int) -> list[dict]:
    """쇼핑몰별 고르게 n 조각 — 쇼핑몰 안에서는 등간격으로 뽑아 페이지 앞·중·뒤를 섞는다."""
    by_shop: dict[str, list[dict]] = defaultdict(list)
    for t in tiles:
        by_shop[t["shop"]].append(t)
    shops = sorted(by_shop)
    quota = {s: n // len(shops) for s in shops}
    for s in sorted(shops, key=lambda s: -len(by_shop[s]))[: n - sum(quota.values())]:
        quota[s] += 1
    picked: list[dict] = []
    for s in shops:
        pool, q = by_shop[s], min(quota[s], len(by_shop[s]))
        picked += [pool[int((i + 0.5) * len(pool) / q)] for i in range(q)] if q else []
    return picked


def total_spent() -> float:
    return sum(r.get("est_cost_usd", 0) for r in read_rows(PILOT_JSONL) + read_rows(TILES_JSONL))


async def cmd_pilot(tiles: list[dict], deadline: float, skip_sonnet: bool = False) -> None:
    picked = pick_pilot(tiles, PILOT_N)
    have = {(r["tile_id"], r["model"]) for r in read_rows(PILOT_JSONL) if r.get("ok")}
    for model in (HAIKU,) if skip_sonnet else (HAIKU, SONNET):
        todo = [t for t in picked if (t["tile_id"], model) not in have]
        # 첫 한 묶음(동시 호출 수)이 전부 실패하면 그 모델은 지금 못 쓰는 것으로 보고 나머지를 건너뛴다.
        for chunk in (todo[:CONCURRENCY], todo[CONCURRENCY:]):
            if not chunk:
                continue
            r = Runner(model, PILOT_JSONL, BUDGET_USD - total_spent(), deadline)
            res = await r.run(chunk)
            if r.stop_reason:
                log.error("파일럿 중단(%s): %s", model, r.stop_reason)
                break
            if chunk is todo[:CONCURRENCY] and res and not any(x["ok"] for x in res):
                log.error("파일럿: %s 첫 %d조각 전부 실패 — 나머지 건너뜀", model, len(res))
                break
    rows = [r for r in read_rows(PILOT_JSONL) if r.get("ok")]
    by = defaultdict(dict)
    for r in rows:
        by[r["tile_id"]][r["model"]] = r["dominant"]
    pairs = [(v[HAIKU], v[SONNET]) for v in by.values() if HAIKU in v and SONNET in v]
    agree = sum(a == b for a, b in pairs)
    rate = agree / len(pairs) if pairs else None
    ok_by_model = Counter(r["model"] for r in rows)
    if rate is None:
        chosen = HAIKU
        rationale = (f"일치율 미측정(비교 쌍 0: Haiku 성공 {ok_by_model[HAIKU]}건, Sonnet 성공 {ok_by_model[SONNET]}건). "
                     f"Sonnet 호출이 전부 실패해 기준(≥{PILOT_THRESHOLD:.0%})을 적용할 수 없다. "
                     "Haiku 로 진행하는 것은 지시서 규칙이 아니라 대체 판단이다 — 근거: Sonnet 은 호출 불가, "
                     "전수 예상비용이 Haiku ≈$2.1 / Sonnet ≈$6.4(상한 $5 초과).")
    else:
        chosen = HAIKU if rate >= PILOT_THRESHOLD else SONNET
        rationale = (f"Haiku↔Sonnet dominant 일치 {agree}/{len(pairs)} = {rate:.1%} "
                     f"{'≥' if rate >= PILOT_THRESHOLD else '<'} {PILOT_THRESHOLD:.0%} → {chosen}")
    result = {
        "pilot_tiles": len(picked), "compared_pairs": len(pairs), "agree": agree,
        "dominant_agreement": None if rate is None else round(rate, 4),
        "threshold": PILOT_THRESHOLD, "chosen_model": chosen, "rationale": rationale,
        "ok_rows_by_model": dict(ok_by_model),
        "disagreements": [{"tile_id": k, "haiku": v.get(HAIKU), "sonnet": v.get(SONNET)}
                          for k, v in by.items() if HAIKU in v and SONNET in v and v[HAIKU] != v[SONNET]],
        "est_cost_usd": round(total_spent(), 4),
        "price_note": "Haiku in $1/M·out $5/M (지시서 가정; 코드 단가표는 0.80/4.0), Sonnet in $3/M·out $15/M",
    }
    PILOT_RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("파일럿: %s", result["rationale"])


async def cmd_run(tiles: list[dict], model: str | None, deadline: float) -> None:
    if not model:
        if not PILOT_RESULT.exists():
            raise SystemExit("파일럿 결과가 없다. --pilot 먼저 실행하거나 --model 을 지정하라.")
        model = json.loads(PILOT_RESULT.read_text(encoding="utf-8"))["chosen_model"]
    if model not in PRICE:
        raise SystemExit(f"단가를 모르는 모델: {model}")
    prior = {r["tile_id"] for r in read_rows(TILES_JSONL) if r.get("ok")}
    for r in read_rows(PILOT_JSONL):  # 파일럿에서 이미 판독한 같은 모델 결과는 재사용
        if r.get("ok") and r["model"] == model and r["tile_id"] not in prior:
            with TILES_JSONL.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            prior.add(r["tile_id"])
    todo = [t for t in tiles if t["tile_id"] not in prior]
    left = BUDGET_USD - total_spent()
    log.info("전수: 모델=%s 전체 %d · 완료 %d · 남음 %d · 잔여 예산 $%.3f", model, len(tiles), len(prior), len(todo), left)
    runner = Runner(model, TILES_JSONL, left, deadline)
    await runner.run(todo)
    n = compact(TILES_JSONL)
    rows = read_rows(TILES_JSONL)
    bad = sum(1 for r in rows if not r.get("ok"))
    status = {
        "model": model, "tiles_generated": len(tiles), "tiles_rows": n, "parse_fail": bad,
        "parse_fail_rate": round(bad / n, 4) if n else None,
        "stop_reason": runner.stop_reason, "est_cost_usd_total": round(total_spent(), 4),
    }
    (VISION / "run_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("전수 종료: %s", json.dumps(status, ensure_ascii=False))


# ───────────────────────── 집계 ─────────────────────────

_NOISE = re.compile(r"[^0-9A-Za-z가-힣]")


def ocr_lines(shot: str) -> list[str]:
    p = OCR / (Path(shot).stem + ".txt")
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if len(_NOISE.sub("", ln)) >= 4:
            out.append(ln[:60])
    return out


def ocr_for_tile(row: dict) -> list[str]:
    """OCR txt 에는 좌표가 없다. 정제한 줄을 캡처 높이에 비례 배분해 조각 구간의 줄을 근사한다."""
    lines = ocr_lines(row["shot"])
    if not lines:
        return []
    a = int(len(lines) * row["y0"] / row["shot_h"])
    b = max(a + 1, int(round(len(lines) * row["y1"] / row["shot_h"])))
    return lines[a:b]


def summarize() -> None:
    rows = [r for r in read_rows(TILES_JSONL) if r.get("ok")]
    pages: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        pages[r["page"]].append(r)

    page_out = []
    for page in sorted(pages):
        ts = sorted(pages[page], key=lambda r: (r["shot_idx"], r["tile_idx"]))
        secs: list[dict] = []
        for r in ts:
            pos = round(100 * r["page_y0"] / r["page_h"], 1)
            ocr = ocr_for_tile(r)
            if secs and secs[-1]["type"] == r["dominant"]:
                s = secs[-1]
                s["end_pct"] = round(100 * r["page_y1"] / r["page_h"], 1)
                s["tiles"] += 1
            else:
                s = {"type": r["dominant"], "first_pct": pos,
                     "end_pct": round(100 * r["page_y1"] / r["page_h"], 1), "tiles": 1,
                     "vision_text": [], "ocr_text": []}
                secs.append(s)
            for x in r.get("text_lines", []):
                if x not in s["vision_text"] and len(s["vision_text"]) < 2:
                    s["vision_text"].append(x)
            for x in ocr:
                if x not in s["ocr_text"] and len(s["ocr_text"]) < 2:
                    s["ocr_text"].append(x)
        page_out.append({"shop": ts[0]["shop"], "page": page, "page_h": ts[0]["page_h"],
                         "tiles": len(ts), "sections": secs})

    def stats(pgs: list[dict]) -> dict:
        tile_cnt, first_pos, seen_in = Counter(), defaultdict(list), Counter()
        for pg in pgs:
            seen = set()
            for s in pg["sections"]:
                tile_cnt[s["type"]] += s["tiles"]
                if s["type"] not in seen:
                    seen.add(s["type"])
                    first_pos[s["type"]].append(s["first_pct"])
            seen_in.update(seen)
        total = sum(tile_cnt.values()) or 1
        pats = Counter(tuple(s["type"] for s in pg["sections"][:3]) for pg in pgs if len(pg["sections"]) >= 1)
        return {
            "pages": len(pgs),
            "type_ratio": {t: round(c / total, 4) for t, c in tile_cnt.most_common()},
            "page_coverage": {t: round(seen_in[t] / len(pgs), 3) for t, _ in tile_cnt.most_common()},
            "avg_first_pct": {t: round(sum(v) / len(v), 1) for t, v in
                              sorted(first_pos.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))},
            "top_order_patterns": [{"pattern": list(p), "pages": c} for p, c in pats.most_common(3)],
        }

    by_shop: dict[str, list[dict]] = defaultdict(list)
    for pg in page_out:
        by_shop[pg["shop"]].append(pg)
    result = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tiles_used": len(rows), "pages": len(page_out),
        "overall": stats(page_out),
        "shops": {s: stats(v) for s, v in sorted(by_shop.items())},
        "page_sections": page_out,
    }
    SUMMARY_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    SUMMARY_MD.write_text(render_md(result), encoding="utf-8")
    log.info("집계 완료: 페이지 %d · 쇼핑몰 %d → %s", len(page_out), len(by_shop), SUMMARY_MD)


def _pat(p: list[str]) -> str:
    return " → ".join(LABEL_KO[t] for t in p)


def render_md(res: dict) -> str:
    ov = res["overall"]
    md = ["# 상세페이지 벤치 섹션 순서표 (AI 비전 판독)", "",
          f"- 생성: {res['generated']} · 판독 조각 {res['tiles_used']}개 · 페이지 {res['pages']}개 · 쇼핑몰 {len(res['shops'])}곳",
          "- 섹션 = 조각(세로 1600px) dominant 유형을 순서대로 잇고 같은 유형 연속을 병합. 위치 %는 페이지 전체 높이 기준.",
          "- 문구: 비전이 읽은 문구(V)와 OCR 문구(O). OCR 은 좌표가 없어 캡처 높이에 비례 배분한 **근사**다.", ""]
    if PILOT_RESULT.exists():
        pr = json.loads(PILOT_RESULT.read_text(encoding="utf-8"))
        md += [f"- 모델 선택: {pr['rationale']}", ""]
    md += ["## 전체 공통 패턴", "", "| 유형 | 출현 비율(조각) | 페이지 포함률 | 평균 첫 등장 위치 |", "|---|---|---|---|"]
    for t, r in ov["type_ratio"].items():
        md.append(f"| {LABEL_KO[t]} | {r:.1%} | {ov['page_coverage'][t]:.0%} | {ov['avg_first_pct'].get(t, '-')}% |")
    md += ["", "상위 3개 시작 순서 패턴(페이지의 첫 3섹션):", ""]
    for i, p in enumerate(ov["top_order_patterns"], 1):
        md.append(f"{i}. {_pat(p['pattern'])} — {p['pages']}페이지")
    md += ["", "## 쇼핑몰별 패턴", "", "| 쇼핑몰 | 페이지 | 많이 나온 유형(비율) | 가장 흔한 시작 순서 |", "|---|---|---|---|"]
    for s, st in res["shops"].items():
        top = ", ".join(f"{LABEL_KO[t]} {r:.0%}" for t, r in list(st["type_ratio"].items())[:3])
        pat = _pat(st["top_order_patterns"][0]["pattern"]) if st["top_order_patterns"] else "-"
        md.append(f"| {s} | {st['pages']} | {top} | {pat} |")
    md += ["", "## 쇼핑몰별 상세", ""]
    for s, st in res["shops"].items():
        md += [f"### {s}", "", "| 유형 | 비율 | 평균 첫 등장 |", "|---|---|---|"]
        for t, r in st["type_ratio"].items():
            md.append(f"| {LABEL_KO[t]} | {r:.1%} | {st['avg_first_pct'].get(t, '-')}% |")
        md.append("")
        for i, p in enumerate(st["top_order_patterns"], 1):
            md.append(f"- 순서 패턴 {i}: {_pat(p['pattern'])} ({p['pages']}페이지)")
        md.append("")
    md += ["## 페이지별 섹션 시퀀스", ""]
    for pg in res["page_sections"]:
        md += [f"### {pg['page']}", "", "| # | 위치 | 유형 | 문구 |", "|---|---|---|---|"]
        for i, s in enumerate(pg["sections"], 1):
            txt = " / ".join([f"V:{x}" for x in s["vision_text"][:1]] + [f"O:{x}" for x in s["ocr_text"][:2]])
            md.append(f"| {i} | {s['first_pct']}% | {LABEL_KO[s['type']]} | {txt.replace('|', '/')} |")
        md.append("")
    return "\n".join(md) + "\n"


# ───────────────────────── main ─────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--pilot", action="store_true")
    g.add_argument("--run", action="store_true")
    g.add_argument("--summarize", action="store_true")
    ap.add_argument("--model", help="--run 에서 파일럿 선택을 덮어쓴다")
    ap.add_argument("--skip-sonnet", action="store_true",
                    help="--pilot: Sonnet 호출을 건너뛰고 기존 pilot.jsonl 로 결과만 계산(Sonnet 불가가 확인된 경우)")
    ap.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    VISION.mkdir(parents=True, exist_ok=True)

    if args.summarize:
        summarize()
        return 0

    tiles = list_tiles()
    if args.dry_run:
        w, h = TILE_W, round(TILE_H * TILE_W / 1280)
        per = {m: est_cost(m, w, h, "x" * 220)[2] for m in PRICE}
        shops = Counter(t["shop"] for t in tiles)
        print(f"조각 {len(tiles)}개 · 페이지 {len({t['page'] for t in tiles})}개 · 쇼핑몰별 {dict(shops)}")
        for m, c in per.items():
            print(f"  {m}: 조각당 ≈${c:.5f} → 전수 ≈${c * len(tiles):.2f} (상한 ${BUDGET_USD})")
        return 0

    _install_spy()
    deadline = time.monotonic() + args.max_seconds
    t0 = time.monotonic()
    if args.pilot:
        asyncio.run(cmd_pilot(tiles, deadline, args.skip_sonnet))
    else:
        asyncio.run(cmd_run(tiles, args.model, deadline))
    log.info("실행 시간 %.0f초", time.monotonic() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
