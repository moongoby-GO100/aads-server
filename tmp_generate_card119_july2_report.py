#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import html
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

KST = timezone(timedelta(hours=9))
DAYS = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
RUN_ID = 404
ROOT = Path("/root/kis-autotrade-v4")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.core.database import AsyncSessionLocal
SUMMARY = ROOT / "artifacts/go100/card119_20260706_20260710_backtest_20260830_summary.json"
OUT = ROOT / "frontend/public/reports/go100_card119_20260706_20260710_backtest_20260830.html"


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def h(value) -> str:
    return html.escape(str(value if value is not None else ""))


def pct(value) -> str:
    try:
        return f"{float(value):+.2f}%"
    except Exception:
        return "미측정"


def money(value) -> str:
    try:
        return f"{int(round(float(value))):,}원"
    except Exception:
        return "미측정"


def tmin(value) -> str:
    text_value = str(value or "")
    return text_value[11:16] if len(text_value) >= 16 and text_value[4] == "-" else text_value[:5]


def tr(cells, tag: str = "td") -> str:
    return "<tr>" + "".join(f"<{tag}>{h(cell)}</{tag}>" for cell in cells) + "</tr>"


def label(code, names: dict[str, str]) -> str:
    code = str(code or "").zfill(6)
    return f"{names.get(code, '종목명미확인')}({code})"


