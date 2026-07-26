# 20260726 FB Recipe Login Routing

- Time: 2026-07-26 20:42 KST
- Scope: `fb.newtalk.kr` recipe login routing for Unni Naengmyeon staff recipe page.
- Cause: `fb.newtalk.kr/login` was proxied to the AADS dashboard login, so unauthenticated recipe access looked like AADS login instead of the FB store assistant login.
- Change: `nginx-fb.conf` routes `/login` to `/static/apps/yeoljeong-finance/index.html` while preserving `redirect`. The FB store assistant app stores the recipe redirect and returns to `/unni-naengmyeon/recipes` after successful login/signup.
- Runtime action: updated `/etc/nginx/conf.d/fb.conf` and reloaded `aads-nginx`.
- Verification:
  - Inline JavaScript syntax check passed.
  - `docker exec aads-nginx nginx -t` passed.
  - `https://unni.newtalk.kr/unni-naengmyeon/recipes` now redirects to `https://fb.newtalk.kr/static/apps/yeoljeong-finance/index.html?redirect=%2Funni-naengmyeon%2Frecipes`.
  - Remote FB app HTML contains the post-login redirect handler.
