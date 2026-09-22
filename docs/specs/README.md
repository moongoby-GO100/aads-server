# 정본 문서 버전 메타데이터

각 슬라이스의 `spec.md`, `plan.md`, `tasks.md` 최상단에는 다음 형식의 한 줄을 둡니다.

```html
<!-- spec-version: v1   updated: YYYY-MM-DD   source: spec plan tasks -->
```

- `spec-version`은 세 문서가 공유하는 정본 버전입니다.
- `updated`는 해당 문서의 마지막 Git 변경일(`git log -1 --format=%ad --date=short`)입니다.
- `source`는 정본 문서 묶음을 식별합니다.

AADS, GO100, SF, NTV2, ACCT 전 프로젝트는 이 형식을 공통으로 적용합니다. 파이프라인 Analyze Gate는 참조된 슬라이스에 문서가 둘 이상 있으면 버전을 비교하며, 서로 다를 때 구현 금지 결과를 추가합니다. 헤더가 없는 문서는 메타데이터 누락으로만 보고합니다.
