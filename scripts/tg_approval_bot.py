#!/usr/bin/env python3
"""
Telegram 수동 승인 복구 봇
- cross_monitor.sh가 장애 감지 시 /tmp/aads_recovery_pending/ 에 JSON 파일 생성
- 봇이 10초마다 체크 → 인라인 버튼 메시지 전송
- CEO가 [✅ 복구 실행] 클릭 → 복구 명령 실행
- CEO가 [❌ 무시] 클릭 → 무시 로그
- Long polling 방식 (webhook 불필요)
"""
import json
import os
import subprocess
import time
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

import requests

# ── 설정 ──
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
_chat_id_str = os.environ.get("TELEGRAM_CHAT_ID")
if not _chat_id_str:
    raise RuntimeError("TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다.")
ALLOWED_CHAT_ID = int(_chat_id_str)
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOG_FILE = "/var/log/tg_approval_bot.log"
PENDING_DIR = "/tmp/aads_recovery_pending"

# ── 로깅 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TG-BOT] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("tg_approval_bot")

# ── 복구 명령 정의 ──
RECOVERY_COMMANDS = {
    "aads_server": {
        "label": "aads-server 컨테이너",
        "cmd": "cd /root/aads && docker compose -f docker-compose.prod.yml up -d --no-deps aads-server",
        "server": "68"
    },
    "auto_trigger": {
        "label": "auto_trigger.sh 파이프라인",
        "cmd": "nohup /root/.genspark/auto_trigger.sh >> /var/log/auto_trigger.log 2>&1 &",
        "server": "local"
    },
    "aads_bridge": {
        "label": "aads-bridge 서비스",
        "cmd": "systemctl restart aads-bridge",
        "server": "211"
    },
    "aads_dashboard": {
        "label": "aads-dashboard 컨테이너",
        "cmd": "cd /root/aads && docker compose -f docker-compose.prod.yml up -d --no-deps aads-dashboard",
        "server": "68"
    },
    "aads_postgres": {
        "label": "aads-postgres DB",
        "cmd": "cd /root/aads && docker compose -f docker-compose.prod.yml up -d --no-deps aads-postgres",
        "server": "68"
    },
}

SERVER_IPS = {
    "68": "68.183.183.11",
    "211": "211.188.51.113",
    "114": "116.120.58.155",
}


def tg_request(method, data=None):
    try:
        resp = requests.post(f"{API_BASE}/{method}", json=data, timeout=60)
        result = resp.json()
        if not result.get("ok"):
            log.error(f"TG API 실패: {method} → {result}")
        return result
    except Exception as e:
        log.error(f"TG API 오류: {e}")
        return None


def send_approval_request(recovery_type, server, issue):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S KST")
    recovery = RECOVERY_COMMANDS.get(recovery_type, {})
    label = recovery.get("label", recovery_type)
    cb_approve = f"approve:{recovery_type}"
    cb_ignore = f"ignore:{recovery_type}"

    text = (
        f"🚨 *[AADS 장애 감지]* {now}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📍 서버: {server}\n"
        f"🔴 이슈: {issue}\n"
        f"🔧 복구 대상: {label}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"아래 버튼으로 복구 여부를 결정하세요."
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ 복구 실행", "callback_data": cb_approve},
            {"text": "❌ 무시", "callback_data": cb_ignore}
        ]]
    }

    result = tg_request("sendMessage", {
        "chat_id": ALLOWED_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard
    })

    if result and result.get("ok"):
        log.info(f"승인 요청 전송 완료: {recovery_type} on {server}")
    else:
        log.error(f"승인 요청 전송 실패: {result}")
    return result


