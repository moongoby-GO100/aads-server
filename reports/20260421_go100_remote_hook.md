# GO100 원격 Hook 확장 완료 보고

- **시각**: 2026-04-21 17:25 KST
- **Task**: AADS-xxx (CEO 지시: GO100 우선 반영)
- **대상**: `aads-server/app/services/tool_executor.py`
- **GitHub**: https://github.com/moongoby-GO100/aads-server/commit/eaf0287

## 배경
채팅창 직접 수정이 다수인 현실에서, GO100 파일을 sed/tee/write로 수정할 때 commit/push/재기동이 매번 수동이었음. AADS 전용 Hook(기존)을 GO100 원격 레포(`/root/kis-autotrade-v4`, 서버211)까지 확장.

## 변경 요약
1. `_post_file_modify_hook`에 **GO100 분기** 신규 추가 (91줄)
   - git add (파일 단위) → commit → push origin main (master 폴백)
   - push 성공 시 `systemctl restart go100` 자동 실행
   - `git_project_lock("GO100:kis-autotrade-v4")`로 KIS와 경합 방지
   - CHANGELOG: `/app/docs/CHANGELOG-go100-direct.md`
2. `_run_ai_code_review_go100` **신규 메서드** (102줄)
   - GO100 원격 diff 수집 → code_reviewer 호출 → FLAG/REQUEST_CHANGES 시 채팅 세션 경고
3. `_git_status_snapshot_go100` **신규 헬퍼** (36줄)
   - `git diff --name-only HEAD` + `git ls-files --others` 조합
4. `_run_remote_command` **GO100 감지 통합**
   - `_enable_hook_go100` 플래그, systemctl restart go100 자기참조 방지
   - 변경 감지 시 `_post_file_modify_hook("GO100", ..., repo="kis-autotrade-v4")`

## 실측 검증
| 단계 | 결과 |
|------|------|
| 구문 검증 | ✅ SYNTAX_OK |
| Hot-Reload | ✅ 54 modules reloaded |
| sed 수정 감지 (tracked) | ✅ run_cmd_hook_fire |
| git add/commit | ✅ `[main 7fc1f30c]` |
| git push origin main | ✅ `7fc1f30c..a39d513c → 7fc1f30c` |
| systemctl restart go100 | ✅ exit=0, 17:25:13 KST 재기동 |
| HTTP /health (8002) | ✅ 200 |
| AI 리뷰 | ✅ APPROVE 0.990 (qwen-turbo) |

## 주의사항 / 한계
- **KIS와 레포 공유**: 같은 `/root/kis-autotrade-v4` 레포이므로 파일 단위 `git add <path>`로 스코프 제한. `git add .` 금지.
- **Untracked 파일 수정**: 스냅샷이 filename 기반이라 `??` → `??` 전환은 감지 못함. 실제 수정 대상(tracked 소스파일)은 정상 감지.
- **AI 리뷰 DB 저장 실패**: `app.services.db` 모듈 부재로 경고 메시지 DB 저장 실패(기존 AADS 리뷰와 동일 이슈, Hook 자체에는 영향 없음).
- **SSH 안정성**: GO100 원격 명령은 SSH 경유(서버68→서버211). SSH 끊김 시 Hook 일부 실패 가능 — best-effort 정책으로 원래 명령 결과에 영향 없음.

## 다음 단계
- [ ] 프론트엔드 빌드 트리거 자동화 (dashboard와 동일 구조)
- [ ] KIS 원격 Hook 확장 (실거래 시스템 — 별도 승인 필요)
- [ ] SF/NTV2 원격 Hook 확장 (서버114)
