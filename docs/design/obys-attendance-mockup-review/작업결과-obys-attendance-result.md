# 오비서 근태 현황 목업 작업 결과

document_key: obys-attendance-mockup-review (ACCT 정본 rev1, revision_id c46f0c01-05a4-40da-ad19-c79acab4cdd1)

## 등록
- ACCT 목업 검토 620f9a77-b1a7-4ce2-ba71-a732faa4c120, revision c73a1a19-284d-448c-9ddc-a0ca89f52373 (rev1), 상태 review_ready(검토대기). CEO 목업 승인 전.
- 서버 자산 저장소: internal://mockup_assets/AADS/obys-attendance-v1/ (이 폴더와 같은 파일, 해시는 manifest.json)

## 검증 (2026-10-28 KST, contabo116)
- assets/verify.cjs: Chromium PC 1440x1000·모바일 390x844 통과, console/pageerror 0, 외부 요청 0, 가로 넘침 0, 캡처 20장.
- 모바일 넘침 수정: label·input·select 에 min-width:0 (검색 입력이 2열 grid 칸을 넘던 문제).
- assets/before-reconstruct.cjs: origin/main admin.html#attendance 를 가상 샘플 4건으로 렌더링(실 API·로그인 없음) → before/.

## 경계
가상 직원 A~D 샘플만 사용. 운영 화면 변경·배포 없음. 승인 전 근태 화면 추가 변경 금지.
