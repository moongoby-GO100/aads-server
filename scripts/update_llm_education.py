#!/usr/bin/env python3
"""LLM 서버 교육자료 최신 정보 업데이트 스크립트 (2026-09-12 v3)"""

filepath = '/app/app/static/reports/20260912_llm_server_comprehensive_education.html'

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

original_len = len(content)
changes = 0

def replace_once(old, new, label=""):
    global content, changes
    if old in content:
        content = content.replace(old, new, 1)
        changes += 1
        print(f"  [OK] {label}")
    else:
        print(f"  [SKIP] {label} — not found")

# ===== 1. Ch2: 상용 모델명 업데이트 =====
replace_once(
    '최상위 상용 모델(GPT-4o, Claude Opus) 대비 성능 차이',
    '최상위 상용 모델(GPT-5, Claude Opus 5) 대비 성능 차이',
    'Ch2 상용모델명'
)

# ===== 2. Ch3: GB300 NVL72 상태 =====
replace_once(
    'Blackwell Ultra, 1,400W, Q3 2025 출하 시작. 단일 GPU가 기존 Blackwell 전체 랙(20 PFLOPS FP4)과 동급 성능을 냄',
    'Blackwell Ultra, 1,400W, 2025 H2 출하 시작·양산 중 (DGX GB300 공급). 단일 GPU가 기존 Blackwell 전체 랙(20 PFLOPS FP4)과 동급 성능',
    'Ch3 GB300 상태'
)

# ===== 3. Ch3: Rubin R100 로드맵 테이블 =====
replace_once(
    '<td class="perf">2026 H2 예정</td><td>TSMC 3nm</td><td>HBM4 288GB, 22 TB/s 대역폭</td><td>Dual-die 패키지, 50 PFLOPS FP4, NVLink 6 (3.6 TB/s), CPU는 Vera, 가격 미정</td>',
    '<td class="perf">2026 H2 초도 / 2027 양산</td><td>TSMC 3nm</td><td>HBM4 288GB+, 22 TB/s 대역폭</td><td>Vera Rubin 플랫폼, Dual-die 패키지, 50 PFLOPS FP4, NVLink 6 (3.6 TB/s), CPU는 Vera, 프로토타입 NVL72 가동 확인(2026.08), 원가의 62%가 HBM4E 메모리</td>',
    'Ch3 Rubin R100 로드맵'
)

# ===== 4. Ch3: GPU 구매 시점 판단 =====
replace_once(
    'Rubin R100은 2026 하반기 출시 예정입니다',
    'Vera Rubin R100은 프로토타입이 가동 중이며 2027 상반기 양산 예정입니다',
    'Ch3 GPU구매시점'
)

# ===== 5. Ch3: AMD MI350 상태 =====
replace_once(
    'CDNA 4, 3nm, 185B 트랜지스터, 1,400W TBP, 2025 중반 출시',
    'CDNA 4, 3nm, 185B 트랜지스터, 1,400W TBP, 2026년 6월 발표·Q3 2026 공급 시작',
    'Ch3 MI350 상태'
)

# ===== 6. Ch3: AMD MI400 시기 =====
replace_once(
    'CDNA 5, 2nm, MI450X(AI)/MI430X(HPC) 분리, 2026 H2 출시 예정',
    'CDNA 5, 2nm, MI450X(AI)/MI430X(HPC) 분리, 2027 출시 예정',
    'Ch3 MI400 시기'
)

# ===== 7. Ch3: Apple M6 실제 출시 정보 =====
replace_once(
    '<td><strong>Apple M6</strong></td><td class="perf">통합 (용량 미정)</td><td class="price">가격 미정 (Mac mini 탑재)</td><td>Mac mini 탑재, M5 Ultra와 함께 2026.08.25 발표</td><td>7B Q4 입문용</td>',
    '<td><strong>Apple M6</strong></td><td class="perf">최대 32GB 통합</td><td class="price">Mac mini 기본 ₩899,000~<br><small style="color:var(--text3)">(약 $650~)</small></td><td>3nm 공정, Mac mini 탑재(M5 Pro와 선택), 2026.08.27 출시, M4에서 M6로 세대 스킵, Wi-Fi 7·BT 6, 16코어 NPU</td><td>7B Q4~Q6, AI 입문 워크스테이션</td>',
    'Ch3 M6 출시정보'
)

# ===== 8. Ch3: 로드맵 요약 - NVIDIA =====
replace_once(
    'Blackwell(B200/B300, 2024~2025) → <strong>Rubin(R100, 2027)</strong>',
    'Blackwell(B200/B300, 2024~2025) → <strong>Vera Rubin(R100, 2026H2~2027)</strong>',
    'Ch3 로드맵 NVIDIA'
)

