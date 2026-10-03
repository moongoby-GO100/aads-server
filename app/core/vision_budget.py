"""LLM 요청에 실리는 이미지 base64 합계를 예산 안으로 맞춘다 (AADS-CHAT-IMAGE-DOWNSCALE-BEFORE-LLM).

relay(aiohttp 기본 client_max_size 1MiB)는 본문이 넘으면 'invalid JSON' 400 을 돌려준다.
원본 이미지는 건드리지 않고, LLM 전송용 block 만 축소·재인코딩한다.
PDF document block 은 예산 계산 대상이 아니며 그대로 통과한다.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_DIMENSION = 1568
DEFAULT_BASE64_BUDGET = 700_000
# (장변 상한 배율, 품질) — 합계가 예산을 넘을 때 순서대로 낮춘다.
_DEGRADE_STEPS: Tuple[Tuple[float, int], ...] = (
    (1.0, 85),
    (0.8, 75),
    (0.65, 65),
    (0.5, 55),
    (0.35, 45),
)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_dimension() -> int:
    return _env_int("CHAT_VISION_MAX_DIMENSION", DEFAULT_MAX_DIMENSION)


def base64_budget() -> int:
    return _env_int("CHAT_VISION_BASE64_BUDGET", DEFAULT_BASE64_BUDGET)


def _is_image_block(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") != "image":
        return False
    src = block.get("source")
    return isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str)


def _data_len(block: Dict[str, Any]) -> int:
    return len(block["source"]["data"])


def _reencode(raw: bytes, limit: int, quality: int) -> Optional[Tuple[str, str]]:
    """raw → (media_type, base64). 알파 없으면 JPEG, 있으면 WebP. 실패 시 None."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("[VISION-BUDGET] Pillow unavailable — cannot downscale")
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            work = im.convert("RGBA" if has_alpha else "RGB")
            if max(work.size) > limit:
                work.thumbnail((limit, limit), Image.LANCZOS)
            buf = io.BytesIO()
            if has_alpha:
                work.save(buf, format="WEBP", quality=quality)
                media_type = "image/webp"
            else:
                work.save(buf, format="JPEG", quality=quality, optimize=True)
                media_type = "image/jpeg"
            return media_type, base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("[VISION-BUDGET] reencode failed: %s: %s", type(e).__name__, str(e)[:200])
        return None


def _long_side(raw: bytes) -> Optional[int]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            return max(im.size)
    except Exception:
        return None


def _shrunk_block(
    block: Dict[str, Any],
    raw: bytes,
    limit: int,
    quality: int,
    must_shrink: bool,
) -> Dict[str, Any]:
    """must_shrink 이면 결과가 더 작을 때만 교체한다. 실패하면 원본 block 그대로."""
    result = _reencode(raw, limit, quality)
    if result is None:
        return block
    media_type, data = result
    if must_shrink and len(data) >= _data_len(block):
        return block
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def fit_image_blocks(
    blocks: Sequence[Dict[str, Any]],
    names: Optional[Sequence[Optional[str]]] = None,
    *,
    max_dim: Optional[int] = None,
    budget: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """vision block 목록을 (축소된 block 목록, 제외 안내문) 으로 돌려준다.

    - 장변이 max_dim 을 넘거나 단독으로 budget 을 넘는 이미지만 재인코딩한다.
      그 밖의 작은 이미지는 같은 객체 그대로 통과한다.
    - 이미지 base64 합계가 budget 을 넘으면 모든 이미지를 한 단계씩 더 줄인다.
    - 끝내 넘으면 마지막 이미지부터 뺀다. 뺀 이미지는 안내문에 이름과 함께 남는다.
    - 입력 block 은 변경하지 않는다. 안내문이 비어 있으면 제외된 이미지가 없다.
    """
    max_dim = max_dim or max_dimension()
    budget = budget or base64_budget()
    out: List[Dict[str, Any]] = list(blocks)
    labels = [
        (names[i] if names and i < len(names) and names[i] else f"이미지 {i + 1}")
        for i in range(len(out))
    ]
    img_idx = [i for i, b in enumerate(out) if _is_image_block(b)]
    if not img_idx:
        return out, ""

    raws: Dict[int, bytes] = {}
    for i in img_idx:
        try:
            raws[i] = base64.b64decode(out[i]["source"]["data"])
        except Exception:
            logger.warning("[VISION-BUDGET] base64 decode failed: %s", labels[i])

    def _total() -> int:
        return sum(_data_len(out[i]) for i in img_idx if out[i] is not None)

    before = _total()

    # 1단계: 큰 이미지만 줄인다.
    for i in img_idx:
        if i not in raws:
            continue
        side = _long_side(raws[i])
        oversized = side is None or side > max_dim
        if oversized or _data_len(out[i]) > budget:
            out[i] = _shrunk_block(out[i], raws[i], max_dim, _DEGRADE_STEPS[0][1], must_shrink=not oversized)

    # 2단계: 합계가 넘으면 전부 단계적으로 낮춘다.
    for scale, quality in _DEGRADE_STEPS[1:]:
        if _total() <= budget:
            break
        limit = max(64, int(max_dim * scale))
        for i in img_idx:
            if i in raws:
                out[i] = _shrunk_block(out[i], raws[i], limit, quality, must_shrink=True)

    # 3단계: 그래도 넘으면 마지막 이미지부터 뺀다.
    excluded: List[str] = []
    keep = list(img_idx)
    while keep and sum(_data_len(out[i]) for i in keep) > budget:
        dropped = keep.pop()
        excluded.append(labels[dropped])
    dropped_set = set(img_idx) - set(keep)
    final = [b for i, b in enumerate(out) if i not in dropped_set]
    excluded.reverse()

    after = sum(_data_len(out[i]) for i in keep)
    if after != before or excluded:
        logger.info(
            "[VISION-BUDGET] images=%d base64 %d→%d budget=%d excluded=%d",
            len(img_idx), before, after, budget, len(excluded),
        )
    notice = f"[이미지 {len(excluded)}개는 크기 한도로 제외: {', '.join(excluded)}]" if excluded else ""
    return final, notice
