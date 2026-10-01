"""로컬 OCR 브릿지 — CEO PC Agent ocr_extract 또는 서버 tesseract.

OCR_BACKEND 가 경로를 고른다.

    auto      (기본) PC Agent 를 먼저 쓰고, 붙은 에이전트가 없거나 실패하면 tesseract
    pc_agent  PC Agent 만 쓴다 (2026-10-01 이전 동작)
    local     서버에 깔린 tesseract 만 쓴다

PC Agent 는 대표님 PC 가 켜져 있을 때만 붙는다. 그래서 PC Agent 가 없는
호스트에서는 서버 OCR 기능이 통째로 죽는다 — 2026-10-01 진아서버
(5.104.85.244) 의 사업자등록증 업로드가 그랬다. 127.0.0.1:8102 가 없어
OCR 호출이 전부 실패하고 4항목 손입력만 가능했다. 그 호스트에는
tesseract 5.3.4 (kor+eng) 가 이미 깔려 있었으므로 이 경로로 연결한다.

OCR 결과는 제안용이다. 값을 만들어내지 않고, 읽지 못하면 읽지 못했다고 돌려준다.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PC_AGENT_URL = os.getenv("PC_AGENT_BASE_URL", "http://127.0.0.1:8102/api/v1/pc-agent")
DEFAULT_LANGUAGE = "kor+eng"
# 사업자등록증은 단일 컬럼 서식이다. 2026-10-01 진아서버 실측에서
# psm 3(자동)은 7줄 중 2항목만 읽었고 psm 4·6 은 6항목 전부(신뢰도 0.92)를
# 읽었다. psm 12 는 1항목이었다. 그래서 기본값을 4 로 둔다.
DEFAULT_PSM = "4"
BACKENDS = ("auto", "pc_agent", "local")


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def backend() -> str:
    """OCR_BACKEND 값. 모르는 값이면 auto 로 본다."""
    value = _env("OCR_BACKEND", "auto").lower()
    return value if value in BACKENDS else "auto"


def _timeout() -> int:
    try:
        return max(5, int(_env("OCR_LOCAL_TIMEOUT_SEC", "120")))
    except ValueError:
        return 120


# --- 서버 tesseract ----------------------------------------------------------
def _tesseract_bin() -> str | None:
    return shutil.which(_env("OCR_TESSERACT_BIN", "tesseract"))


def _installed_langs(binary: str) -> set[str]:
    try:
        done = subprocess.run(
            [binary, "--list-langs"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    # 첫 줄은 "List of available languages ..." 안내문이다.
    return {line.strip() for line in done.stdout.splitlines()[1:] if line.strip()}


def select_langs(requested: str, installed: set[str]) -> str:
    """요청한 언어 중 실제로 깔린 것만 남긴다. 하나도 없으면 eng 로 떨어진다."""
    wanted = [item.strip() for item in str(requested or "").split("+") if item.strip()]
    usable = [item for item in wanted if item in installed]
    if usable:
        return "+".join(usable)
    if "eng" in installed:
        return "eng"
    return sorted(installed)[0] if installed else "eng"


def parse_tsv(tsv: str) -> tuple[str, float]:
    """tesseract TSV → (줄 단위 텍스트, 평균 신뢰도 0~1).

    사업자등록증은 표 형식이라 줄 구분이 중요하다. 파서가 "항목 표시 다음 줄에
    값" 형태를 읽으므로 단어를 line_num 단위로 묶어 줄을 복원한다.
    """
    rows = str(tsv or "").splitlines()
    if not rows:
        return "", 0.0
    header = rows[0].split("\t")
    try:
        index = {
            name: header.index(name)
            for name in ("block_num", "par_num", "line_num", "conf", "text")
        }
    except ValueError:
        return "", 0.0

    grouped: dict[tuple[str, str, str], list[str]] = {}
    order: list[tuple[str, str, str]] = []
    scores: list[float] = []
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) <= index["text"]:
            continue
        word = cols[index["text"]].strip()
        if not word:
            continue
        try:
            score = float(cols[index["conf"]])
        except ValueError:
            continue
        if score < 0:
            continue
        key = (cols[index["block_num"]], cols[index["par_num"]], cols[index["line_num"]])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(word)
        scores.append(score)

    text = "\n".join(" ".join(grouped[key]) for key in order)
    confidence = round(sum(scores) / len(scores) / 100, 3) if scores else 0.0
    return text, confidence


def _to_grayscale_png(data: bytes) -> bytes | None:
    """원본을 그대로 넘기면 한글 라벨을 놓친다.

    2026-10-01 진아 실측(열정국밥 성신여대점 등록증 JPEG, 2008x2844, psm 4 동일):
    원본 그대로는 6항목 중 2항목(사업자등록번호·주소)만 읽었고, 흑백 PNG 로
    변환하면 6항목 전부(대표자·개업연월일 포함)를 읽었다. 확대(2~3배)는
    오히려 신뢰도가 0.82 에서 0.58 로 떨어졌으므로 배율은 건드리지 않는다.

    Pillow 가 없거나 이미지를 열지 못하면 None 을 돌려주고 원본으로 진행한다.
    """
    if _env("OCR_PREPROCESS", "1") == "0":
        return None
    try:
        from PIL import Image
    except ImportError:
        logger.info("OCR 전처리 건너뜀 — Pillow 없음")
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("L").save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 — 전처리 실패는 원본으로 계속한다
        logger.info("OCR 전처리 건너뜀 — 이미지를 열지 못했습니다: %s", exc)
        return None


def _pdf_to_png(data: bytes, workdir: Path) -> bytes | None:
    """PDF 첫 장만 PNG 로. tesseract 는 PDF 를 직접 읽지 못한다."""
    binary = shutil.which(_env("OCR_PDFTOPPM_BIN", "pdftoppm"))
    if not binary:
        return None
    source = workdir / "input.pdf"
    source.write_bytes(data)
    prefix = workdir / "page"
    try:
        subprocess.run(
            [binary, "-png", "-r", _env("OCR_PDF_DPI", "300"),
             "-f", "1", "-l", "1", str(source), str(prefix)],
            capture_output=True, timeout=_timeout(), check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pages = sorted(workdir.glob("page*.png"))
    return pages[0].read_bytes() if pages else None


def tesseract_extract(data: bytes, language: str = DEFAULT_LANGUAGE) -> dict:
    """동기 호출. ocr_extract 가 스레드로 돌린다."""
    binary = _tesseract_bin()
    if not binary:
        raise RuntimeError("tesseract 실행 파일이 없습니다 — 서버 OCR 불가")
    if not data:
        raise ValueError("OCR 대상 데이터가 비어 있습니다")
    langs = select_langs(language, _installed_langs(binary))
    with tempfile.TemporaryDirectory(prefix="obys-ocr-") as tmp:
        workdir = Path(tmp)
        payload = data
        if payload[:4] == b"%PDF":
            converted = _pdf_to_png(payload, workdir)
            if not converted:
                raise RuntimeError("PDF 를 이미지로 변환하지 못했습니다 — 이미지 파일로 올려 주십시오")
            payload = converted
        prepared = _to_grayscale_png(payload)
        if prepared:
            payload = prepared
        source = workdir / "input.bin"
        source.write_bytes(payload)
        done = subprocess.run(
            [binary, str(source), "stdout", "-l", langs,
             "--psm", _env("OCR_TESSERACT_PSM", DEFAULT_PSM), "tsv"],
            capture_output=True, text=True, timeout=_timeout(),
        )
    if done.returncode != 0:
        detail = (done.stderr or "").strip().replace("\n", " ")[:200]
        raise RuntimeError(f"tesseract 실패(rc={done.returncode}): {detail}")
    text, confidence = parse_tsv(done.stdout)
    return {
        "text": text,
        "confidence": confidence,
        "language": langs,
        "error": None,
        "backend": "local",
        "engine": "tesseract",
    }


# --- PaddleOCR 폴백 (저해상도·저신뢰 전용) -------------------------------------
# 2026-10-01 진아244 실측: tesseract 10/12, PaddleOCR 원본 7/12, PaddleOCR+줄 병합 10/12.
# 595x835 등록증에서 tesseract 는 '서울'을 'ASSIA' 로 틀렸고 PaddleOCR 은 맞혔다.
# 동점에 20~59초(tesseract 1~2초)라 전면 교체하지 않고 폴백으로만 쓴다.
# paddleocr 는 requirements 에 없다 — 진아 전용 venv 에만 있고, 없으면 폴백만 꺼진다.
# 오류 사전 obys.paddleocr_cpu_setup_three_traps:
#   1) 버전을 고정하지 않는다  2) enable_mkldnn=False 필수  3) 장변 1600px 선축소(없으면 29.7GB OOM)
PADDLE_MAX_LONG_SIDE = 1600
LEGAL_FIELDS = ("registration_no", "name", "representative", "opened_at")
_paddle_engines: dict[str, object] = {}
_paddle_state = {"available": True}


def paddle_available() -> bool:
    """초기화에 한 번 실패하면 프로세스가 끝날 때까지 폴백을 끈다(매 요청 재시도 방지)."""
    return _paddle_state["available"]


def fallback_engine() -> str:
    """OCR_FALLBACK_ENGINE. 기본 off, 알려진 값은 paddle 뿐이다."""
    value = _env("OCR_FALLBACK_ENGINE", "off").lower()
    return "paddle" if value in ("paddle", "paddleocr") else "off"


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def merge_boxes_to_lines(items: list[dict], tol_ratio: float = 0.6) -> str:
    """텍스트 박스(text/score/y/x/h) → 줄 단위 텍스트.

    PaddleOCR 은 박스 단위로 돌려주지만 parse_registration_text 는 라벨과 값이 같은 줄에
    있다고 가정한다. y 중심을 중앙값 높이 x tol_ratio 허용오차로 묶고 x 순으로 이어 붙인다.
    """
    boxes = [item for item in items if str(item.get("text") or "").strip()]
    if not boxes:
        return ""
    heights = sorted(float(item.get("h") or 0) for item in boxes)
    median_h = heights[len(heights) // 2]
    tolerance = median_h * tol_ratio
    rows: list[dict] = []
    for item in sorted(boxes, key=lambda b: float(b.get("y") or 0)):
        y = float(item.get("y") or 0)
        row = rows[-1] if rows else None
        if row is not None and abs(y - row["y"]) <= tolerance:
            row["items"].append(item)
            row["y"] = sum(float(b.get("y") or 0) for b in row["items"]) / len(row["items"])
        else:
            rows.append({"y": y, "items": [item]})
    return "\n".join(
        " ".join(str(b["text"]).strip() for b in sorted(row["items"], key=lambda b: float(b.get("x") or 0)))
        for row in rows
    )


def _paddle_engine(language: str):
    """지연 import + 인스턴스 캐시. 실패하면 예외를 그대로 올린다(호출부가 폴백을 끈다)."""
    engine = _paddle_engines.get(language)
    if engine is None:
        try:
            from paddleocr import PaddleOCR  # noqa: PLC0415 — 진아 전용 venv 에만 있다

            engine = PaddleOCR(
                lang=language,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )
        except Exception:
            _paddle_state["available"] = False
            raise
        _paddle_engines[language] = engine
    return engine


def _downscale_for_paddle(data: bytes):
    """장변 PADDLE_MAX_LONG_SIDE 이하로 줄인 RGB numpy 배열."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        long_side = max(image.size)
        if long_side > PADDLE_MAX_LONG_SIDE:
            ratio = PADDLE_MAX_LONG_SIDE / long_side
            image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))))
        return np.array(image)


