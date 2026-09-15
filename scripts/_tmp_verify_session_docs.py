"""배포 검증 — 운영 컨테이너에서 세션 문서 수집 결과를 본다. 확인 후 삭제."""
import asyncio
import json
import sys
import uuid

sys.path.insert(0, "/app")


async def main() -> None:
    from app.core.db_pool import init_pool
    from app.services.session_documents import collect_session_documents

    await init_pool()
    sid = sys.argv[1]
    out = await collect_session_documents(uuid.UUID(sid), limit=80)
    print("sources_used:", out["sources_used"], "| other_files:", out["other_files"],
          "| documents:", len(out["documents"]))
    for d in out["documents"]:
        print(f"  {d['icon']} {d['name']}  [{d['source_label']}]  {d['dir']}  link={'Y' if d['view_url'] else 'N'}")


asyncio.run(main())
