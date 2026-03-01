from pydantic_settings import BaseSettings
from pydantic import SecretStr


class Settings(BaseSettings):
    # API Keys (SecretStr로 로그 마스킹)
    ANTHROPIC_API_KEY: SecretStr
    OPENAI_API_KEY: SecretStr = SecretStr("")
    GOOGLE_API_KEY: SecretStr = SecretStr("")
    E2B_API_KEY: SecretStr

    # Supabase 직접 연결 (R-011: port 5432 필수)
    # 형식: postgresql://postgres:[pw]@db.[ref].supabase.co:5432/postgres
    SUPABASE_DIRECT_URL: str

    # Redis
    UPSTASH_REDIS_URL: str = ""

    # 비용/한도 (R-012, 설계서 Section 21)
    MAX_LLM_CALLS_PER_TASK: int = 15
    MAX_COST_PER_TASK_USD: float = 10.0
    MAX_COST_MONTHLY_USD: float = 500.0
    COST_WARNING_THRESHOLD: float = 0.8  # 80%에서 경고

    # 동시성 (설계서 Section 21)
    MAX_CONCURRENT_THREADS: int = 10
    MAX_CONCURRENT_SANDBOXES: int = 5
    MAX_DB_CONNECTIONS: int = 15

    # E2B 샌드박스 설정
    SANDBOX_TIMEOUT_SECONDS: int = 300
    SANDBOX_MAX_RETRIES: int = 3

    # 환경
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
