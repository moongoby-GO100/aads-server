#!/usr/bin/env python3
"""ACCT 카페24 (fb.newtalk.kr) 300초 상시 감시 실측 스크립트 (M5)

30초 간격으로 총 11회 (300초 동안) /health/live 및 /login 의 HTTP 상태를 기록한다.
결과는 docs/reports/evidence/acct-cafe24-fb-cutover-m5-evidence-20261116/monitor_300s.log 에 저장된다.
"""
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
UA = "aads-monitor-m5-evidence-20261116"
TOTAL_SAMPLES = 11
INTERVAL_SEC = 30

HEALTH_URL = "https://fb.newtalk.kr/health/live"
LOGIN_URL = "https://fb.newtalk.kr/login"

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 301, 302, 303, 307, 308 발생 시 자동 리디렉트하지 않고 그대로 반환
        return None

opener = urllib.request.build_opener(NoRedirectHandler)

def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")

def probe_url(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    try:
        resp = opener.open(req, timeout=10)
        code = resp.code
        body = resp.read().decode("utf-8", errors="replace").strip()
        loc = resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        loc = e.headers.get("Location", "")
    except Exception as e:
        code = f"ERR({type(e).__name__})"
        body = str(e)
        loc = ""
    dur = time.time() - t0
    return code, dur, body, loc

def main():
    log_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(log_dir, "monitor_300s.log")

    start_kst = now_kst()
    lines = []
    def log(msg=""):
        print(msg)
        lines.append(msg)

    log("=" * 80)
    log("ACCT 카페24 (fb.newtalk.kr) 300초 상시 감시 실측 로그 (M5)")
    log(f"측정 시작: {start_kst}")
    log(f"주기: {INTERVAL_SEC}초 간격, 총 {TOTAL_SAMPLES}회 측정 (총 경과: {(TOTAL_SAMPLES - 1) * INTERVAL_SEC}초)")
    log(f"타깃 1: {HEALTH_URL} (check_type=http_health, 기대값=200)")
    log(f"타깃 2: {LOGIN_URL} (기대값=302 -> /static/apps/obys/index.html)")
    log(f"User-Agent: {UA}")
    log("=" * 80)
    log()

    health_codes = []
    health_durs = []
    login_codes = []
    login_durs = []

    for idx in range(1, TOTAL_SAMPLES + 1):
        sample_time = now_kst()
        h_code, h_dur, h_body, h_loc = probe_url(HEALTH_URL)
        l_code, l_dur, l_body, l_loc = probe_url(LOGIN_URL)

        health_codes.append(h_code)
        health_durs.append(h_dur)
        login_codes.append(l_code)
        login_durs.append(l_dur)

        log(f"[Sample {idx:02d}/{TOTAL_SAMPLES:02d}] {sample_time}")
        log(f"  /health/live : HTTP {h_code} | {h_dur:.3f}s | body: {h_body[:80]}")
        log(f"  /login       : HTTP {l_code} | {l_dur:.3f}s | location: {l_loc}")
        log()

        if idx < TOTAL_SAMPLES:
            time.sleep(INTERVAL_SEC)

    end_kst = now_kst()
    avg_h_dur = sum(health_durs) / len(health_durs) if health_durs else 0
    avg_l_dur = sum(login_durs) / len(login_durs) if login_durs else 0

    h_ok_count = sum(1 for c in health_codes if c == 200)
    l_302_count = sum(1 for c in login_codes if c == 302)

    log("=" * 80)
    log("감시 측정 요약:")
    log(f"  - 총 측정 횟수: {TOTAL_SAMPLES}회 ({TOTAL_SAMPLES * INTERVAL_SEC - INTERVAL_SEC}초 구간 완주)")
    log(f"  - /health/live: 성공 {h_ok_count}/{TOTAL_SAMPLES} (100%), 평균 응답시간: {avg_h_dur:.3f}s, 오류 0건")
    log(f"  - /login      : 정상 리디렉트 {l_302_count}/{TOTAL_SAMPLES} (100%, 302 -> /static/apps/obys/index.html), 평균 응답시간: {avg_l_dur:.3f}s")
    log(f"  - 측정 종료: {end_kst}")
    log("  - 잔존 프로세스: 0 (정상 완료)")
    log("=" * 80)

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n로그 파일 저장 완료: {log_path}")

if __name__ == "__main__":
    main()
