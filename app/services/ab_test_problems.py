"""A/B 벤치마크 문제 목록 — 5단계 × 5문제 = 25문제"""

PROBLEMS = [
    # ── Lv1: 기초 ──────────────────────────────────────────────────────────────
    {
        "id": "lv1_1",
        "level": 1,
        "title": "리스트 교집합 O(n)",
        "category": "algorithm",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "두 정수 리스트 a, b의 교집합을 O(n) 시간복잡도로 반환하는 함수 `intersect(a, b)`를 작성하세요.\n"
            "- 중복 원소는 최솟값만큼만 포함 (예: a=[1,1,2], b=[1,2,2] → [1,2])\n"
            "- 정렬은 불필요\n"
            "- 타입 힌트 포함"
        ),
    },
    {
        "id": "lv1_2",
        "level": 1,
        "title": "팰린드롬 검사",
        "category": "algorithm",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "문자열이 팰린드롬인지 검사하는 함수 `is_palindrome(s: str) -> bool`을 작성하세요.\n"
            "- 영문자·숫자만 고려, 대소문자 무시\n"
            "- 공백·특수문자 제거 후 판별\n"
            "- 단위 테스트 3개 포함"
        ),
    },
    {
        "id": "lv1_3",
        "level": 1,
        "title": "중첩 dict flatten",
        "category": "data_structure",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "중첩된 딕셔너리를 평탄화하는 함수 `flatten_dict(d: dict, sep: str = '.') -> dict`를 작성하세요.\n"
            "- 예: {'a': {'b': {'c': 1}}} → {'a.b.c': 1}\n"
            "- 리스트 값은 그대로 보존\n"
            "- 재귀 없이 반복문으로 구현"
        ),
    },
    {
        "id": "lv1_4",
        "level": 1,
        "title": "async retry 데코레이터",
        "category": "async",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "비동기 함수에 적용할 수 있는 재시도 데코레이터 `async_retry(max_attempts=3, delay=1.0)`를 작성하세요.\n"
            "- 지수 백오프 적용 (delay * 2^attempt)\n"
            "- 특정 예외 타입만 재시도 가능하도록 `exceptions` 파라미터 지원\n"
            "- 최종 실패 시 원래 예외를 re-raise"
        ),
    },
    {
        "id": "lv1_5",
        "level": 1,
        "title": "JSON 스키마 검증기",
        "category": "validation",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "외부 라이브러리 없이 JSON 스키마를 검증하는 `validate_schema(data: dict, schema: dict) -> tuple[bool, list[str]]`을 작성하세요.\n"
            "- 지원 타입: string, integer, number, boolean, array, object, null\n"
            "- required 필드 검사\n"
            "- 오류 메시지 리스트 반환"
        ),
    },

    # ── Lv2: 중급-FastAPI ───────────────────────────────────────────────────────
    {
        "id": "lv2_1",
        "level": 2,
        "title": "CRUD + Pydantic",
        "category": "fastapi",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "FastAPI와 Pydantic v2를 사용해 Todo 아이템 CRUD API를 작성하세요.\n"
            "- GET /todos, POST /todos, PUT /todos/{id}, DELETE /todos/{id}\n"
            "- in-memory 저장소 사용\n"
            "- 응답 모델, 유효성 검사, HTTP 상태코드 명시"
        ),
    },
    {
        "id": "lv2_2",
        "level": 2,
        "title": "WebSocket 채팅",
        "category": "fastapi",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "FastAPI WebSocket으로 다중 클라이언트 채팅 서버를 구현하세요.\n"
            "- ConnectionManager 클래스로 연결 관리\n"
            "- 입장/퇴장 시 브로드캐스트\n"
            "- 특정 클라이언트에게만 메시지 전송 가능"
        ),
    },
    {
        "id": "lv2_3",
        "level": 2,
        "title": "Rate Limiter 미들웨어",
        "category": "fastapi",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "FastAPI 미들웨어로 IP 기반 Rate Limiter를 구현하세요.\n"
            "- 슬라이딩 윈도우 알고리즘 (60초당 100 요청)\n"
            "- 초과 시 429 Too Many Requests 반환\n"
            "- X-RateLimit-Remaining 헤더 추가"
        ),
    },
    {
        "id": "lv2_4",
        "level": 2,
        "title": "파일 업로드 + SSE",
        "category": "fastapi",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "FastAPI로 CSV 파일 업로드 후 처리 진행률을 SSE(Server-Sent Events)로 스트리밍하세요.\n"
            "- POST /upload: 파일 업로드 후 task_id 반환\n"
            "- GET /progress/{task_id}: 처리 진행률 SSE 스트림\n"
            "- 백그라운드 태스크로 파일 처리"
        ),
    },
    {
        "id": "lv2_5",
        "level": 2,
        "title": "JWT 인증",
        "category": "fastapi",
        "prompt": (
            "다음 요구사항에 맞는 Python 코드를 작성하세요:\n"
            "FastAPI JWT 인증 시스템을 구현하세요.\n"
            "- POST /auth/login: username/password → access_token + refresh_token\n"
            "- POST /auth/refresh: refresh_token → 새 access_token\n"
            "- Depends로 보호된 라우트 구현\n"
            "- python-jose 사용"
        ),
    },

    # ── Lv3: 고급-버그수정 ────────────────────────────────────────────────────
    {
        "id": "lv3_1",
        "level": 3,
        "title": "asyncio 데드락 수정",
        "category": "bug_fix",
        "prompt": (
            "다음 버그가 있는 Python 코드를 분석하고 수정하세요:\n\n"
            "```python\n"
            "import asyncio\n\n"
            "lock_a = asyncio.Lock()\n"
            "lock_b = asyncio.Lock()\n\n"
            "async def task_a():\n"
            "    async with lock_a:\n"
            "        await asyncio.sleep(0.1)\n"
            "        async with lock_b:\n"
            "            print('task_a done')\n\n"
            "async def task_b():\n"
            "    async with lock_b:\n"
            "        await asyncio.sleep(0.1)\n"
            "        async with lock_a:\n"
            "            print('task_b done')\n\n"
            "async def main():\n"
            "    await asyncio.gather(task_a(), task_b())\n"
            "```\n\n"
            "버그를 설명하고, 데드락 없이 동작하도록 수정하세요."
        ),
    },
    {
        "id": "lv3_2",
        "level": 3,
        "title": "SQL Injection 취약점 수정",
        "category": "bug_fix",
        "prompt": (
            "다음 취약한 Python 코드를 분석하고 수정하세요:\n\n"
            "```python\n"
            "import sqlite3\n\n"
            "def get_user(username: str) -> dict | None:\n"
            "    conn = sqlite3.connect('users.db')\n"
            "    cursor = conn.cursor()\n"
            "    query = f\"SELECT * FROM users WHERE username = '{username}'\"\n"
            "    cursor.execute(query)\n"
            "    row = cursor.fetchone()\n"
            "    conn.close()\n"
            "    return row\n"
            "```\n\n"
            "SQL Injection 취약점을 설명하고, 파라미터화 쿼리로 수정하세요."
        ),
    },
    {
        "id": "lv3_3",
        "level": 3,
        "title": "메모리 누수 수정",
        "category": "bug_fix",
        "prompt": (
            "다음 메모리 누수가 있는 Python 코드를 분석하고 수정하세요:\n\n"
            "```python\n"
            "class EventEmitter:\n"
            "    _listeners: dict[str, list] = {}\n\n"
            "    def on(self, event: str, callback):\n"
            "        if event not in self._listeners:\n"
            "            self._listeners[event] = []\n"
            "        self._listeners[event].append(callback)\n\n"
            "    def emit(self, event: str, *args):\n"
            "        for cb in self._listeners.get(event, []):\n"
            "            cb(*args)\n"
            "```\n\n"
            "메모리 누수 원인을 설명하고 수정하세요. weakref 활용 권장."
        ),
    },
    {
        "id": "lv3_4",
        "level": 3,
        "title": "Race Condition 수정",
        "category": "bug_fix",
        "prompt": (
            "다음 Race Condition이 있는 Python 코드를 분석하고 수정하세요:\n\n"
            "```python\n"
            "import asyncio\n\n"
            "class Counter:\n"
            "    def __init__(self):\n"
            "        self.value = 0\n\n"
            "    async def increment(self):\n"
            "        current = self.value\n"
            "        await asyncio.sleep(0)  # 컨텍스트 스위치 유발\n"
            "        self.value = current + 1\n\n"
            "async def main():\n"
            "    counter = Counter()\n"
            "    await asyncio.gather(*[counter.increment() for _ in range(1000)])\n"
            "    print(counter.value)  # 1000이 아님\n"
            "```\n\n"
            "Race Condition을 설명하고 asyncio.Lock으로 수정하세요."
        ),
    },
    {
        "id": "lv3_5",
        "level": 3,
        "title": "N+1 쿼리 최적화",
        "category": "bug_fix",
        "prompt": (
            "다음 N+1 쿼리 문제가 있는 SQLAlchemy 코드를 분석하고 수정하세요:\n\n"
            "```python\n"
            "from sqlalchemy.orm import Session\n\n"
            "def get_posts_with_comments(session: Session) -> list[dict]:\n"
            "    posts = session.query(Post).all()\n"
            "    result = []\n"
            "    for post in posts:\n"
            "        result.append({\n"
            "            'title': post.title,\n"
            "            'comments': [c.body for c in post.comments]  # N+1\n"
            "        })\n"
            "    return result\n"
            "```\n\n"
            "N+1 쿼리 문제를 설명하고 joinedload/selectinload로 최적화하세요."
        ),
    },

    # ── Lv4: 복합-멀티파일 ───────────────────────────────────────────────────
    {
        "id": "lv4_1",
        "level": 4,
        "title": "3계층 아키텍처 리팩토링",
        "category": "refactoring",
        "prompt": (
            "다음 단일 파일 FastAPI 앱을 3계층(Router/Service/Repository)으로 리팩토링하세요:\n\n"
            "```python\n"
            "# main.py — 모든 로직이 한 파일에\n"
            "from fastapi import FastAPI\n"
            "import sqlite3\n\n"
            "app = FastAPI()\n\n"
            "@app.get('/users/{user_id}')\n"
            "def get_user(user_id: int):\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
            "    user = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()\n"
            "    conn.close()\n"
            "    return {'id': user[0], 'name': user[1]}\n"
            "```\n\n"
            "routers/users.py, services/user_service.py, repositories/user_repo.py 3개 파일로 분리하세요."
        ),
    },
    {
        "id": "lv4_2",
        "level": 4,
        "title": "sync→async 전환",
        "category": "refactoring",
        "prompt": (
            "다음 동기 코드를 완전히 비동기로 전환하세요:\n\n"
            "```python\n"
            "import requests\n"
            "import psycopg2\n\n"
            "def fetch_and_store(urls: list[str], conn_str: str):\n"
            "    conn = psycopg2.connect(conn_str)\n"
            "    cur = conn.cursor()\n"
            "    for url in urls:\n"
            "        resp = requests.get(url, timeout=10)\n"
            "        cur.execute('INSERT INTO pages (url, content) VALUES (%s, %s)',\n"
            "                    (url, resp.text))\n"
            "    conn.commit()\n"
            "    conn.close()\n"
            "```\n\n"
            "httpx.AsyncClient + asyncpg + asyncio.gather로 전환하세요."
        ),
    },
    {
        "id": "lv4_3",
        "level": 4,
        "title": "Strategy 패턴 적용",
        "category": "design_pattern",
        "prompt": (
            "다음 if-elif 체인을 Strategy 패턴으로 리팩토링하세요:\n\n"
            "```python\n"
            "def process_payment(method: str, amount: float) -> dict:\n"
            "    if method == 'card':\n"
            "        # 카드 처리 로직 20줄\n"
            "        fee = amount * 0.02\n"
            "        return {'status': 'ok', 'fee': fee}\n"
            "    elif method == 'bank':\n"
            "        # 계좌이체 처리 로직 20줄\n"
            "        fee = 500\n"
            "        return {'status': 'ok', 'fee': fee}\n"
            "    elif method == 'crypto':\n"
            "        # 암호화폐 처리 로직 20줄\n"
            "        fee = amount * 0.001\n"
            "        return {'status': 'ok', 'fee': fee}\n"
            "```\n\n"
            "PaymentStrategy ABC + CardStrategy/BankStrategy/CryptoStrategy + PaymentProcessor로 구현하세요."
        ),
    },
    {
        "id": "lv4_4",
        "level": 4,
        "title": "레거시 코드에 테스트 추가",
        "category": "testing",
        "prompt": (
            "다음 레거시 코드에 pytest 단위 테스트를 작성하세요:\n\n"
            "```python\n"
            "# legacy_calculator.py\n"
            "class Calculator:\n"
            "    def __init__(self):\n"
            "        self.history = []\n\n"
            "    def calculate(self, expr: str) -> float:\n"
            "        result = eval(expr)  # 보안 무시\n"
            "        self.history.append({'expr': expr, 'result': result})\n"
            "        return result\n\n"
            "    def get_history(self) -> list:\n"
            "        return self.history.copy()\n\n"
            "    def clear_history(self):\n"
            "        self.history.clear()\n"
            "```\n\n"
            "최소 10개 테스트 케이스(정상/경계/오류)를 작성하세요. monkeypatch로 eval을 모킹하세요."
        ),
    },
    {
        "id": "lv4_5",
        "level": 4,
        "title": "환경변수 리팩토링",
        "category": "refactoring",
        "prompt": (
            "다음 하드코딩된 설정을 pydantic-settings로 리팩토링하세요:\n\n"
            "```python\n"
            "# config.py\n"
            "DB_HOST = 'localhost'\n"
            "DB_PORT = 5432\n"
            "DB_NAME = 'mydb'\n"
            "DB_USER = 'admin'\n"
            "DB_PASS = 'secret123'\n"
            "REDIS_URL = 'redis://localhost:6379'\n"
            "API_KEY = 'abc123'\n"
            "DEBUG = True\n"
            "MAX_WORKERS = 4\n"
            "```\n\n"
            "pydantic-settings BaseSettings로 전환하고, .env 파일 로드, 타입 검증, 기본값 설정을 구현하세요."
        ),
    },

    # ── Lv5: 아키텍처 ────────────────────────────────────────────────────────
    {
        "id": "lv5_1",
        "level": 5,
        "title": "실시간 알림 시스템",
        "category": "architecture",
        "prompt": (
            "다음 요구사항을 만족하는 실시간 알림 시스템을 설계하고 구현하세요:\n"
            "- FastAPI + WebSocket + Redis Pub/Sub\n"
            "- 다중 서버 환경에서 알림 브로드캐스트\n"
            "- 사용자별 채널 구독/구독해제\n"
            "- 연결 끊김 시 재연결 + 미수신 알림 재전송\n"
            "- NotificationService, WebSocketManager, RedisSubscriber 클래스 포함\n"
            "전체 구조도와 핵심 코드를 작성하세요."
        ),
    },
    {
        "id": "lv5_2",
        "level": 5,
        "title": "LLM 프록시 레이어",
        "category": "architecture",
        "prompt": (
            "다음 요구사항을 만족하는 LLM 프록시 레이어를 설계하고 구현하세요:\n"
            "- 여러 LLM 제공자(OpenAI, Anthropic, Gemini) 추상화\n"
            "- 제공자별 비용 추적 및 예산 초과 시 자동 폴백\n"
            "- 응답 캐싱 (Redis, TTL 1시간)\n"
            "- 스트리밍 응답 지원\n"
            "- LLMProvider ABC, CostTracker, ResponseCache, LLMProxy 클래스 포함\n"
            "전체 구조도와 핵심 코드를 작성하세요."
        ),
    },
    {
        "id": "lv5_3",
        "level": 5,
        "title": "이벤트 소싱 구현",
        "category": "architecture",
        "prompt": (
            "다음 요구사항을 만족하는 이벤트 소싱 시스템을 설계하고 구현하세요:\n"
            "- 도메인: 은행 계좌 (입금/출금/이체)\n"
            "- EventStore(PostgreSQL), Aggregate(Account), EventBus\n"
            "- 스냅샷 지원 (매 100 이벤트)\n"
            "- 이벤트 재생으로 현재 상태 복원\n"
            "- CQRS: Command Handler / Query Handler 분리\n"
            "전체 구조도와 핵심 코드를 작성하세요."
        ),
    },
    {
        "id": "lv5_4",
        "level": 5,
        "title": "분산 작업 큐",
        "category": "architecture",
        "prompt": (
            "다음 요구사항을 만족하는 분산 작업 큐 시스템을 설계하고 구현하세요:\n"
            "- FastAPI + Redis Streams\n"
            "- 작업 우선순위 (HIGH/MEDIUM/LOW)\n"
            "- 워커 자동 확장/축소 (min 1, max 10)\n"
            "- 실패 작업 재시도 (최대 3회, 지수 백오프)\n"
            "- 데드레터 큐\n"
            "- TaskQueue, Worker, WorkerPool, DeadLetterQueue 클래스 포함\n"
            "전체 구조도와 핵심 코드를 작성하세요."
        ),
    },
    {
        "id": "lv5_5",
        "level": 5,
        "title": "플러그인 아키텍처",
        "category": "architecture",
        "prompt": (
            "다음 요구사항을 만족하는 플러그인 아키텍처를 설계하고 구현하세요:\n"
            "- 런타임에 플러그인 로드/언로드 가능\n"
            "- 플러그인 의존성 해결 (DAG 기반 순서)\n"
            "- 플러그인 간 이벤트 훅 시스템\n"
            "- 플러그인 버전 호환성 검사\n"
            "- Plugin ABC, PluginManager, HookRegistry, DependencyResolver 포함\n"
            "전체 구조도와 핵심 코드를 작성하세요."
        ),
    },
]
