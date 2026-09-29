#!/usr/bin/env bash
# Run from the AADS source host before any Jinah migration or cutover step.
set -euo pipefail

target=${OBYS_JINAH_SSH_TARGET:-jinah244}
expected_host=${OBYS_JINAH_EXPECTED_HOST:-5.104.85.244}

config=$(ssh -T -G "$target")
configured_host=$(awk '$1 == "hostname" { print $2; exit }' <<<"$config")
configured_user=$(awk '$1 == "user" { print $2; exit }' <<<"$config")
identity=$(awk '$1 == "identityfile" { print $2; exit }' <<<"$config")

if [[ "$configured_host" != "$expected_host" || "$configured_user" != root ]]; then
    printf 'BLOCKED: %s resolves to %s as %s; expected %s as root\n' \
        "$target" "$configured_host" "$configured_user" "$expected_host" >&2
    exit 2
fi
if [[ -z "$identity" || ! -r "$identity" ]]; then
    printf 'BLOCKED: configured SSH identity is unreadable: %s\n' "$identity" >&2
    exit 2
fi

ssh_options=(
    -T
    -o BatchMode=yes
    -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes
    -o ConnectTimeout=5
    -o ConnectionAttempts=1
)

if remote_uid=$(ssh "${ssh_options[@]}" "$target" 'id -u' 2>&1) &&
    [[ "$remote_uid" == 0 ]]; then
    printf 'READY: %s (%s) authenticated as root using %s\n' \
        "$target" "$expected_host" "$identity"
    exit 0
fi

printf 'BLOCKED: root SSH preflight failed for %s (%s): %s\n' \
    "$target" "$expected_host" "$remote_uid" >&2
if partner_user=$(ssh "${ssh_options[@]}" -l partner "$expected_host" 'id -un' 2>/dev/null) &&
    [[ "$partner_user" == partner ]]; then
    printf 'DIAGNOSIS: partner authenticates with the configured key; root authorization or root SSH policy must be checked on Jinah.\n' >&2
fi
exit 1
