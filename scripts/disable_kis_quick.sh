#!/bin/bash
# KIS systemd disable - quick version
for unit in kis-1m-daily.timer kis-ohlcv-sync.timer kis-chart-refresh.timer kis-collect-report.timer kis-autotrade-top100.timer kis-autotrade-backup.timer kis-autotrade-backup-verify.timer kis-autotrade-backup-sandbox.timer kis-autotrade.service kis-autotrade-api.service kis-autotrade-scalping.service kis-1m-intraday.service; do
  systemctl disable "$unit" 2>/dev/null
  systemctl mask "$unit" 2>/dev/null
done
echo "DONE: KIS systemd units masked"
systemctl list-units --type=timer --state=active 2>/dev/null | grep kis || echo "No active KIS timers"
