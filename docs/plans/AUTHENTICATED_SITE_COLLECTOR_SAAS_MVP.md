# Authenticated Site Collector SaaS MVP

상세 제품·구현 계획은 [`reports/20260903_logged_in_site_collection_platform_plan.md`](../../reports/20260903_logged_in_site_collection_platform_plan.md)를 정본으로 한다.

사용자 진입점은 Dashboard `/authenticated-collector`, API 진입점은 `/api/v1/authenticated-site-collector`다. 공식 API와 수동 내보내기를 우선하며, 인증 챌린지는 자동 우회하지 않고 사용자 조치 후 동일 세션을 재개한다.
