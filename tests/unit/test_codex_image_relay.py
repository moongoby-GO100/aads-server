import base64

from app.services import model_selector
from scripts import claude_relay_server


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def test_extract_codex_prompt_and_images_preserves_image_payload():
    prompt, images = model_selector._extract_codex_prompt_and_images([
        {"type": "text", "text": "이 이미지의 오류를 설명해줘."},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": PNG_1X1,
            },
        },
    ])

    assert "이 이미지의 오류" in prompt
    assert "첨부 이미지" in prompt
    assert images == [{
        "name": "image_2",
        "media_type": "image/png",
        "data": PNG_1X1,
    }]


def test_codex_relay_materializes_images_for_cli_image_option(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_relay_server.tempfile, "gettempdir", lambda: str(tmp_path))

    paths = claude_relay_server._materialize_codex_image_attachments(
        [{"name": "sample.png", "media_type": "image/png", "data": PNG_1X1}],
        "session-1",
    )

    assert len(paths) == 1
    assert paths[0].endswith(".png")
    assert open(paths[0], "rb").read() == base64.b64decode(PNG_1X1)
