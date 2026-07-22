from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import unni_naengmyeon as api


class _FakeConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self):
        self.connection = _FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _client(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(api, "get_pool", lambda: pool)
    api._request_times.clear()
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    return TestClient(app), pool


def test_public_inquiry_is_stored_and_returns_reference(monkeypatch):
    client, pool = _client(monkeypatch)

    response = client.post(
        "/api/v1/unni-naengmyeon/inquiries",
        json={
            "name": " 홍 길동 ",
            "contact": "010-1234-5678",
            "subject": "단체 주문 문의",
            "message": "내일 점심 단체 주문이 가능한지 문의드립니다.",
            "privacy_consent": True,
            "website": "",
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "received"
    assert response.json()["reference"]
    assert len(pool.connection.calls) == 1
    assert pool.connection.calls[0][1][1:] == (
        "홍 길동",
        "010-1234-5678",
        "단체 주문 문의",
        "내일 점심 단체 주문이 가능한지 문의드립니다.",
    )


def test_inquiry_requires_privacy_consent(monkeypatch):
    client, pool = _client(monkeypatch)

    response = client.post(
        "/api/v1/unni-naengmyeon/inquiries",
        json={
            "name": "고객",
            "contact": "customer@example.com",
            "message": "메뉴 알레르기 정보를 문의드립니다.",
            "privacy_consent": False,
        },
    )

    assert response.status_code == 422
    assert pool.connection.calls == []


def test_honeypot_submission_is_not_persisted(monkeypatch):
    client, pool = _client(monkeypatch)

    response = client.post(
        "/api/v1/unni-naengmyeon/inquiries",
        json={
            "name": "bot",
            "contact": "010-0000-0000",
            "message": "자동으로 작성된 광고 문의입니다.",
            "privacy_consent": True,
            "website": "https://spam.example",
        },
    )

    assert response.status_code == 201
    assert response.json()["reference"] == ""
    assert pool.connection.calls == []
