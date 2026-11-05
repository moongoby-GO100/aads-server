"""Detached entrypoint that runs one document cross review outside a short-lived MCP bridge process."""
import argparse
import asyncio
import logging
import signal
import sys

from app.core.db_pool import close_pool, get_pool, init_pool
from app.services import doc_cross_review

logger = logging.getLogger(__name__)

_HEAD_SQL = (
    "SELECT *,EXISTS(SELECT 1 FROM project_document_goal_links gl WHERE gl.head_id=project_document_heads.id) AS goal_linked "
    "FROM project_document_heads WHERE tenant_id=$1::uuid AND project_key=$2 AND document_key=$3"
)


async def run_worker(tenant, project, document_key, revision_id, follow_up=False):
    await init_pool()
    try:
        async with get_pool().acquire() as conn:
            head = await conn.fetchrow(_HEAD_SQL, tenant, project, document_key)
            if not head:
                logger.warning('doc_cross_review_worker_head_missing document_key=%s', document_key)
                return 'head_missing'
            if str(head['latest_revision_id']) != str(revision_id):
                logger.warning('doc_cross_review_worker_not_latest document_key=%s revision_id=%s', document_key, revision_id)
                return 'not_latest'
            revision = await conn.fetchrow('SELECT * FROM project_document_revisions WHERE head_id=$1 AND id=$2::uuid',
                                           head['id'], str(revision_id))
            if not revision:
                logger.warning('doc_cross_review_worker_revision_missing revision_id=%s', revision_id)
                return 'revision_missing'
        if follow_up:
            await doc_cross_review.execute_review(tenant, project, dict(head), dict(revision), follow_up=True)
        else:
            await doc_cross_review.execute_review(tenant, project, dict(head), dict(revision))
        return 'executed'
    finally:
        await close_pool()


# Slightly below the parent's `timeout 1500` so the worker records its own timeout before SIGTERM arrives.
_DEADLINE_SECONDS = 1440


async def _guarded(args):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError):
        logger.warning('doc_cross_review_worker_sigterm_handler_unavailable')
    logger.warning('doc_cross_review_worker_started document_key=%s revision_id=%s', args.document_key, args.revision_id)
    try:
        extra = {'follow_up': True} if getattr(args, 'follow_up', False) else {}
        result = await asyncio.wait_for(
            run_worker(args.tenant, args.project, args.document_key, args.revision_id, **extra), timeout=_DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        logger.error('doc_cross_review_worker_timeout document_key=%s revision_id=%s limit=%ss',
                     args.document_key, args.revision_id, _DEADLINE_SECONDS)
        return 124
    except asyncio.CancelledError:
        logger.warning('doc_cross_review_worker_cancelled document_key=%s revision_id=%s',
                       args.document_key, args.revision_id)
        return 143
    except Exception:
        logger.exception('doc_cross_review_worker_failed document_key=%s revision_id=%s',
                         args.document_key, args.revision_id)
        return 1
    finally:
        try:
            loop.remove_signal_handler(signal.SIGTERM)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    logger.warning('doc_cross_review_worker_finished document_key=%s revision_id=%s result=%s',
                   args.document_key, args.revision_id, result)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--document-key', required=True)
    parser.add_argument('--revision-id', required=True)
    parser.add_argument('--follow-up', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format='%(asctime)s pid=%(process)d %(levelname)s %(name)s %(message)s')
    return asyncio.run(_guarded(args))


if __name__ == '__main__':
    sys.exit(main())
