#!/bin/bash
# migrate-contabo.sh — AADS Contabo VPS 원클릭 배포 스크립트
# task_id: CUR-AADS-INFRA-CONTABO-009
# 실행 위치: 새 Contabo VPS (Ubuntu 22.04 LTS)
# 실행 방법: bash migrate-contabo.sh
# 주의: .env 파일은 scp로 별도 수동 복사 필요 (R-003)

set -e

AADS_ROOT="/root/aads"
GITHUB_ORG="moongoby-GO100"
DOMAIN="aads.newtalk.kr"

echo "================================================"
echo "  AADS Contabo 마이그레이션 스크립트"
echo "  task: CUR-AADS-INFRA-CONTABO-009"
echo "  실행시각: $(date '+%Y-%m-%d %H:%M:%S KST')"
echo "================================================"

# ─── Step 1: 시스템 업데이트 ───────────────────────
echo "[1/8] 시스템 패키지 업데이트..."
apt-get update -y
apt-get upgrade -y

# ─── Step 2: 기본 도구 설치 ───────────────────────
echo "[2/8] 기본 도구 설치..."
apt-get install -y \
    curl wget git vim ufw \
    python3 python3-pip \
    ca-certificates gnupg lsb-release

# ─── Step 3: Docker CE 설치 ───────────────────────
echo "[3/8] Docker CE 설치..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    systemctl enable docker
    systemctl start docker
    echo "Docker 설치 완료: $(docker --version)"
else
    echo "Docker 이미 설치됨: $(docker --version)"
fi

# Docker Compose v2
if ! docker compose version &> /dev/null; then
    apt-get install -y docker-compose-plugin
fi
echo "Docker Compose: $(docker compose version)"

# ─── Step 4: Nginx + Certbot 설치 ──────────────────
echo "[4/8] Nginx + Certbot 설치..."
apt-get install -y nginx certbot python3-certbot-nginx
systemctl enable nginx
systemctl start nginx

# ─── Step 5: 방화벽 설정 ──────────────────────────
echo "[5/8] UFW 방화벽 설정..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   # SSH
ufw allow 80/tcp   # HTTP
ufw allow 443/tcp  # HTTPS
ufw allow 8000/tcp # AADS API
ufw --force enable
echo "UFW 상태:"
ufw status

# ─── Step 6: 코드 클론 ─────────────────────────────
echo "[6/8] GitHub 코드 클론..."
mkdir -p "$AADS_ROOT"
cd "$AADS_ROOT"

for repo in aads-server aads-dashboard aads-docs; do
    if [ -d "${AADS_ROOT}/${repo}" ]; then
        echo "  ${repo}: 이미 존재 — git pull"
        git -C "${AADS_ROOT}/${repo}" pull origin main
    else
        echo "  ${repo}: clone 중..."
        git clone "https://github.com/${GITHUB_ORG}/${repo}.git"
    fi
done

# ─── Step 7: .env 파일 확인 ────────────────────────
echo "[7/8] .env 파일 확인 (R-003: 수동 복사 필요)..."
if [ ! -f "${AADS_ROOT}/aads-server/.env" ]; then
    echo ""
    echo "⚠️  경고: .env 파일이 없습니다!"
    echo "   구 서버(68.183.183.11)에서 아래 명령으로 복사하세요:"
    echo ""
    echo "   scp root@68.183.183.11:/root/aads/aads-server/.env \\"
    echo "       root@$(hostname -I | awk '{print $1}'):/root/aads/aads-server/.env"
    echo ""
    echo "   .env 파일 복사 후 스크립트를 재실행하거나 아래 Docker 명령을 수동 실행하세요."
    exit 1
fi
echo "  .env 파일 확인 OK"

# ─── Step 8: Docker Compose 빌드 & 기동 ────────────
echo "[8/8] Docker Compose 빌드 및 기동..."
cd "${AADS_ROOT}/aads-server"
docker compose up -d --build

echo ""
echo "서비스 상태 확인 (30초 대기)..."
sleep 30

# Health Check
echo "Health check..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ Health check 성공: HTTP ${HTTP_CODE}"
else
    echo "❌ Health check 실패: HTTP ${HTTP_CODE}"
    echo "Docker 로그:"
    docker compose logs --tail=20
    exit 2
fi

echo ""
echo "================================================"
echo "  마이그레이션 완료!"
echo "  HTTP: ${HTTP_CODE}"
echo "  다음 단계:"
echo "  1. SSL 인증서 발급:"
echo "     certbot --nginx -d ${DOMAIN}"
echo "  2. DNS 변경: ${DOMAIN} → $(curl -s ifconfig.me)"
echo "  3. PostgreSQL 데이터 마이그레이션"
echo "     (pg_dump → pg_restore 수동 실행)"
echo "================================================"
