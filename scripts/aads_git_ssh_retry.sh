#!/usr/bin/env bash
# aads-git-ssh-retry — git 이 GitHub 에 SSH 로 붙을 때 쓰는 ssh 대체 명령.
#
# 2026-11-04 18시경(KST)부터 GitHub 가 정상 등록된 키를 간헐적으로 거부한다
# ("Permission denied (publickey)", 같은 키가 성공·실패를 오감, 116·42 모두).
# 러너 fetch/push 가 한 번 거부되면 작업 전체가 실패하므로, 인증 단계에서
# 끊긴 경우에만 다시 접속한다.
#
# 다시 접속해도 안전한 이유: git 의 upload-pack/receive-pack 은 서버가 먼저
# 광고를 보내고 클라이언트가 그 뒤에 쓴다. 인증 거부(종료코드 255)면 아직
# 표준입력을 읽지도, 표준출력에 쓰지도 않았다.
#
# 다시 접속하지 않는 경우: 호스트 키 검증 실패(보안), 인증 후 오류, 그 외 실패.
#
# 설치: /usr/local/bin/aads-git-ssh-retry, git config --system core.sshCommand 로 지정.
# 저장소에 core.sshCommand 가 따로 있으면 맨 앞 "ssh" 를 이 명령으로 바꾼다.
# 되돌리기: git config --system --unset core.sshCommand (저장소별은 "ssh" 로 복원).
set -u

max="${AADS_GIT_SSH_RETRIES:-5}"
base_sleep="${AADS_GIT_SSH_RETRY_SLEEP:-2}"
ssh_bin="${AADS_GIT_SSH_BIN:-ssh}"
log="${AADS_GIT_SSH_RETRY_LOG:-/var/log/aads-git-ssh-retry.log}"

errf="$(mktemp "${TMPDIR:-/tmp}/aads-git-ssh.XXXXXX")" || exec "$ssh_bin" "$@"
trap 'rm -f "$errf"' EXIT

attempt=1
while :; do
    "$ssh_bin" "$@" 2>"$errf"
    rc=$?
    if [[ $rc -eq 255 && $attempt -lt $max ]] \
        && grep -qE 'Permission denied \(publickey\)|kex_exchange_identification|Connection reset by peer|Connection closed by remote host' "$errf" \
        && ! grep -q 'Host key verification failed' "$errf"; then
        { printf '%s retry %d/%d rc=%d %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$attempt" "$max" "$rc" \
            "$(grep -oE 'Permission denied \(publickey\)|kex_exchange_identification|Connection reset by peer|Connection closed by remote host' "$errf" | head -1)"; } >>"$log" 2>/dev/null || true
        sleep $(( base_sleep * attempt ))
        attempt=$(( attempt + 1 ))
        continue
    fi
    cat "$errf" >&2
    if [[ $attempt -gt 1 ]]; then
        printf '%s done attempts=%d rc=%d\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$attempt" "$rc" >>"$log" 2>/dev/null || true
    fi
    exit "$rc"
done