async def main() -> None:
    async with AsyncSessionLocal() as db:
        run = (
            await db.execute(
                text(
                    """
                    SELECT id, status, start_date::text, end_date::text,
                           initial_capital, total_return, max_drawdown, win_rate,
                           total_trades, error_message, created_at::text,
                           completed_at::text
                    FROM go100_backtest_runs
                    WHERE id = :run_id
                    """
                ),
                {"run_id": RUN_ID},
            )
        ).mappings().first()
        detail = (
            await db.execute(
                text("SELECT result_detail FROM go100_backtest_runs WHERE id = :run_id"),
                {"run_id": RUN_ID},
            )
        ).scalar_one()
        events = (
            await db.execute(
                text(
                    """
                    SELECT trade_date::text, stock_code, stock_name, event_type,
                           lock_status, closed_locked, open_gap_pct, high_return_pct,
                           close_return_pct, first_25pct_at::text, first_touch_at::text,
                           lock_at::text, unlock_count, next_trade_date::text,
                           next_open_gap_pct, next_high_return_pct,
                           next_close_return_pct, next_day_limitup
                    FROM go100_limitup_events
                    WHERE trade_date BETWEEN DATE '2026-07-06' AND DATE '2026-07-10'
                      AND event_type IN ('limitup', 'near_limitup')
                    ORDER BY trade_date, event_type DESC, stock_code
                    """
                )
            )
        ).mappings().all()

    if isinstance(detail, str):
        detail = json.loads(detail)

    trades_all = detail.get("trade_log") or []
    trades = [t for t in trades_all if str(t.get("entry_date"))[:10] in DAYS]

    codes = {str(t.get("stock_code") or "").zfill(6) for t in trades}
    codes.update(str(e.get("stock_code") or "").zfill(6) for e in events)
    async with AsyncSessionLocal() as db:
        name_rows = (
            await db.execute(
                text("SELECT stock_code, stock_name FROM stock_universe WHERE stock_code = ANY(:codes)"),
                {"codes": sorted(codes)},
            )
        ).mappings().all()
    names = {str(r["stock_code"]).zfill(6): r["stock_name"] for r in name_rows}

    by_day = defaultdict(
        lambda: {
            "candidate": 0,
            "snapshot": 0,
            "source": "-",
            "post_facto": False,
            "event_total": 0,
            "locked": 0,
            "near": 0,
            "entered_true": 0,
            "missed_locked": 0,
            "trades": 0,
            "same": 0,
            "next": 0,
            "reentry": 0,
            "pnl": 0.0,
            "wins": 0,
            "losses": 0,
        }
    )

    replay = detail.get("card119_candidate_replay") or {}
    for item in replay.get("by_day", []):
        day = str(item.get("trading_date"))[:10]
        if day in DAYS:
            by_day[day]["candidate"] = int(item.get("candidate_count") or 0)
            by_day[day]["snapshot"] = int(item.get("snapshot_row_count") or 0)
            by_day[day]["source"] = (
                "전일 universe fallback" if item.get("fallback") else str(item.get("mode") or item.get("source") or "-")
            )
            by_day[day]["post_facto"] = bool(item.get("post_facto_event_data_used"))

    event_by_day = defaultdict(list)
    for item in events:
        event = dict(item)
        day = str(event["trade_date"])[:10]
        event_by_day[day].append(event)
        if event["event_type"] == "limitup":
            by_day[day]["event_total"] += 1
        if event["event_type"] == "near_limitup":
            by_day[day]["near"] += 1
        if event["event_type"] == "limitup" and (event["closed_locked"] or event["lock_status"] == "locked"):
            by_day[day]["locked"] += 1

    entered_codes_by_day = defaultdict(set)
    for trade in trades:
        day = str(trade.get("entry_date"))[:10]
        code = str(trade.get("stock_code") or "").zfill(6)
        entered_codes_by_day[day].add(code)
        by_day[day]["trades"] += 1
        pnl = float(trade.get("realized_pnl") or 0)
        by_day[day]["pnl"] += pnl
        by_day[day]["same"] += int(trade.get("exit_date") == trade.get("entry_date"))
        by_day[day]["next"] += int(trade.get("exit_date") != trade.get("entry_date"))
        by_day[day]["wins"] += int(float(trade.get("return_pct") or 0) > 0)
        by_day[day]["losses"] += int(float(trade.get("return_pct") or 0) <= 0)
        by_day[day]["reentry"] += int(int(trade.get("reentry_count") or 0) > 0)

    locked_missed = []
    event_rows = []
    for day in DAYS:
        for event in event_by_day[day]:
            code = str(event["stock_code"]).zfill(6)
            entered = code in entered_codes_by_day[day]
            is_locked = event["event_type"] == "limitup" and (
                event["closed_locked"] or event["lock_status"] == "locked"
            )
            if is_locked and entered:
                by_day[day]["entered_true"] += 1
            if is_locked and not entered:
                by_day[day]["missed_locked"] += 1
                locked_missed.append(event)
            event_rows.append(
                [
                    day,
                    label(code, names),
                    event["event_type"],
                    event["lock_status"],
                    "Y" if event["closed_locked"] else "N",
                    tmin(event["first_25pct_at"]),
                    tmin(event["first_touch_at"]),
                    tmin(event["lock_at"]),
                    event["unlock_count"],
                    pct(event["open_gap_pct"]),
                    pct(event["high_return_pct"]),
                    pct(event["close_return_pct"]),
                    event["next_trade_date"],
                    pct(event["next_open_gap_pct"]),
                    pct(event["next_high_return_pct"]),
                    pct(event["next_close_return_pct"]),
                    "Y" if event["next_day_limitup"] else "N",
                    "진입" if entered else "미진입",
                ]
            )

    trade_rows = []
    issues = []
    for idx, trade in enumerate(trades, 1):
        day = str(trade.get("entry_date"))[:10]
        code = str(trade.get("stock_code") or "").zfill(6)
        event = next((e for e in event_by_day[day] if str(e["stock_code"]).zfill(6) == code), None)
        locked_at_close = bool(
            event and event.get("event_type") == "limitup" and (event.get("closed_locked") or event.get("lock_status") == "locked")
        )
        if trade.get("exit_date") != trade.get("entry_date") and not locked_at_close:
            issues.append(f"{label(code, names)} {day} 미잠김/이벤트불일치인데 {trade.get('exit_date')}까지 이월됨")
        trade_rows.append(
            [
                idx,
                label(code, names),
                trade.get("entry_date"),
                trade.get("entry_time"),
                money(trade.get("entry_price")),
                pct(trade.get("entry_realtime_change_pct")),
                {
                    "before_lock": "잠김 전",
                    "lock_touched_in_entry_bar": "잠김 터치봉",
                    "at_or_after_lock": "잠김 후/동시",
                }.get(trade.get("entry_lock_timing"), trade.get("entry_lock_timing")),
                "Y" if int(trade.get("reentry_count") or 0) > 0 else "N",
                trade.get("reentry_count") or 0,
                "Y" if locked_at_close else "N",
                trade.get("exit_date"),
                trade.get("exit_time"),
                money(trade.get("exit_price")),
                trade.get("exit_reason"),
                pct(trade.get("return_pct")),
                money(trade.get("realized_pnl")),
                "Y" if trade.get("next_day_exit_applied") else "N",
                trade.get("prior_exit_reason") or "-",
            ]
        )

    total_pnl = sum(float(t.get("realized_pnl") or 0) for t in trades)
    initial_capital = float(run["initial_capital"]) if run else 5_000_000.0
    filtered_return = total_pnl / initial_capital * 100
    same_day = sum(1 for t in trades if t.get("exit_date") == t.get("entry_date"))
    next_day = len(trades) - same_day
    reentry = sum(1 for t in trades if int(t.get("reentry_count") or 0) > 0)
    wins = sum(1 for t in trades if float(t.get("return_pct") or 0) > 0)

    daily_rows = []
    for day in DAYS:
        b = by_day[day]
        daily_rows.append(
            [
                day,
                b["candidate"],
                b["snapshot"],
                b["source"],
                "Y" if b["post_facto"] else "N",
                b["event_total"],
                b["locked"],
                b["entered_true"],
                b["missed_locked"],
                b["near"],
                b["trades"],
                b["same"],
                b["next"],
                b["reentry"],
                f"{b['wins']}/{b['losses']}",
                money(b["pnl"]),
            ]
        )

    summary = {
        "generated_at_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        "run": dict(run),
        "report_entry_period": DAYS,
        "filtered_trade_count": len(trades),
        "filtered_total_pnl": round(total_pnl, 2),
        "filtered_return_pct": round(filtered_return, 4),
        "same_day_exit_count": same_day,
        "next_day_exit_count": next_day,
        "reentry_trade_count": reentry,
        "win_count": wins,
        "loss_count": len(trades) - wins,
        "daily": {day: by_day[day] for day in DAYS},
        "missed_locked_count": len(locked_missed),
        "event_count": len(event_rows),
        "issues": issues,
        "source": "DB go100_backtest_runs.result_detail + go100_limitup_events + stock_universe",
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")

    criteria_rows = [
        [
            "발굴",
            "당일 +20% 이상 watch 후보. 백테스트는 후보 스냅샷 우선, 없으면 전일 universe fallback.",
            "이번 7/6~7/10은 snapshot_row_count 0, fallback 후보 170개. 사후 상한가 이벤트는 후보 생성에 미사용.",
        ],
        [
            "선정",
            "watch 후보 중 +27% BUY 게이트, 체결 가능성, 슬롯/자본, 방어청산 후 재진입 허용을 통과한 종목.",
            "실제 진입 12건, 고유 종목 6개. 후보 대비 거래 전환율 7.1%. 선정 탈락 사유별 로그는 미저장.",
        ],
        [
            "진입",
            "완전잠김 차단이 아니라 미보유면 체결 가능성 검토. +27% 이상에서 매수, 방어매도 후 재상승 시 재진입.",
            "잠김 전 3건, 잠김 후/동시 9건. 재진입 6건.",
        ],
        [
            "청산",
            "당일 종가잠김 실패 종목은 당일 방어청산이 원칙. 종가잠김 보유건은 익일 복합 트레일링 청산.",
            "당일청산 8건, 익일청산 4건. 미잠김 이월 의심 1건이 발견되어 P0 수정 필요.",
        ],
    ]
    total_locked = sum(by_day[day]["locked"] for day in DAYS)
    total_entered_locked = sum(by_day[day]["entered_true"] for day in DAYS)
    html_doc = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>GO100 #119 2026-07-06~2026-07-10 백테스트 상세 보고서</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 28px; background: #f5f7fa; color: #172033; }}
main {{ max-width: 1500px; margin: auto; background: white; border: 1px solid #dce3ec; border-radius: 8px; padding: 28px; }}
h1 {{ font-size: 28px; margin: 0 0 8px; }}
h2 {{ font-size: 20px; margin-top: 30px; border-top: 1px solid #e5ebf2; padding-top: 18px; }}
.small {{ font-size: 12px; color: #607087; }}
.warn {{ background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px; padding: 12px 14px; margin: 16px 0; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 18px 0; }}
.metric {{ background: #fbfcfe; border: 1px solid #dce3ec; border-radius: 8px; padding: 12px; }}
.metric b {{ display: block; font-size: 12px; color: #65758b; margin-bottom: 6px; }}
.metric span {{ font-size: 20px; font-weight: 700; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; margin: 12px 0 22px; }}
th, td {{ border: 1px solid #e1e7ef; padding: 7px 8px; text-align: left; vertical-align: top; }}
th {{ background: #eef3f8; white-space: nowrap; }}
tr:nth-child(even) td {{ background: #fbfcfe; }}
code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; }}
</style>
</head>
<body><main>
<h1>GO100 #119 2026-07-06~2026-07-10 백테스트 상세 보고서</h1>
<p class="small">생성 {h(summary['generated_at_kst'])} / run {RUN_ID} / 원천: DB <code>go100_backtest_runs.result_detail</code>, <code>go100_limitup_events</code>, <code>stock_universe</code></p>
<div class="warn"><b>핵심 판정</b><br />7월 둘째 주 진입분만 필터링하면 총손익 {money(total_pnl)}, 수익률 {pct(filtered_return)}, 거래 {len(trades)}건입니다. 종가잠김/잠김진단 이벤트 {total_locked}건 중 실제 진입은 {total_entered_locked}건이며, {len(locked_missed)}건은 놓쳤습니다. 이번 구간도 후보 스냅샷이 0건이라 발굴은 전일 universe fallback으로 재생됐고, 사후 상한가 이벤트는 성과 진단 라벨에만 사용했습니다.</div>
<div class="summary">
<div class="metric"><b>run 상태</b><span>{h(run['status'])}</span></div>
<div class="metric"><b>보고구간 손익</b><span>{money(total_pnl)}</span></div>
<div class="metric"><b>보고구간 수익률</b><span>{pct(filtered_return)}</span></div>
<div class="metric"><b>거래/승패</b><span>{len(trades)}건 / {wins}승 {len(trades) - wins}패</span></div>
<div class="metric"><b>당일/익일청산</b><span>{same_day}/{next_day}</span></div>
<div class="metric"><b>재진입</b><span>{reentry}건</span></div>
<div class="metric"><b>후보 fallback</b><span>{sum(by_day[day]['candidate'] for day in DAYS)}개</span></div>
<div class="metric"><b>미진입 잠김</b><span>{len(locked_missed)}건</span></div>
</div>
<h2>#119 로직 기준</h2>
<table><thead>{tr(['단계', '전략 기준', '이번 백테스트 확인'], 'th')}</thead><tbody>{''.join(tr(row) for row in criteria_rows)}</tbody></table>
<h2>일자별 발굴-선정-진입-청산</h2>
<table><thead>{tr(['일자', '발굴후보', '스냅샷', '후보소스', '사후데이터 후보사용', '상한가이벤트', '잠김진단', '잠김진입', '미진입잠김', '근접상한', '진입건', '당일청산', '익일청산', '재진입', '승/패', '손익'], 'th')}</thead><tbody>{''.join(tr(row) for row in daily_rows)}</tbody></table>
<h2>종목별 진입/청산 12건</h2>
<table><thead>{tr(['no', '종목', '진입일', '진입시각', '매수가', '진입등락률', '잠김대비', '재진입', '재진입회차', '종가잠김진단', '청산일', '청산시각', '청산가', '청산사유', '수익률', '손익', '익일청산', '직전청산사유'], 'th')}</thead><tbody>{''.join(tr(row) for row in trade_rows)}</tbody></table>
<h2>일자별 상한가/근접상한 이벤트와 진입 여부</h2>
<table><thead>{tr(['일자', '종목', '이벤트', '잠김상태', '종가잠김', '25%최초', '터치', '잠김', '풀림수', '시가갭', '당일고가', '종가등락', '익일', '익일갭', '익일고가', '익일종가', '익일상한가', '진입여부'], 'th')}</thead><tbody>{''.join(tr(row) for row in event_rows)}</tbody></table>
<h2>문제점과 개선안</h2>
<table><thead>{tr(['우선순위', '확인된 문제', '개선안', '완료 기준'], 'th')}</thead><tbody>
{tr(['P0', '후보 스냅샷 0건으로 발굴/선정 탈락 사유를 종목별로 복원할 수 없습니다.', '실매매 20% watch 후보를 분 단위로 저장하고 백테스트는 해당 스냅샷을 replay합니다.', '동일 구간 재실행 시 snapshot_row_count > 0, 후보별 selected/rejected reason 기록.'])}
{tr(['P0', f'종가잠김/잠김진단 {total_locked}건 중 진입 {total_entered_locked}건, 미진입 {len(locked_missed)}건입니다.', '27% 진입 큐에서 25% 최초 접근, 29.5% 터치, 매도잔량 감소, 주문가능수량을 우선순위화합니다.', '미진입 잠김 수를 동일 구간 재실행에서 절반 이하로 감소.'])}
{tr(['P0', '미잠김 이월 의심: ' + ('; '.join(issues) if issues else '없음'), '당일 종가잠김 실패/이벤트 불일치 종목은 예외 없이 당일 방어청산으로 닫고 익일 청산 대상에서 제외합니다.', '미잠김 이월 건수 0건.'])}
{tr(['P1', '재진입은 열려 있으나 점수화 라벨만 있고 실제 점수화는 보류 상태입니다.', 'CEO 지시대로 점수화는 학습 라벨링으로 두고, 현 단계는 재진입 결과/라벨 축적만 수행합니다.', 'reentry_count, prior_exit_reason, relock signal 라벨 저장률 100%.'])}
</tbody></table>
<p class="small">주의: run 404의 엔진 종료일은 2026-07-13입니다. 본 보고서는 2026-07-06~2026-07-10 진입분만 집계하고, 7/13은 7/10 보유분 익일청산 확인용으로만 해석했습니다.</p>
</main></body></html>"""
    OUT.write_text(html_doc, encoding="utf-8")
    print(
        json.dumps(
            {
                "summary": str(SUMMARY),
                "html": str(OUT),
                "filtered_trade_count": len(trades),
                "filtered_total_pnl": round(total_pnl, 2),
                "filtered_return_pct": round(filtered_return, 4),
                "missed_locked_count": len(locked_missed),
                "issues": issues,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
