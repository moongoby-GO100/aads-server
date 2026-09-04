# 2026-09-04 Shinhan Security Preflight

## Scope
- CEO reported the Shinhan login window did not appear on the target PC until AhnLab was manually started.
- Add a bank queue preflight so Shinhan/financial collection checks PC security runtime before opening the bank browser.

## Changes
- `scripts/yeoljeong_auto_collect.py`
  - Added PC Agent `financial_exclusive` PowerShell preflight for AhnLab/VeraPort/keyboard security runtime.
  - Bank queue drain now checks security runtime after account collectability and before browser collection.
  - If security runtime is not ready, the queue item is requeued with `browser_attempted=false` and `SHINHAN_SECURITY_PROGRAM_NOT_READY`.
  - Added `SHINHAN_SECURITY_PROGRAM_NOT_READY` as a blocking/action-required code.
- `app/services/yeoljeong_bank_browser_connector.py`
  - Direct Shinhan browser collection now stops before login when required runtime is not detected.
  - Diagnostics remain secret-free.
- Tests added for queue defer and direct connector fail-fast behavior.

## Verification
- `docker run --rm -v /root/aads/aads-server:/app -w /app aads-server:bb5c68537adb python -m pytest tests/unit/test_yeoljeong_auto_collect.py::test_drain_bank_queue_defers_when_pc_security_program_not_ready tests/unit/test_yeoljeong_bank_browser_connector.py::test_collect_async_shinhan_stops_before_login_when_security_program_not_ready`
- Result: 2 passed.

## Live State Before Deploy
- PC Agent `7f99c528-24d` / `DESKTOP-ICU55HK` is online.
- PC Agent PowerShell preflight detected AhnLab Safe Transaction, VeraPort, TouchEn, nProtect, AnySign, INISAFE and running services including `SafeTransactionSVC`, `WizveraPMSvc`, `nossvc`.
- `transactions.json` is still empty before the next Shinhan live collection retry.