def execute_recovery(recovery_type):
    recovery = RECOVERY_COMMANDS.get(recovery_type)
    if not recovery:
        return f"❌ 알 수 없는 복구 타입: {recovery_type}"

    cmd = recovery["cmd"]
    server = recovery["server"]

    try:
        if server == "local" or server == "68":
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        else:
            ip = SERVER_IPS.get(server, server)
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", f"root@{ip}", cmd],
                capture_output=True, text=True, timeout=60
            )

        if result.returncode == 0:
            log.info(f"복구 성공: {recovery_type}")
            out = result.stdout[:500] if result.stdout else "(출력 없음)"
            return f"✅ 복구 성공: {recovery['label']}\n{out}"
        else:
            log.error(f"복구 실패: {recovery_type} exit={result.returncode}")
            err = result.stderr[:500] if result.stderr else "(오류 없음)"
            return f"⚠️ 복구 실행됨 (exit={result.returncode})\n{err}"
    except subprocess.TimeoutExpired:
        return f"⏰ 복구 타임아웃 (60초 초과)"
    except Exception as e:
        return f"❌ 복구 오류: {e}"


def handle_callback(callback_query):
    callback_id = callback_query.get("id")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    message_id = callback_query.get("message", {}).get("message_id")
    data = callback_query.get("data", "")
    from_user = callback_query.get("from", {}).get("id")

    if from_user != ALLOWED_CHAT_ID:
        tg_request("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "⛔ 권한 없음", "show_alert": True
        })
        return

    parts = data.split(":", 1)
    if len(parts) != 2:
        return
    action, recovery_type = parts

    if action == "approve":
        tg_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": "🔧 복구 실행 중..."})
        tg_request("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": f"🔧 *복구 실행 중...* ({recovery_type})", "parse_mode": "Markdown"
        })
        result_text = execute_recovery(recovery_type)
        tg_request("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": f"🔧 *복구 결과*\n\n{result_text}", "parse_mode": "Markdown"
        })
        log.info(f"CEO 승인 → 복구 완료: {recovery_type}")

    elif action == "ignore":
        tg_request("answerCallbackQuery", {"callback_query_id": callback_id, "text": "무시 처리됨"})
        tg_request("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": f"💤 *무시됨* — {recovery_type} ({datetime.now().strftime('%H:%M:%S')})",
            "parse_mode": "Markdown"
        })
        log.info(f"CEO 무시: {recovery_type}")


def check_pending_requests():
    pending_dir = Path(PENDING_DIR)
    if not pending_dir.exists():
        return
    for f in sorted(pending_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            send_approval_request(
                recovery_type=data.get("type", "unknown"),
                server=data.get("server", "unknown"),
                issue=data.get("issue", "상세 불명")
            )
            f.unlink()
            log.info(f"Pending 처리 완료: {f.name}")
        except Exception as e:
            log.error(f"Pending 처리 오류 ({f.name}): {e}")
            try:
                f.unlink()
            except:
                pass


def main():
    log.info(f"=== AADS Telegram 승인 봇 시작 (chat_id={ALLOWED_CHAT_ID}) ===")

    def signal_handler(sig, frame):
        log.info(f"Signal {sig} 수신 — 종료 중...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    offset = None
    last_pending_check = 0

    while True:
        try:
            now = time.time()
            if now - last_pending_check > 10:
                check_pending_requests()
                last_pending_check = now

            params = {"timeout": 30, "allowed_updates": ["callback_query", "message"]}
            if offset:
                params["offset"] = offset

            resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=35)

            if resp.status_code != 200:
                log.error(f"Telegram API 오류: {resp.status_code} {resp.text[:200]}")
                time.sleep(5)
                continue

            data = resp.json()
            if not data.get("ok"):
                log.error(f"Telegram 응답 오류: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id")
                    if text == "/status" and chat_id == ALLOWED_CHAT_ID:
                        tg_request("sendMessage", {
                            "chat_id": chat_id,
                            "text": f"✅ 승인 복구 봇 정상 동작 중\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S KST')}\n📋 복구 타입: {', '.join(RECOVERY_COMMANDS.keys())}"
                        })
                    elif text == "/test_alert" and chat_id == ALLOWED_CHAT_ID:
                        send_approval_request("aads_server", "Core(68)", "[테스트] /test_alert 명령")

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError:
            log.warning("네트워크 오류, 10초 후 재시도")
            time.sleep(10)
        except Exception as e:
            log.error(f"메인 루프 오류: {e}")
            time.sleep(5)


if __name__ == "__main__":
    Path(PENDING_DIR).mkdir(parents=True, exist_ok=True)
    main()