# ===== 9. Ch3: 로드맵 요약 - AMD =====
replace_once(
    'CDNA 4(MI350, 2025) → <strong>CDNA 5(MI400, 2026 H2)</strong>',
    'CDNA 4(MI350, 2026 Q3 공급) → <strong>CDNA 5(MI400, 2027)</strong>',
    'Ch3 로드맵 AMD'
)

# ===== 10. Ch3: 로드맵 요약 - Apple =====
replace_once(
    'M5 Max/Ultra(2026-08)</strong> → M6(2026-08) · M7(미정)',
    'M5 Max/Ultra(2026-08)</strong> → <strong>M6(2026-08 출시)</strong> · M7(미정)',
    'Ch3 로드맵 Apple'
)

# ===== 11. Ch3: 차세대 하드웨어 테이블 - Rubin R100 =====
replace_once(
    '<td><strong>NVIDIA Rubin R100</strong></td><td class="perf">HBM4 (용량 미확정)</td><td class="price">미공개<br><small style="color:var(--text3)">(GTC 2026 발표)</small></td><td>2027 H1 양산</td><td class="recommend">Blackwell 대비 추론 비용 1/10, 학습 GPU 1/4</td>',
    '<td><strong>NVIDIA Vera Rubin R100</strong></td><td class="perf">HBM4 288GB+</td><td class="price">미공개<br><small style="color:var(--text3)">(GTC 2026 발표)</small></td><td>2026 H2 초도·2027 양산</td><td class="recommend">Blackwell 대비 추론 비용 1/10, 학습 GPU 1/4, 프로토타입 NVL72 가동(2026.08), SpaceX 등 초기 고객</td>',
    'Ch3 차세대 Rubin'
)

# ===== 12. Ch3: 차세대 - Vera Rubin 플랫폼 =====
replace_once(
    '<td><strong>NVIDIA Vera Rubin 플랫폼</strong></td><td class="perf">통합 랙 시스템</td><td class="price">미공개</td><td>2027~</td><td>R100 GPU + Vera CPU 통합, 7개 칩 유기적 작동</td>',
    '<td><strong>NVIDIA Vera Rubin 플랫폼</strong></td><td class="perf">통합 랙 시스템</td><td class="price">미공개</td><td>2027 H1~</td><td>R100 GPU + Vera CPU 통합, HBM4E 메모리 비중 62% (GB300 대비 +9%p), 7개 칩 유기적 작동</td>',
    'Ch3 차세대 Vera Rubin 플랫폼'
)

# ===== 13. Ch3: 차세대 - AMD MI400 =====
replace_once(
    '<td><strong>AMD MI400</strong></td><td class="perf">HBM4</td><td class="price">미공개</td><td>2026 H2</td><td>CDNA 5, 삼성 HBM4 탑재 예정, Rubin 세대 경쟁</td>',
    '<td><strong>AMD MI400</strong></td><td class="perf">HBM4</td><td class="price">미공개</td><td>2027</td><td>CDNA 5, 2nm, MI450X(AI)/MI430X(HPC) 분리 예정, Vera Rubin 세대 경쟁</td>',
    'Ch3 차세대 MI400'
)

# ===== 14. Ch7: Qwen 3 다운로드 수 업데이트 =====
replace_once(
    '<td>최신, 하이브리드 사고, 119개 언어</td>',
    '<td>하이브리드 사고, 119개 언어, 누적 11억+ 다운로드(2026.03), 글로벌 오픈소스 50%+ 점유</td>',
    'Ch7 Qwen3 다운로드'
)

# ===== 15. Ch7: Qwen3-Next 뒤에 Qwen 3.5/3.8 추가 =====
replace_once(
    '        <tr><td><strong>Qwen3-Next-80B-A3B</strong></td><td>Alibaba</td><td>80B (MoE, 활성 3B)</td><td>Apache 2.0</td><td>초경량 MoE, 에이전트 워크로드 특화</td><td>변동 (활성 3B 기준)</td></tr>',
    '''        <tr><td><strong>Qwen3-Next-80B-A3B</strong></td><td>Alibaba</td><td>80B (MoE, 활성 3B)</td><td>Apache 2.0</td><td>초경량 MoE, 에이전트 워크로드 특화</td><td>변동 (활성 3B 기준)</td></tr>
        <tr><td><strong>Qwen 3.5-397B-A17B</strong></td><td>Alibaba</td><td>397B (MoE, 활성 17B)</td><td>Apache 2.0</td><td class="recommend">네이티브 멀티모달 에이전트 모델, 이미지·비디오 이해·생성, 도구 호출 최적화</td><td>변동</td></tr>
        <tr><td><strong>Qwen 3.8-Max</strong></td><td>Alibaba</td><td>비공개 (플래그십)</td><td>오픈웨이트 (2026.08 복귀)</td><td class="recommend">2026.08 출시, 최신 플래그십, 비공개→오픈웨이트 전환으로 주가 7%↑</td><td>-</td></tr>
        <tr><td><strong>Qwen 3.8-27B</strong></td><td>Alibaba</td><td>27B (Dense)</td><td>Apache 2.0</td><td>262K 기본 컨텍스트(YaRN 1M), FP8 양자화, 이미지·영상 이해, 프로덕션급</td><td>~54GB</td></tr>''',
    'Ch7 Qwen 3.5/3.8 추가'
)

