# AADS Reports 404 Recovery - 2026-06-04

## Summary

- Request: make `https://aads.newtalk.kr/reports/newtalk-ai-fashion-influencer-plan-v1.html` and related HTML planning reports viewable in the browser.
- Result: recovered public report delivery. The target URL and all 19 public report HTML files now return `200`.
- Verification time: 2026-06-04 09:58-10:00 KST.

## Cause

The report HTML existed in repository/public asset locations, but the active public static directory used by `aads.newtalk.kr` did not have the expected report file available under `/var/www/aads-public/reports/`.

The effective user-facing failure was therefore a public static file placement issue, not a missing source report.

## Action Taken

- Synchronized report HTML files into `/var/www/aads-public/reports/`.
- Confirmed the target public file exists:
  - `/var/www/aads-public/reports/newtalk-ai-fashion-influencer-plan-v1.html`
  - size: `42514` bytes
- Confirmed the source copies exist:
  - `/root/aads/aads-server/reports/newtalk-ai-fashion-influencer-plan-v1.html`
  - `/root/aads/aads-dashboard/public/reports/newtalk-ai-fashion-influencer-plan-v1.html`

## Verification

Target URL:

```text
curl -I -L --max-time 15 https://aads.newtalk.kr/reports/newtalk-ai-fashion-influencer-plan-v1.html
HTTP/1.1 200 OK
Content-Type: text/html
```

Body check:

```text
<title>뉴톡 AI 패션 인플루언서 사업기획서 v1</title>
```

Browser Bridge snapshot confirmed the rendered page content starts with:

```text
뉴톡 AI 패션 인플루언서 사업기획서
```

All public report HTML files checked:

```text
19 files, all returned HTTP 200
```

## Known Tool Issue

`capture_screenshot` failed with:

```text
Argument list too long: 'ssh'
```

Fallback verification was completed with Browser Bridge ARIA snapshot and direct `curl` checks.

## Deployment And Git Status

- Runtime deployment: completed by placing files in `/var/www/aads-public/reports/`.
- Service restart: not required.
- Code change: none.
- Documentation: this report file records the recovery.
