import os
import sys

# chat_service 및 sandbox 설정은 모듈 import 시 환경변수를 읽는다.
# 단위 테스트 수집은 외부 자격증명 없이도 가능해야 한다.
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")
os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "unit-test-auth-token")

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
