#!/data/data/com.termux/files/usr/bin/bash
# AADS Mobile Agent — Termux 원클릭 설치
set -e

echo "=== AADS Mobile Agent 설치 시작 ==="

# Termux 패키지 업데이트
pkg update -y
pkg install -y python termux-api git

# Python 의존성
pip install websockets pydantic

# 에이전트 코드 다운로드
AGENT_DIR="$HOME/aads-mobile-agent"
if [ -d "$AGENT_DIR" ]; then
    cd "$AGENT_DIR" && git pull
else
    git clone https://github.com/moongoby-GO100/aads-mobile-agent.git "$AGENT_DIR"
fi

# 설정 파일 생성
cat > "$AGENT_DIR/.env" << 'EOF'
DEVICE_SERVER_URL=wss://aads.newtalk.kr/api/v1/devices/ws
DEVICE_AGENT_TOKEN=your-token-here
EOF

echo ""
echo "=== 설치 완료 ==="
echo "1. $AGENT_DIR/.env 파일에서 토큰을 설정하세요"
echo "2. 실행: cd $AGENT_DIR && python -m mobile_agent.agent"
echo ""
echo "자동 시작 등록 (선택):"
echo "  mkdir -p ~/.termux/boot"
echo "  echo 'cd $AGENT_DIR && python -m mobile_agent.agent &' > ~/.termux/boot/aads-agent.sh"
echo "  chmod +x ~/.termux/boot/aads-agent.sh"
