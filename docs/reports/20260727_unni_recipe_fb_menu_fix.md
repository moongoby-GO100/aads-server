# 2026-07-27 Unni Recipe FB Menu Fix

- Time: 2026-07-27 08:15:30 KST
- Scope: `fb.newtalk.kr` store-assistant static app and `unni.newtalk.kr` staff recipe route.
- Change:
  - Added visible Unni Naengmyeon homepage and staff recipe shortcuts to the FB dashboard.
  - Changed recipe redirect login mode from employee signup to login.
  - Kept post-login redirect target as `/unni-naengmyeon/recipes`.
- Verification:
  - FB app inline script syntax checked with `node --check`.
  - Dashboard recipe page checked with targeted ESLint and TypeScript.
  - Server static file bind mount verified in `aads-server-green`.

