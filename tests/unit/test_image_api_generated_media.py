import os
from pathlib import Path

import pytest
from fastapi.responses import FileResponse

os.environ.setdefault("JWT_SECRET_KEY", "test-only-generated-media-secret-key")

from app.api import image as image_api
from app.core import db_pool


class _Acquire:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, exc_type, exc, tb): return False


class _Pool:
    def __init__(self, row): self.row = row
    def acquire(self): return _Acquire(self)
    async def fetchrow(self, query, job_id):
        assert "result_path" in query
        assert job_id == "media-inline123"
        return self.row


@pytest.mark.asyncio
async def test_gallery_image_streams_externalized_local_file(monkeypatch, tmp_path: Path):
    image_path = tmp_path / "media" / "generated" / "image" / "media-inline123.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png-bytes")
    pool = _Pool({"result_uri": "/api/v1/image/gallery/media-inline123/image", "result_path": str(image_path)})
    monkeypatch.setenv("AADS_MEDIA_STATIC_DIR", str(tmp_path))
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    response = await image_api.get_gallery_image("media-inline123")
    assert isinstance(response, FileResponse)
    assert Path(response.path) == image_path
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400, immutable"


def test_gallery_image_get_is_public_but_generation_remains_admin_only():
    routes = {(route.path, ",".join(sorted(route.methods or []))): route for route in image_api.router.routes}
    image_route = routes[("/gallery/{job_id}/image", "GET")]
    generate_route = routes[("/generate", "POST")]

    assert image_route.dependencies == []
    assert len(generate_route.dependencies) == 1
