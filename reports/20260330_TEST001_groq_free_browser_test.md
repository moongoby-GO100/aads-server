# TEST-001 · Groq 무료 모델 브라우저 검증 (833a7bb4)

**일시**: 2026-03-30 KST (브라우저 직접 테스트)  
**환경**: `https://aads.newtalk.kr/chat` → 세션 **TEST-001**, URL 해시 `833a7bb4-d42a-46ad-ba38-2ba8a2b1c24a`  
**방법**: 모델 콤보에서 지정 모델 선택 → 짧은 사용자 메시지 전송 → 어시스턴트 말풍선 메타(모델 id) 확인

## 세션 컨텍스트 (측정값)

- 맥락 배너: **메모리 304건 · 토큰 약 21,988** · Compaction ❌  
- 이전 분석(`reports/20260330_groq_failure_root_cause.md`)과 같이, **고토큰 컨텍스트 + Groq on_demand TPM 한도** 조합 시 Groq 실패 → **`gemini-2.5-flash` 폴백** 가능성이 높음.

## 결과 요약

| UI 라벨 | 기대 백엔드 계열 | 실제 메타(어시스턴트) | 판정 |
|--------|------------------|----------------------|------|
| Qwen3 32B (무료) | Groq | `[gemini-2.5-flash]` | **폴백** (Groq 미사용) |
| Kimi K2 (무료) | Groq | `[gemini-2.5-flash]` | **폴백** |
| Llama 4 Scout (무료) | Groq | `[gemini-2.5-flash]` | **폴백** |
| Llama 3.3 70B (무료) | Groq | `[gemini-2.5-flash]` | **폴백** |
| GPT-OSS 120B (무료) | Groq | `[gemini-2.5-flash]` | **폴백** |
| Groq Compound (무료) | Groq | `[gemini-2.5-flash]` | **폴백** |
| Nemotron Free (무료) | (UI: 무료) | `[openrouter-nemotron-free]` | **폴백 아님** · OpenRouter 경로 |

- 위 **Groq 라벨 6종 + Qwen3** 모두 본문은 정상(예: `OK`)이었으나, **표시된 실제 모델은 전부 Gemini Flash 폴백**이었다.  
- **Nemotron Free**만 메타가 **`openrouter-nemotron-free`**로, Groq/Gemini 폴백이 아닌 **별도 라우트**로 응답함.

## 검증 메시지 예시 (세션 내 구분용)

- `[Groq테스트-Qwen3]`, `[Groq-Kimi]`, `[L4Scout]`, `[L3370]`, `[GPT120]`, `[Compound]`, `[Nemo]` 등 짧은 프롬프트로 구분.

## 결론 · 후속

1. **TEST-001(833a7bb4) 세션**에서는 UI에서 선택한 **Groq 무료 계열이 실제 Groq로 가지 않고 Gemini로 폴백**되는 것이 브라우저에서 재현됨.  
2. **원인 가설**: 기존 TPM/페이로드 리포트와 동일 — **맥락 토큰 과다** 등으로 Groq 요청 실패 후 `model_selector` 폴백.  
3. **대응 아이디어**: 동일 워크스페이스에서 **맥락 축소·compaction·새 세션** 후 Groq 재시도, 또는 **LiteLLM/Groq 한도·모델 매핑** 점검.  
4. Nemotron은 Groq가 아니라 **OpenRouter** 메타이므로, “무료 Groq 모델 일괄 테스트”에서 **제외하거나 별도 항목**으로 표기하는 것이 맞음.

## 변경 파일

- 본 문서: `reports/20260330_TEST001_groq_free_browser_test.md` (신규)

**배포**: 문서만 추가 — 앱 배포 불필요.
