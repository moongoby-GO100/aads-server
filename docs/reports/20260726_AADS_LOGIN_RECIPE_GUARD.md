# AADS Login Recipe Guard - 2026-07-26

## Summary
- Issue: The public Unni Naengmyeon recipe link already routed through `fb.newtalk.kr`, but stale browser sessions or old links could still land on `https://aads.newtalk.kr/login?redirect=/unni-naengmyeon/recipes`.
- Impact: Those stale/direct entries displayed the AADS dashboard login, so the recipe click still looked incorrectly tied to AADS login.
- Fix: `nginx-aads.conf` now catches only recipe-specific `/login` redirect parameters on `aads.newtalk.kr` and sends them to the FB store assistant app.

## Runtime Action
- Applied the same guard to `/etc/nginx/conf.d/aads.conf`.
- Reloaded `aads-nginx` after `nginx -t` passed.

## Verification
- `https://aads.newtalk.kr/login?redirect=%2Funni-naengmyeon%2Frecipes`: `302` to `https://fb.newtalk.kr/static/apps/yeoljeong-finance/index.html?redirect=%2Funni-naengmyeon%2Frecipes`.
- `https://aads.newtalk.kr/login?redirect=https%3A%2F%2Ffb.newtalk.kr%2Funni-naengmyeon%2Frecipes`: `302` to the same FB store assistant URL.
- `https://aads.newtalk.kr/login`: remains the normal AADS dashboard login.
- `https://unni.newtalk.kr/unni-naengmyeon/recipes`: ends at the FB store assistant login page, not the AADS login page.

## Notes
- Existing dirty changes in `docs/HANDOVER.md` and changelog files were not touched.
- This report isolates the guard fix from unrelated worktree changes.
