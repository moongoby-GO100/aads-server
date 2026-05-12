#!/bin/bash
# Keep AADS blue/green host ports private to nginx/local health checks.

set -euo pipefail

PORTS=(8100 8102 3100 3101)
IPTABLES="${IPTABLES:-iptables}"
IP6TABLES="${IP6TABLES:-ip6tables}"

remove_legacy_ipv4_rules() {
    local port="$1"
    shift
    local comment

    for comment in "$@"; do
        while "$IPTABLES" -w 10 -C DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$comment" -j DROP 2>/dev/null; do
            "$IPTABLES" -w 10 -D DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$comment" -j DROP
        done
    done
}

ensure_ipv4_rule() {
    local port="$1"
    local comment="aads-bg-host-only-port-${port}"

    "$IPTABLES" -w 10 -N DOCKER-USER 2>/dev/null || true
    remove_legacy_ipv4_rules "$port" "aads-bg-host-only-api-${port}" "aads-bg-host-only-dashboard-${port}"
    if "$IPTABLES" -w 10 -C DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$comment" -j DROP 2>/dev/null; then
        return 0
    fi

    if "$IPTABLES" -w 10 -C DOCKER-USER -j RETURN 2>/dev/null; then
        local return_line
        return_line="$("$IPTABLES" -w 10 -L DOCKER-USER --line-numbers | awk '$2 == "RETURN" { print $1; exit }')"
        "$IPTABLES" -w 10 -I DOCKER-USER "${return_line:-1}" -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$comment" -j DROP
    else
        "$IPTABLES" -w 10 -A DOCKER-USER -p tcp -m conntrack --ctorigdstport "$port" -m comment --comment "$comment" -j DROP
        "$IPTABLES" -w 10 -A DOCKER-USER -j RETURN
    fi
}

ensure_ipv6_rule() {
    local comment="aads-bg-host-only-public-ports"

    if "$IP6TABLES" -w 10 -C INPUT ! -i lo -p tcp -m multiport --dports 8100,8102,3100,3101 -m comment --comment "$comment" -j DROP 2>/dev/null; then
        return 0
    fi
    "$IP6TABLES" -w 10 -I INPUT 1 ! -i lo -p tcp -m multiport --dports 8100,8102,3100,3101 -m comment --comment "$comment" -j DROP
}

main() {
    for port in "${PORTS[@]}"; do
        ensure_ipv4_rule "$port"
    done
    ensure_ipv6_rule
}

main "$@"