def _paddle_items(prediction) -> list[dict]:
    items: list[dict] = []
    for page in prediction or []:
        data = page.get("res", page) if hasattr(page, "get") else {}
        texts = data.get("rec_texts") or []
        scores = data.get("rec_scores") or []
        polys = data.get("rec_polys")
        if polys is None:
            polys = data.get("dt_polys")
        if polys is None:
            polys = []
        for index, text in enumerate(texts):
            if index >= len(polys):
                break
            points = [(float(p[0]), float(p[1])) for p in polys[index]]
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            items.append({
                "text": str(text),
                "score": float(scores[index]) if index < len(scores) else 0.0,
                "x": min(xs),
                "y": (min(ys) + max(ys)) / 2,
                "h": max(ys) - min(ys),
            })
    return items


def paddle_extract(data: bytes, language: str = "korean") -> dict:
    """동기 호출. PaddleOCR 결과를 tesseract 와 같은 모양으로 돌려준다."""
    if not data:
        raise ValueError("OCR 대상 데이터가 비어 있습니다")
    engine = _paddle_engine(language)
    image = _downscale_for_paddle(data)
    items = _paddle_items(engine.predict(image))
    scores = [item["score"] for item in items]
    return {
        "text": merge_boxes_to_lines(items),
        "confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "language": language,
        "error": None,
        "backend": "local",
        "engine": "paddle",
    }


