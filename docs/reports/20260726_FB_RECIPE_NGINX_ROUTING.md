# FB Recipe Nginx Routing Fix - 2026-07-26

## Summary
- Issue: `https://fb.newtalk.kr/unni-naengmyeon/recipes` was redirected by nginx to the Yeoljeong finance static app before the Next.js recipe access guard could run.
- Fix: `nginx-fb.conf` now routes `/unni-naengmyeon/`, `/login`, and `/_next/` to the `aads_dashboard` upstream for both HTTP and HTTPS server blocks.
- Existing finance app routes remain unchanged: `/`, `/api/v1/`, and `/static/` still use the finance app/API routing.

## Deployment
- Dashboard commit deployed: `c7e83ca1261e8d9faabec8e828459c90587dbd32`
- Nginx routing commit: `b988c26d8b06c0b6f680b2fce5b1c734fe9c4377`
- Applied live config: copied `nginx-fb.conf` to `/etc/nginx/conf.d/fb.conf`
- Reload: `docker exec aads-nginx nginx -s reload`

## Verification
- `docker exec aads-nginx nginx -t`: passed
- `https://fb.newtalk.kr/unni-naengmyeon/recipes`: `307` to `/login?redirect=%2Funni-naengmyeon%2Frecipes`
- `https://fb.newtalk.kr/login?redirect=%2Funni-naengmyeon%2Frecipes`: `200`
- `https://fb.newtalk.kr/static/apps/yeoljeong-finance/index.html?v=20260722.2135`: `200`
- `https://unni.newtalk.kr/unni-naengmyeon/recipes`: `307` to `https://fb.newtalk.kr/unni-naengmyeon/recipes`

## Notes
- `docs/HANDOVER.md` already had unrelated local dirty changes at the time of this fix, so this standalone report was added to keep the record isolated.
