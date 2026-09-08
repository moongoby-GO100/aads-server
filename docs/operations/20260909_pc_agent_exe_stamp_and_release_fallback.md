# PC Agent EXE 최신판정 스탬프화 + Release 404 폴백 차단 (2026-09-09 KST)

## 배경
- 운영 다운로드에서 EXE 대신 ZIP/GitHub 404가 노출되는 사고가 반복됐다.
- 근본 원인: `_local_pc_agent_exe_is_current()`가 `dist/kakaobot-setup.exe`와 `pc_agent/VERSION`의 mtime 비교만으로 최신 여부를 판정했다.
  배포/복사/touch 순서에 따라 EXE가 VERSION보다 오래된 것으로 오판되면 GitHub Release로 307 리다이렉트했고,
  해당 태그의 Release 자산이 없으면 사용자에게 GitHub 404가 그대로 노출됐다.

## 변경 내용

### app/api/kakao_bot.py
- `PC_AGENT_EXE_STAMP_SUFFIX = ".version"`, `_pc_agent_exe_stamp_file()` 추가.
- `_local_pc_agent_exe_is_current()` 판정 순서
  1. `dist/kakaobot-setup.exe.version` 내용 == `pc_agent/VERSION` -> 최신
  2. 스탬프 없음(레거시 배포본) -> 기존 mtime 비교 폴백
- `_release_asset_available(url)` 추가: GitHub Release 자산 HEAD probe(timeout 3초, 결과 5분 캐시).
- `agent_download_exe()`
  - 로컬 EXE 없음 -> 기존과 동일하게 Release 307 리다이렉트
  - 로컬 EXE 있으나 stale -> Release 자산이 실제 존재할 때만 리다이렉트. 없으면 로컬 EXE를 `X-PC-Agent-Exe-Stale: true` 헤더와 함께 스트리밍
  - 정상 응답에는 `X-PC-Agent-Exe-Stale: false`

### pc_agent/build_exe.py
- 빌드 성공 시 `_write_build_stamp()` 호출: 스탬프 파일 기록 + EXE/스탬프 mtime 현재 시각 보정.

### tests/unit/test_pc_agent_download.py
- 스탬프 일치(mtime 역전에도 최신), 스탬프 버전 불일치, Release 없음->로컬 제공, Release 있음->리다이렉트, 빌드 스크립트 스탬프 기록 총 5건 추가.

## 검증
- `docker exec -w /app aads-server python3 -m pytest tests/unit/test_pc_agent_download.py -q` -> 11 passed
- `docker exec -w /app aads-server python3 -m pytest tests/unit/test_tools_and_pipeline.py -q` -> 64 passed
- 운영 dist에 `kakaobot-setup.exe.version = 1.0.73` 스탬프 생성

## 운영 주의
- `dist/`는 git 제외 경로이므로 EXE를 서버에 수동 배치할 때 스탬프 파일도 함께 생성해야 한다.
- GitHub Actions(`.github/workflows/build-pc-agent.yml`)는 `pc_agent/**` 변경 시 Windows EXE Release를 생성한다.