def _long_side(data: bytes) -> int | None:
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(io.BytesIO(data)) as image:
            return max(image.size)
    except Exception:  # noqa: BLE001 — 크기를 모르면 크기 조건은 판정하지 않는다
        return None


def _legal_filled(text: str, confidence: float) -> int:
    try:
        from app.services.obys_registration_ocr import parse_registration_text  # noqa: PLC0415 — 순환 import 회피

        parsed = parse_registration_text(text, confidence)
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for field in LEGAL_FIELDS if parsed.get(field, {}).get("valid"))


def _should_try_fallback(data: bytes, result: dict, filled: int) -> bool:
    if _float_env("OCR_FALLBACK_MIN_CONFIDENCE", 0.80) > float(result.get("confidence") or 0.0):
        return True
    side = _long_side(data)
    if side is not None and side < _float_env("OCR_FALLBACK_MIN_LONG_SIDE", 1200):
        return True
    return filled < 3


def _with_fallback(data: bytes, language: str, primary: dict) -> dict:
    """tesseract 결과가 약하면 PaddleOCR 로 재시도해 법정 항목이 더 많이 채워진 쪽을 쓴다."""
    if fallback_engine() == "off" or not paddle_available():
        return primary
    primary_text, primary_conf = str(primary.get("text") or ""), primary.get("confidence") or 0.0
    primary_filled = _legal_filled(primary_text, primary_conf)
    if not _should_try_fallback(data, primary, primary_filled):
        return primary
    try:
        candidate = paddle_extract(data, "korean")
    except Exception as exc:  # noqa: BLE001 — ImportError 포함, 폴백만 꺼지고 기본 경로는 유지
        logger.info("PaddleOCR 폴백 건너뜀: %s", str(exc)[:200])
        return primary
    candidate_filled = _legal_filled(str(candidate.get("text") or ""), candidate.get("confidence") or 0.0)
    if candidate_filled > primary_filled:
        return candidate
    return primary


