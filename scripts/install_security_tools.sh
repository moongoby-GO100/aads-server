#!/bin/bash
# 커밋 게이트가 의존하는 외부 도구를 설치한다.
#
# 왜 스크립트로 두는가. 2026-09-15 이 호스트에 ruff 가 없다는 것을 아무도 몰랐고,
# pre-commit 의 `command -v ruff` 가 조용히 거짓이 되어 정적 분석 단계가 한 번도
# 돌지 않았다. CLAUDE.md 는 그동안 "③ruff 정적 분석" 이 돈다고 적고 있었다.
# 게이트가 외부 바이너리에 의존하면 그 바이너리를 설치하는 방법도 저장소에
# 있어야 한다 — 없으면 새 서버에서 게이트가 소리 없이 비활성화된다.
#
# 사용:
#   bash scripts/install_security_tools.sh          # 없는 것만 설치
#   bash scripts/install_security_tools.sh --check   # 설치 여부만 보고 (exit 1 = 누락)
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.30.1}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"

check_only=0
[ "${1:-}" = "--check" ] && check_only=1

report() {
  local missing=0
  echo "── 커밋 게이트 의존 도구 ──"
  for t in ruff gitleaks; do
    if command -v "$t" &>/dev/null; then
      printf "  ${GREEN}✅ %-10s${NC} %s\n" "$t" "$(command -v "$t")"
    else
      printf "  ${RED}❌ %-10s MISSING${NC}\n" "$t"
      missing=1
    fi
  done
  return $missing
}

install_gitleaks() {
  command -v gitleaks &>/dev/null && { echo "gitleaks 이미 설치됨 ($(gitleaks version))"; return 0; }

  local arch tarball url tmp
  case "$(uname -m)" in
    x86_64)  arch="x64" ;;
    aarch64) arch="arm64" ;;
    *) echo -e "${RED}지원하지 않는 아키텍처: $(uname -m)${NC}"; return 1 ;;
  esac
  tarball="gitleaks_${GITLEAKS_VERSION}_linux_${arch}.tar.gz"
  url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${tarball}"
  tmp=$(mktemp -d)

  echo "gitleaks ${GITLEAKS_VERSION} 내려받는 중..."
  if ! curl -sSL --max-time 120 -o "$tmp/$tarball" "$url"; then
    echo -e "${RED}다운로드 실패: $url${NC}"; rm -rf "$tmp"; return 1
  fi

  # 체크섬 검증. 바이너리를 네트워크에서 받아 PATH 에 넣는 일이므로 건너뛰지 않는다.
  if curl -sSL --max-time 60 -o "$tmp/checksums.txt" \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_checksums.txt"; then
    local want got
    want=$(grep " $tarball\$" "$tmp/checksums.txt" | awk '{print $1}')
    got=$(sha256sum "$tmp/$tarball" | awk '{print $1}')
    if [ -z "$want" ] || [ "$want" != "$got" ]; then
      echo -e "${RED}체크섬 불일치 — 설치 중단${NC}"
      echo "  expected=$want"
      echo "  actual  =$got"
      rm -rf "$tmp"; return 1
    fi
    echo -e "  ${GREEN}sha256 검증 통과${NC}"
  else
    echo -e "${RED}체크섬 파일을 받지 못했다 — 설치 중단${NC}"; rm -rf "$tmp"; return 1
  fi

  tar xzf "$tmp/$tarball" -C "$tmp" gitleaks || { rm -rf "$tmp"; return 1; }
  install -m 0755 "$tmp/gitleaks" "$BIN_DIR/gitleaks" || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  echo -e "  ${GREEN}설치 완료: $BIN_DIR/gitleaks ($(gitleaks version))${NC}"
}

install_ruff() {
  command -v ruff &>/dev/null && { echo "ruff 이미 설치됨 ($(ruff --version))"; return 0; }
  echo "ruff 설치 중 (pip)..."
  python3 -m pip install --quiet ruff && echo -e "  ${GREEN}설치 완료: $(command -v ruff)${NC}"
}

if [ $check_only -eq 1 ]; then
  report
  rc=$?
  [ $rc -ne 0 ] && echo -e "${YELLOW}누락 도구가 있다. 설치: bash scripts/install_security_tools.sh${NC}"
  exit $rc
fi

install_ruff
install_gitleaks
echo
report
