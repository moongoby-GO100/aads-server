"""배포 락 TTL 갱신(renew_deploy_lock) 계약.

acquire_deploy_lock 은 SET NX 라 홀더가 다시 불러도 TTL 이 늘지 않는다. 락 TTL 은
600초인데 autoheal successor 추적은 최대 2700초라, 갱신 없이는 추적 도중 락이 풀려
다른 잡이 병행 배포를 시작한다. 갱신은 소유자일 때만 성공해야 한다.
"""

import asyncio
from unittest.mock import patch

from app.services import deploy_lock


class FakeRedis:
    def __init__(self, data=None, fail=False):
        self.data = dict(data or {})
        self.ttls = {k: 100 for k in self.data}
        self.fail = fail
        self.expire_calls = []

    def get(self, key):
        if self.fail:
            raise RuntimeError("boom")
        return self.data.get(key)

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def expire(self, key, seconds):
        self.expire_calls.append((key, seconds))
        if key not in self.data:
            return False
        self.ttls[key] = seconds
        return True

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        self.ttls[key] = ex or -1
        return True


def _with(fake):
    return patch.object(deploy_lock, "_get_redis", return_value=fake)


def test_holder_renewal_extends_ttl():
    fake = FakeRedis({"deploy_lock:AADS": "runner-a"})
    with _with(fake):
        res = deploy_lock.renew_deploy_lock("AADS", "runner-a")
    assert res["renewed"] is True
    assert fake.expire_calls == [("deploy_lock:AADS", 600)]
    assert fake.ttls["deploy_lock:AADS"] == 600


def test_reacquire_by_holder_does_not_extend_ttl_which_is_why_renew_exists():
    fake = FakeRedis({"deploy_lock:AADS": "runner-a"})
    with _with(fake):
        res = deploy_lock.acquire_deploy_lock("AADS", "runner-a")
    assert res["acquired"] is False
    assert fake.ttls["deploy_lock:AADS"] == 100


def test_non_owner_cannot_renew_and_ttl_is_untouched():
    fake = FakeRedis({"deploy_lock:AADS": "runner-other"})
    with _with(fake):
        res = deploy_lock.renew_deploy_lock("AADS", "runner-a")
    assert res["renewed"] is False
    assert res["reason"] == "not_owner"
    assert res["holder"] == "runner-other"
    assert fake.expire_calls == []
    assert fake.ttls["deploy_lock:AADS"] == 100


def test_expired_lock_is_not_revived():
    fake = FakeRedis()
    with _with(fake):
        res = deploy_lock.renew_deploy_lock("AADS", "runner-a")
    assert res["renewed"] is False
    assert res["reason"] == "expired_or_released"
    assert "deploy_lock:AADS" not in fake.data


def test_key_expiring_between_get_and_expire_is_a_failure():
    class Racy(FakeRedis):
        def expire(self, key, seconds):
            self.data.pop(key, None)
            return super().expire(key, seconds)

    fake = Racy({"deploy_lock:AADS": "runner-a"})
    with _with(fake):
        res = deploy_lock.renew_deploy_lock("AADS", "runner-a")
    assert res["renewed"] is False


def test_redis_error_is_fail_closed():
    with _with(FakeRedis({"deploy_lock:AADS": "runner-a"}, fail=True)):
        res = deploy_lock.renew_deploy_lock("AADS", "runner-a")
    assert res["renewed"] is False
    assert res["reason"] == "redis_error"


def test_no_redis_mirrors_acquire_graceful_degradation():
    with _with(None):
        assert deploy_lock.acquire_deploy_lock("AADS", "runner-a")["acquired"] is True
        res = deploy_lock.renew_deploy_lock("AADS", "runner-a")
    assert res["renewed"] is True
    assert res["reason"] == "redis_unavailable_no_lock"


def test_custom_timeout_is_applied():
    fake = FakeRedis({"deploy_lock:AADS": "runner-a"})
    with _with(fake):
        deploy_lock.renew_deploy_lock("AADS", "runner-a", timeout=900)
    assert fake.expire_calls == [("deploy_lock:AADS", 900)]


def test_renew_endpoint_delegates_to_service():
    from app.api import ops

    fake = FakeRedis({"deploy_lock:AADS": "runner-a"})
    with _with(fake):
        res = asyncio.run(ops.api_renew_deploy_lock("AADS", "runner-a"))
    assert res["renewed"] is True
    assert any(
        getattr(r, "path", "") == "/ops/locks/deploy/renew" for r in ops.router.routes
    )