# --- CEO PC Agent ------------------------------------------------------------
async def _get_agent_id() -> str | None:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{PC_AGENT_URL}/agents")
        agents = r.json().get("agents", [])
        return agents[0]["agent_id"] if agents else None


async def pc_agent_extract(
    image_url: str | None = None,
    image_base64: str | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> dict:
    agent_id = await _get_agent_id()
    if not agent_id:
        raise RuntimeError("PC Agent 연결 없음 — OCR 불가")

    params: dict = {"language": language}
    if image_url:
        params["image_url"] = image_url
    elif image_base64:
        params["image_base64"] = image_base64
    else:
        raise ValueError("image_url 또는 image_base64 필요")

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"{PC_AGENT_URL}/route-execute",
            json={
                "agent_id": agent_id,
                "command_type": "ocr_extract",
                "params": params,
            },
        )
        r.raise_for_status()
        body = r.json()

    result = body.get("result", {}).get("result", {})
    return {
        "text": result.get("text", ""),
        "confidence": result.get("confidence", 0.0),
        "language": result.get("language", language),
        "error": result.get("error"),
        "backend": "pc_agent",
        "engine": "pc_agent",
    }


async def _load_bytes(image_url: str | None, image_base64: str | None) -> bytes:
    if image_base64:
        try:
            return base64.b64decode(image_base64)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"image_base64 디코딩 실패: {exc}") from exc
    if str(image_url or "").startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(str(image_url))
            r.raise_for_status()
            return r.content
    raise ValueError("서버 OCR 은 image_base64 또는 http(s) image_url 만 지원합니다")


async def ocr_extract(
    image_url: str | None = None,
    image_base64: str | None = None,
    language: str = DEFAULT_LANGUAGE,
) -> dict:
    if not image_url and not image_base64:
        raise ValueError("image_url 또는 image_base64 필요")

    chosen = backend()
    failures: list[str] = []

    if chosen in ("auto", "pc_agent"):
        try:
            return await pc_agent_extract(image_url, image_base64, language)
        except Exception as exc:  # noqa: BLE001 — auto 는 서버 OCR 로 넘어간다
            if chosen == "pc_agent":
                raise
            failures.append(f"pc_agent: {str(exc)[:120]}")
            logger.info("PC Agent OCR 실패 — 서버 tesseract 로 넘어갑니다: %s", exc)

    data = await _load_bytes(image_url, image_base64)
    try:
        result = await asyncio.to_thread(tesseract_extract, data, language)
        result.setdefault("engine", "tesseract")
        if fallback_engine() == "off":
            return result
        return await asyncio.to_thread(_with_fallback, data, language, result)
    except Exception as exc:  # noqa: BLE001 — 실패 경로를 모아서 알린다
        failures.append(f"local: {str(exc)[:120]}")
        raise RuntimeError("OCR 실패 — " + " / ".join(failures)) from exc