# ===== 16. Ch7: DeepSeek R2 업데이트 =====
replace_once(
    '<tr><td><strong>DeepSeek R2</strong></td><td>DeepSeek</td><td>비공개 (MoE 추정)</td><td>MIT</td><td>R1 후속, 추론/사고 강화</td><td>변동</td></tr>',
    '''<tr><td><strong>DeepSeek R2</strong></td><td>DeepSeek</td><td>대규모 (MoE)</td><td>MIT</td><td>R1 후속, 추론/사고 강화, 2026 출시 확인</td><td>변동</td></tr>
        <tr><td><strong>DeepSeek V4</strong></td><td>DeepSeek</td><td>대규모 (MoE)</td><td>MIT</td><td class="recommend">2026 최신, V3 후속 범용 모델, mHC(다중 헤드 잠재 주의) 안정화 기법 적용</td><td>변동</td></tr>''',
    'Ch7 DeepSeek R2/V4'
)

# ===== 17. Ch7: Kimi K2 업데이트 + K2.5/K3 추가 =====
replace_once(
    '        <tr><td><strong>Kimi K2</strong></td><td>Moonshot AI (중국)</td><td>대규모 (MoE)</td><td>Modified MIT</td><td>2025.07 출시, 대규모 MoE</td><td>변동</td></tr>',
    '''        <tr><td><strong>Kimi K2</strong></td><td>Moonshot AI (중국)</td><td>대규모 (MoE)</td><td>Modified MIT</td><td>2025.07 출시, 실리콘밸리 기준 모델로 인정, $2B 투자 유치(2026.05)</td><td>변동</td></tr>
        <tr><td><strong>Kimi K2.5</strong></td><td>Moonshot AI</td><td>대규모 (MoE)</td><td>Modified MIT</td><td>2026.01 출시, 시각이해·코딩·에이전트 통합, Kimi Claw(클라우드 코딩) 기반</td><td>변동</td></tr>
        <tr><td><strong>Kimi K2.7-Code</strong></td><td>Moonshot AI</td><td>대규모</td><td>Modified MIT</td><td>코딩 특화, K2.6 대비 사고 토큰 30% 절감, 장기 에이전트 코딩 최적화</td><td>변동</td></tr>
        <tr><td><strong>Kimi K3</strong></td><td>Moonshot AI</td><td>대규모</td><td>비공개</td><td class="recommend">2026.08 최신, 글로벌 고성능 모델 경쟁 진입</td><td>변동</td></tr>''',
    'Ch7 Kimi K2~K3'
)

# ===== 18. Ch7: EXAONE 업데이트 + K-EXAONE 2.0 / SKT A.X K2 추가 =====
replace_once(
    '        <tr><td><strong>EXAONE 3.5</strong></td><td>LG AI Research</td><td>2.4B / 7.8B / 32B</td><td>EXAONE License</td><td class="recommend">한국어 최강급, 비즈니스 특화</td><td>5 / 16 / 64 GB</td></tr>',
    '''        <tr><td><strong>EXAONE 3.5</strong></td><td>LG AI Research</td><td>2.4B / 7.8B / 32B</td><td>EXAONE License</td><td>한국어 강점, 비즈니스 특화</td><td>5 / 16 / 64 GB</td></tr>
        <tr><td><strong>EXAONE 4.0 / K-EXAONE 2.0</strong></td><td>LG AI Research</td><td>비공개</td><td>EXAONE License</td><td class="recommend">EXAONE 4.0: 글로벌 오픈 모델 4위(2025), K-EXAONE 2.0: 국가대표 AI 경쟁 모델(2026.08)</td><td>변동</td></tr>
        <tr><td><strong>SKT A.X K2</strong></td><td>SK텔레콤</td><td>비공개</td><td>비공개</td><td class="recommend">한국어·수학 벤치마크 글로벌 오픈소스 최고 수준, IMO 2026 금메달 기준 점수 달성</td><td>변동</td></tr>''',
    'Ch7 EXAONE/SKT'
)

# ===== 19. Ch15: 비용 비교 테이블 - 모델명 =====
replace_once(
    '<th>클라우드 API (GPT-4o)</th><th>클라우드 API (Claude Sonnet)</th>',
    '<th>클라우드 API (GPT-5)</th><th>클라우드 API (Claude Sonnet 5)</th>',
    'Ch15 비용 헤더'
)

