"""R-DOC mockup HTML asset import API. 목업 HTML 한 장을 asset 으로 가져오는 단일 라우터 (기존 mockup_reviews 와 분리)."""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.canonical_documents import _authorize, _project
from app.api.mockup_reviews import WRITE
from app.core.db_pool import get_pool
from app.services import mockup_html_import
from app.services import mockup_review_service as svc

router = APIRouter(prefix="/projects/{project_key}/mockup-assets", tags=["mockup-assets"])


_HTML_IMPORT_BODY = {"requestBody": {"required": True, "content": {
    "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
        "file": {"type": "string", "format": "binary", "description": ".html 또는 text/html 파일"}}}},
    "application/json": {"schema": {"type": "object", "required": ["source_url"], "properties": {
        "source_url": {"type": "string", "format": "uri",
                       "description": "https 주소. aads.newtalk.kr, v2.newtalk.kr, MOCKUP_HTML_IMPORT_HOSTS 만 허용"}}}},
}}}


async def _html_input(request: Request) -> tuple[bytes | None, str | None]:
    """-> (uploaded bytes, source_url); exactly one of the two is set."""
    limit = svc.MAX_ASSET_BYTES + 1024 * 1024
    if int(request.headers.get("content-length") or 0) > limit:
        svc.fail(413, "asset_too_large", max_bytes=svc.MAX_ASSET_BYTES, message="HTML 이 25MB 를 넘어 등록할 수 없습니다")
    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        form = await request.form()
        upload, url = form.get("file"), form.get("source_url")
        if upload is not None and not hasattr(upload, "read"):
            svc.fail(422, "html_source_required", message="file 은 파일이어야 합니다")
        if upload is not None:
            name = (getattr(upload, "filename", "") or "").lower()
            ctype = (getattr(upload, "content_type", "") or "").split(";")[0].strip().lower()
            if not (name.endswith(mockup_html_import.HTML_SUFFIXES) or ctype == "text/html"):
                svc.fail(415, "html_file_required", message=".html 파일 또는 text/html 만 올릴 수 있습니다")
            if url:
                svc.fail(422, "html_source_conflict", message="file 과 source_url 은 하나만 보내 주세요")
            return await upload.read(svc.MAX_ASSET_BYTES + 1), None
        return None, str(url) if url else None
    try:
        body = await request.json()
    except ValueError:
        svc.fail(422, "invalid_json", message="요청 본문이 올바른 JSON 이 아닙니다")
    url = body.get("source_url") if isinstance(body, dict) else None
    return None, url if isinstance(url, str) and url.strip() else None


@router.post("/html", status_code=201, openapi_extra=_HTML_IMPORT_BODY)
async def import_html_asset(project_key: str, request: Request, response: Response, context: dict = WRITE):
    """HTML 파일(업로드) 또는 자사 URL 을 목업 asset 으로 가져온다. 응답은 manifest assets 항목에 쓸 수 있는 값이다."""
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        await _authorize(conn, context, project, "write")
    data, url = await _html_input(request)
    if data is None and url is None:
        svc.fail(422, "html_source_required",
                 message="HTML 파일(file) 또는 source_url 중 하나를 보내 주세요")
    if data is not None:
        asset, created = await mockup_html_import.import_from_bytes(project, data)
    else:
        asset, created = await mockup_html_import.import_from_url(project, url)
    if not created:
        response.status_code = 200
    return asset
