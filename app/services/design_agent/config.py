"""Environment-only limits, loaded explicitly rather than at import time."""
import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    max_llm_calls: int = 15
    daily_usd_cap: Decimal = Decimal('2.0')

    def __post_init__(self):
        if not 0 <= self.max_llm_calls <= 15:
            raise ValueError('LLM call limit must be between 0 and 15')
        if not self.daily_usd_cap.is_finite() or self.daily_usd_cap < 0:
            raise ValueError('Invalid daily cap')

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv('DESIGN_AGENT_ENABLED', 'false').lower() == 'true',
            min(15, max(0, int(os.getenv('DESIGN_AGENT_MAX_LLM_CALLS_PER_RUN', '15')))),
            Decimal(os.getenv('DESIGN_AGENT_DAILY_USD_CAP', '2.0')),
        )
