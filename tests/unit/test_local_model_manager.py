from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.local_model_manager import LocalModelManager


def _write_queue(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "ollama": [
                    {"priority": 1, "model": "qwen3:4b", "route_model_id": "pc-qwen3-4b"},
                ],
                "transformers": [
                    {"priority": 2, "model": "Qwen/Qwen3-Embedding-0.6B", "bridge": "local_embedding", "task": "embedding"},
                ],
                "document": [
                    {"priority": 1, "model": "tesseract-5", "bridge": "local_document", "task": "ocr_text"},
                ],
                "media": [
                    {"priority": 2, "model": "Lightricks/LTX-Video", "bridge": "local_video", "task": "text_or_image_to_video"},
                    {"priority": 3, "model": "Tencent-Hunyuan/Hunyuan3D-2.1", "bridge": "local_3d", "task": "image_to_3d"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_local_model_manager_lists_canonical_queue(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    _write_queue(queue_path)
    manager = LocalModelManager(queue_path=queue_path)

    items = manager.list_queue()

    assert [item["priority"] for item in items] == [1, 1, 2, 2, 3]
    assert items[0]["item_id"] == "pc_ollama:pc-qwen3-4b"
    assert manager.find_item(item_id="local_document:tesseract-5")["model"] == "tesseract-5"


def test_local_model_manager_resolves_media_model(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    _write_queue(queue_path)
    manager = LocalModelManager(queue_path=queue_path)

    video = manager.resolve_media_model(kind="video", provider="pc_local")
    model_3d = manager.resolve_media_model(kind="model_3d", model_id="Tencent-Hunyuan/Hunyuan3D-2.1")

    assert video["bridge"] == "local_video"
    assert video["model"] == "Lightricks/LTX-Video"
    assert model_3d["bridge"] == "local_3d"


@pytest.mark.asyncio
async def test_local_model_install_requires_single_known_item(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    _write_queue(queue_path)
    manager = LocalModelManager(queue_path=queue_path)

    result = await manager.run_install_test(item_id=["a", "b"])  # type: ignore[arg-type]
    missing = await manager.run_install_test(item_id="missing")

    assert result["error_code"] == "SINGLE_ITEM_REQUIRED"
    assert missing["error_code"] == "QUEUE_ITEM_NOT_FOUND"
