#!/usr/bin/env python3
"""서버 환경 스냅샷 수집기 — AADS Context API 자동 저장"""

import subprocess
import json
import os
import glob
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
AADS_API = os.getenv("AADS_API_URL", "https://aads.newtalk.kr/api/v1")
MONITOR_KEY = os.getenv("AADS_MONITOR_KEY", "")
SERVER_NAME = os.getenv("SERVER_NAME", "unknown")

def run(cmd, timeout=10):
    """쉘 명령 실행, 실패 시 빈 문자열"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()[:2000]  # 최대 2KB
    except:
        return ""

def collect_light():
    """경량 스냅샷 (5분 주기) — 서비스 상태, 디스크, 프로세스"""
    return {
        "type": "light",
        "collected_at": datetime.now(KST).isoformat(),
        "server": SERVER_NAME,
        "system": {
            "disk_usage": run("df -h / --output=pcent,size,avail | tail -1"),
            "memory": run("free -h | grep Mem | awk '{print $2,$3,$4}'"),
            "load": run("uptime | awk -F'load average:' '{print $2}'"),
            "uptime": run("uptime -p"),
        },
        "services": {
            "systemd_active": run("systemctl list-units --type=service --state=active --no-pager --plain | grep -E '(nginx|php|mysql|postgres|redis|docker|reverb|node|pm2|supervisor|aads|bridge|watchdog|remote)' | awk '{print $1, $3}'"),
            "docker": run("docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}' 2>/dev/null") or "not running",
            "open_ports": run("ss -tlnp | grep LISTEN | awk '{print $4, $6}' | head -20"),
        },
        "processes": {
            "php": run("ps aux | grep -E 'php|artisan' | grep -v grep | wc -l"),
            "node": run("ps aux | grep node | grep -v grep | wc -l"),
            "python": run("ps aux | grep python | grep -v grep | wc -l"),
        },
        "recent_changes": {
            "last_git_commits": {},
            "last_docker_events": run("docker events --since '5m' --until '0s' --format '{{.Time}} {{.Action}} {{.Actor.Attributes.name}}' 2>/dev/null | tail -5") or "none",
        },
    }

def collect_full():
    """전체 스냅샷 (30분 주기) — DB 스키마, 폴더, 패키지 포함"""
    snapshot = collect_light()
    snapshot["type"] = "full"

    # 런타임 버전
    snapshot["runtimes"] = {
        "php": run("php -v 2>/dev/null | head -1") or "not installed",
        "python3": run("python3 --version 2>/dev/null") or "not installed",
        "node": run("node -v 2>/dev/null") or "not installed",
        "npm": run("npm -v 2>/dev/null") or "not installed",
        "composer": run("composer --version 2>/dev/null | head -1") or "not installed",
        "pip": run("pip3 --version 2>/dev/null | head -1") or "not installed",
        "docker": run("docker --version 2>/dev/null") or "not installed",
        "git": run("git --version 2>/dev/null") or "not installed",
    }

    # 프로젝트 디렉터리 스캔
    PROJECT_DIRS = os.getenv("PROJECT_DIRS", "").split(",")
    snapshot["projects"] = {}
    for d in PROJECT_DIRS:
        d = d.strip()
        if not d or not os.path.exists(d):
            snapshot["projects"][d] = {"exists": False}
            continue

        snapshot["projects"][d] = {
            "exists": True,
            "tree": run(f"find {d} -maxdepth 3 -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/vendor/*' -not -path '*/__pycache__/*' | head -150"),
            "config_files": {
                "composer.json": os.path.exists(f"{d}/composer.json"),
                "package.json": os.path.exists(f"{d}/package.json"),
                "requirements.txt": os.path.exists(f"{d}/requirements.txt"),
                "pyproject.toml": os.path.exists(f"{d}/pyproject.toml"),
                "docker-compose.yml": os.path.exists(f"{d}/docker-compose.yml"),
                "Dockerfile": os.path.exists(f"{d}/Dockerfile"),
                ".env": os.path.exists(f"{d}/.env"),
                "Makefile": os.path.exists(f"{d}/Makefile"),
            },
            "env_keys": run(f"grep -oP '^[A-Z][A-Z0-9_]+' {d}/.env 2>/dev/null | sort | head -50") or "no .env",
            "git_branch": run(f"cd {d} && git branch --show-current 2>/dev/null") or "no git",
            "git_last3": run(f"cd {d} && git log --oneline -3 2>/dev/null") or "no git",
            "git_status": run(f"cd {d} && git status --short 2>/dev/null | head -10") or "clean",
        }

        # composer.json → PHP 패키지 목록
        if os.path.exists(f"{d}/composer.json"):
            snapshot["projects"][d]["php_packages"] = run(f"cd {d} && composer show --no-ansi 2>/dev/null | head -30") or "composer not available"
            snapshot["projects"][d]["laravel_version"] = run(f"cd {d} && php artisan --version 2>/dev/null") or "not laravel"

        # package.json → Node 패키지
        if os.path.exists(f"{d}/package.json"):
            snapshot["projects"][d]["node_packages"] = run(f"cat {d}/package.json | python3 -c \"import sys,json; d=json.load(sys.stdin); print('\\n'.join(f'{{k}}: {{v}}' for k,v in {{**d.get('dependencies',{{}}), **d.get('devDependencies',{{}})}}.items()))\" 2>/dev/null | head -30") or "parse error"

    # DB 스키마
    snapshot["databases"] = {}

    # MySQL
    mysql_dbs = run("mysql -u root -e 'SHOW DATABASES;' 2>/dev/null")
    if mysql_dbs:
        for db_name in mysql_dbs.split("\n")[1:]:
            db_name = db_name.strip()
            if db_name in ("information_schema", "performance_schema", "mysql", "sys", ""):
                continue
            tables = run(f"mysql -u root -e 'SHOW TABLES;' {db_name} 2>/dev/null")
            schema = run(f"mysql -u root -e \"SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='{db_name}' ORDER BY TABLE_NAME, ORDINAL_POSITION;\" 2>/dev/null | head -200")
            row_counts = run(f"mysql -u root -e \"SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES WHERE TABLE_SCHEMA='{db_name}' ORDER BY TABLE_ROWS DESC;\" 2>/dev/null | head -30")
            snapshot["databases"][f"mysql:{db_name}"] = {
                "tables": tables,
                "schema": schema,
                "row_counts": row_counts,
            }

    # PostgreSQL
    pg_dbs = run("sudo -u postgres psql -c '\\l' 2>/dev/null") or run("docker exec aads-postgres psql -U aads -d aads -c '\\dt' 2>/dev/null")
    if pg_dbs:
        pg_schema = run("sudo -u postgres psql -d aads -c \"SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name, ordinal_position;\" 2>/dev/null | head -200") or run("docker exec aads-postgres psql -U aads -d aads -c \"SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name, ordinal_position;\" 2>/dev/null | head -200")
        snapshot["databases"]["postgresql"] = {
            "databases": pg_dbs,
            "schema": pg_schema,
        }

    # Nginx 설정
    snapshot["nginx"] = {
        "sites_enabled": run("ls /etc/nginx/sites-enabled/ 2>/dev/null") or run("ls /etc/nginx/conf.d/ 2>/dev/null") or "not found",
        "server_names": run("grep -r server_name /etc/nginx/sites-enabled/ 2>/dev/null | head -10") or "not found",
    }

    # cron
    snapshot["cron"] = run("crontab -l 2>/dev/null | grep -v '^#' | head -15") or "no crontab"

    return snapshot

async def post_snapshot(snapshot, category_suffix=""):
    """AADS Context API에 저장"""
    key = f"env_{SERVER_NAME}"
    if category_suffix:
        key = f"env_{SERVER_NAME}_{category_suffix}"

    async with aiohttp.ClientSession() as session:
        # Context API 저장
        await session.post(
            f"{AADS_API}/context/system",
            json={
                "category": "server_environment",
                "key": key,
                "data": snapshot,
            },
            headers={"X-Monitor-Key": MONITOR_KEY, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        )

        # 정적 JSON 파일로도 저장 (지휘 AI 크롤링용)
        local_path = f"/root/aads/aads-dashboard/public/manager/env_{SERVER_NAME}.json"
        if os.path.exists(os.path.dirname(local_path)):
            with open(local_path, "w") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)

async def main():
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "light"

    if mode == "full":
        snapshot = collect_full()
    elif mode == "event":
        snapshot = collect_full()
        snapshot["trigger"] = "event"
        snapshot["event_reason"] = sys.argv[2] if len(sys.argv) > 2 else "manual"
    else:
        snapshot = collect_light()

    await post_snapshot(snapshot)
    print(f"[{datetime.now(KST).strftime('%H:%M:%S')}] {mode} snapshot → AADS API ({SERVER_NAME})")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
