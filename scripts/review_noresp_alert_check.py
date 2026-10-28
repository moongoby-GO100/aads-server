"""검수 무응답률 경보 점검 도구.

    --current     현재 7일 무응답률과 판정 (읽기 전용)
    --backtest    60일 이력을 KST 일 단위로 재생해 발화/비발화 표 출력 (읽기 전용)
    --test-fire   '[시험]' 알림 1건 (category=review_noresp_rate_test, INFO) — 쓰기 발생.
                  배포 후 수신 확인 단계에서만 쓴다. 실제 경보 집계에서 제외된다.

    docker exec aads-server python3 /app/scripts/review_noresp_alert_check.py --backtest
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _print_backtest(days: list, cfg) -> int:
    print(f"임계 {cfg.threshold_pct:g}% / 최소표본 {cfg.min_samples} / 창 {cfg.window_days}일 (행 기준 판정)")
    print(f"{'날짜(KST)':<11}{'분자':>6}{'분모':>7}{'행%':>8}{'job%':>8}  판정")
    for d in days:
        mark = "FIRE-" + d["decision"] if d["fired"] else d["decision"]
        if d["false_positive"]:
            mark += "  <== 오탐"
        print(
            f"{d['date']:<11}{d['numerator']:>6}{d['denominator']:>7}"
            f"{d['row_rate_pct']:>8.1f}{d['job_rate_pct']:>8.1f}  {mark}"
        )
    fired = [d["date"] for d in days if d["fired"]]
    quiet = [d["date"] for d in days if not d["fired"]]
    fps = [d["date"] for d in days if d["false_positive"]]
    print(f"\n발화 {len(fired)}일 / 비발화 {len(quiet)}일 / 오탐 {len(fps)}일")
    print("발화 일자:", ", ".join(fired) or "없음")
    print("※ 과거 재생이며 운영 관측이 아니다.")
    return 1 if fps else 0


async def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--current", action="store_true")
    mode.add_argument("--backtest", action="store_true")
    mode.add_argument("--test-fire", action="store_true")
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    from app.core.db_pool import close_pool, get_pool, init_pool
    from app.services import review_noresp_alert as nr

    cfg = nr.load_config()
    await init_pool()
    try:
        pool = get_pool()
        now = datetime.now(timezone.utc)
        if args.test_fire:
            ok = await nr.send_test_alert(now)
            print("시험 알림함 기록:", "성공" if ok else "실패")
            print("※ 성공은 alert_history 저장 성공이며 외부 전달·실제 수신 확인이 아니다.")
            return 0 if ok else 1

        if args.current:
            rows = await nr.fetch_window_rows(pool, now - timedelta(days=cfg.window_days), now)
            s = nr.compute_noresp_rate(rows, now, cfg.window_days)
            print(
                f"관측구간(KST, 시작 포함·끝 제외): "
                f"[{s.window_start.astimezone(nr.KST).isoformat()}, "
                f"{s.window_end.astimezone(nr.KST).isoformat()})"
            )
            print(
                f"임계 {cfg.threshold_pct:g}% / critical {cfg.critical_pct:g}% / "
                f"최소표본 {cfg.min_samples} / 창 {cfg.window_days}일 (행 기준 판정)"
            )
            print(f"행 기준 {s.numerator}/{s.denominator} = {s.row_rate_pct:.2f}%")
            print(f"job 기준 {s.job_numerator}/{s.job_denominator} = {s.job_rate_pct:.2f}%")
            print(f"최근 24시간 무응답 {s.last24h_numerator}건 / 별도지표 API_UNAVAILABLE {s.api_unavailable} TIMEOUT {s.timeout}")
            print("판정:", nr.decide(s.numerator, s.denominator, cfg))
            state = await nr._latest_state(pool, now)
            fires, resolves = await nr._delivered_today(pool, now)
            last = (
                f"{state.category}/{state.severity} @ {state.created_at.astimezone(nr.KST):%Y-%m-%d %H:%M} KST"
                if state else "없음"
            )
            print(f"장애 상태(알림함 기준): {nr.lifecycle_label(state)} (마지막 행: {last})")
            print(
                f"오늘(KST) 발송 경보 {fires}/{cfg.max_daily_incidents}회, 발송 해소 {resolves}회"
            )
            return 0

        rows = await nr.fetch_window_rows(
            pool, now - timedelta(days=args.days + cfg.window_days + 1), now
        )
        return _print_backtest(nr.replay_backtest(rows, now, args.days, cfg), cfg)
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