replace_once(
    '<td>GPT-4o (비공개)</td><td>Claude 3.5 Sonnet</td>',
    '<td>GPT-5 (비공개)</td><td>Claude Sonnet 5</td>',
    'Ch15 구동모델'
)

# ===== 20. Ch15: 하이브리드 전략 모델명 =====
replace_once(
    '상용 API (Claude/GPT-4o)',
    '상용 API (Claude/GPT-5)',
    'Ch15 하이브리드 Claude/GPT'
)

replace_once(
    '상용 API (Opus/o1)',
    '상용 API (Opus 5/o3)',
    'Ch15 하이브리드 Opus/o1'
)

# ===== 21. Ch19: AADS 현재 적용 현황 =====
replace_once(
    '<td>Anthropic Claude (Opus/Sonnet)</td>',
    '<td>Anthropic Claude (Opus 5/Sonnet 5)</td>',
    'Ch19 AADS 주력모델'
)

# ===== 22. Ch20: 참고자료 업데이트 =====
replace_once(
    'M5/M5 Pro/M5 Max/M5 Ultra/M6 발표 자료 [Apple Newsroom, 2026-08]',
    'M5/M5 Pro/M5 Max/M5 Ultra/M6 발표·출시 자료 [Apple Newsroom, 2026-08]',
    'Ch20 Apple 참고'
)

replace_once(
    'B200/B300/GB200/GB300 사양 및 로드맵 [NVIDIA 공식, 2025-2026]',
    'B200/B300/GB200/GB300/DGX GB300 사양 및 Vera Rubin 로드맵 [NVIDIA 공식, 2025-2026]',
    'Ch20 NVIDIA 참고'
)

# 새 참고자료 추가
replace_once(
    '    <li><strong>VESSL AI Blog</strong> — vessl.ai/blog — B200 클라우드 임대 가격 비교 [VESSL AI Blog, 2026-04]</li>',
    '''    <li><strong>VESSL AI Blog</strong> — vessl.ai/blog — B200 클라우드 임대 가격 비교 [VESSL AI Blog, 2026-04]</li>
    <li><strong>AMD Advancing AI 2026</strong> — amd.com/instinct — MI350 시리즈 공식 발표, Q3 2026 공급 시작 [AMD 공식, 2026-06]</li>
    <li><strong>Moonshot AI</strong> — kimi.ai — Kimi K2/K2.5/K2.7-Code/K3 모델 시리즈 [Moonshot AI, 2025-2026]</li>
    <li><strong>Qwen Blog</strong> — qwenlm.github.io — Qwen 3/3.5/3.8 시리즈, 글로벌 오픈소스 50%+ 점유 [Alibaba, 2026]</li>
    <li><strong>DeepSeek</strong> — deepseek.com — DeepSeek R2/V4 모델 시리즈 [DeepSeek, 2026]</li>''',
    'Ch20 새 참고자료'
)

# ===== 23. 핵심 논문 추가 =====
replace_once(
    '    <li><strong>Mixture of Experts</strong> (Shazeer et al., 2017; Fedus et al., 2022) — MoE 효율성</li>',
    '''    <li><strong>Mixture of Experts</strong> (Shazeer et al., 2017; Fedus et al., 2022) — MoE 효율성</li>
    <li><strong>DeepSeek-V3</strong> (DeepSeek, 2024) — Multi-head Latent Attention, MoE 최적화</li>
    <li><strong>Qwen3 Technical Report</strong> (Alibaba, 2025) — 하이브리드 사고(Thinking/Non-Thinking) 모드</li>''',
    'Ch20 논문추가'
)

# ===== 24. 푸터 업데이트 =====
replace_once(
    'AADS Academy · LLM 서버 구축 종합 교육자료 · 2026-09-12 (2026-09-12 업데이트: 최신 하드웨어·원화 가격 추가) · CEO 교육용',
    'AADS Academy · LLM 서버 구축 종합 교육자료 · 2026-09-12 (v3: Vera Rubin·M6 출시·Qwen 3.5/3.8·DeepSeek V4·Kimi K3·K-EXAONE 2.0·SKT A.X K2 반영) · CEO 교육용',
    '푸터 버전'
)

replace_once(
    '출처가 명시되지 않은 가격·수치는 2025~2026년 시점의 공개 자료 기준이며 변동 가능합니다.',
    '출처가 명시되지 않은 가격·수치는 2025~2026년 9월 시점의 공개 자료 기준이며 변동 가능합니다.',
    '푸터 시점'
)

# ===== 저장 =====
with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

new_len = len(content)
new_lines = content.count('\n') + 1
print(f"\n=== 완료 ===")
print(f"변경 항목: {changes}건")
print(f"파일 크기: {original_len:,} → {new_len:,} chars ({new_len - original_len:+,})")
print(f"줄 수: {new_lines}")
