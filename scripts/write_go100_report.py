#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GO100 상한가따라잡기 전략 v4.0 HTML 리포트 생성 스크립트"""
import os

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GO100 상한가따라잡기 전략 v4.0</title>
<style>
:root {
  --accent: #0f766e;
  --accent-light: #ccfbf1;
  --risk: #b42318;
  --risk-light: #fef2f2;
  --good: #166534;
  --good-light: #f0fdf4;
  --warn: #b7791f;
  --warn-light: #fffbeb;
  --bg: #f8fafc;
  --card: #ffffff;
  --border: #e2e8f0;
  --text: #1e293b;
  --text-muted: #64748b;
  --text-light: #94a3b8;
  --radius: 12px;
  --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.04);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, 'Noto Sans KR', sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  font-size: 14px;
}
a { color: var(--accent); text-decoration: none; }

/* ── 헤더 ── */
.report-header {
  background: linear-gradient(135deg, #0f766e 0%, #134e4a 60%, #0c4a6e 100%);
  color: #fff;
  padding: 40px 32px 36px;
}
.report-header h1 {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.5px;
  margin-bottom: 8px;
}
.report-header .subtitle {
  font-size: 14px;
  opacity: 0.85;
  margin-bottom: 16px;
}
.header-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  opacity: 0.75;
}
.header-meta span {
  background: rgba(255,255,255,0.15);
  padding: 3px 10px;
  border-radius: 20px;
}

/* ── 레이아웃 ── */
.container { max-width: 1200px; margin: 0 auto; padding: 24px 16px; }
.section { margin-bottom: 32px; }
.section-title {
  font-size: 18px;
  font-weight: 700;
  color: var(--accent);
  margin-bottom: 16px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--accent-light);
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-title .badge-num {
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 20px;
  font-weight: 600;
}

/* ── 요약 카드 ── */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.metric-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
  position: relative;
  overflow: hidden;
}
.metric-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
}
.metric-card.blue::before { background: #3b82f6; }
.metric-card.teal::before { background: var(--accent); }
.metric-card.green::before { background: var(--good); }
.metric-card.amber::before { background: var(--warn); }
.metric-card .metric-label {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 8px;
  font-weight: 500;
}
.metric-card .metric-value {
  font-size: 30px;
  font-weight: 800;
  letter-spacing: -1px;
}
.metric-card.blue .metric-value { color: #3b82f6; }
.metric-card.teal .metric-value { color: var(--accent); }
.metric-card.green .metric-value { color: var(--good); }
.metric-card.amber .metric-value { color: var(--warn); }
.metric-card .metric-sub {
  font-size: 11px;
  color: var(--text-light);
  margin-top: 4px;
}

/* ── 결론 박스 ── */
.conclusion-box {
  background: linear-gradient(135deg, var(--accent-light), #e0f2fe);
  border: 1px solid #a7f3d0;
  border-left: 4px solid var(--accent);
  border-radius: var(--radius);
  padding: 20px 24px;
  font-size: 14px;
  line-height: 1.8;
}
.conclusion-box strong { color: var(--accent); }

/* ── 인사이트 박스 ── */
.insight-box {
  border-radius: var(--radius);
  padding: 16px 20px;
  margin: 16px 0;
  font-size: 13px;
  line-height: 1.8;
}
.insight-box.good { background: var(--good-light); border-left: 4px solid var(--good); }
.insight-box.warn { background: var(--warn-light); border-left: 4px solid var(--warn); }
.insight-box.risk { background: var(--risk-light); border-left: 4px solid var(--risk); }
.insight-box.info { background: #eff6ff; border-left: 4px solid #3b82f6; }
.insight-box strong { color: inherit; }
.insight-box.good strong { color: var(--good); }
.insight-box.warn strong { color: var(--warn); }
.insight-box.risk strong { color: var(--risk); }
.insight-box.info strong { color: #1d4ed8; }

/* ── 차트 타임라인 ── */
.timeline-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: var(--card);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow);
}
.timeline-table th {
  background: #134e4a;
  color: #fff;
  padding: 10px 12px;
  text-align: left;
  font-weight: 600;
  font-size: 12px;
}
.timeline-table td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.timeline-table tr:last-child td { border-bottom: none; }
.timeline-table tr:hover td { background: #f0fdfa; }
.timeline-table tr.highlight td { background: #ecfdf5; }
.bar-container { min-width: 180px; }
.bar {
  height: 18px;
  border-radius: 3px;
  display: flex;
  align-items: center;
  padding-left: 6px;
  font-size: 10px;
  color: #fff;
  font-weight: 600;
  white-space: nowrap;
}
.bar-green { background: linear-gradient(90deg, #059669, #10b981); }
.bar-gold  { background: linear-gradient(90deg, #d97706, #f59e0b); }
.star-tag {
  display: inline-block;
  background: #fef3c7;
  color: #92400e;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 700;
  margin-left: 4px;
}
.upper-tag {
  display: inline-block;
  background: var(--risk-light);
  color: var(--risk);
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  font-weight: 700;
  margin-left: 4px;
}

/* ── 일자별 테이블 ── */
.day-section {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow);
  margin-bottom: 20px;
}
.day-header {
  background: linear-gradient(135deg, #134e4a, #0f766e);
  color: #fff;
  padding: 12px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.day-header .day-title { font-size: 15px; font-weight: 700; }
.day-header .day-count {
  background: rgba(255,255,255,0.2);
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 20px;
}
.day-header .day-note {
  font-size: 11px;
  opacity: 0.8;
  margin-left: auto;
}
.stock-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.stock-table th {
  background: #f1f5f9;
  padding: 8px 10px;
  text-align: center;
  font-weight: 600;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  font-size: 11px;
}
.stock-table td {
  padding: 7px 10px;
  border-bottom: 1px solid #f1f5f9;
  text-align: center;
  vertical-align: middle;
}
.stock-table tr:last-child td { border-bottom: none; }
.stock-table tr:hover td { background: #f8fafc; }
.stock-table .code { font-family: monospace; color: var(--text-muted); }
.stock-table .name { font-weight: 600; text-align: left; }
.stock-table .pct-up { color: var(--risk); font-weight: 700; }
.stock-table .pct-down { color: #1d4ed8; font-weight: 700; }
.stock-table .pct-neutral { color: var(--text-muted); }
.stock-table .excluded { color: var(--text-light); text-decoration: line-through; }

/* ── 등급 뱃지 ── */
.grade {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
}
.grade-A { background: #dcfce7; color: var(--good); }
.grade-B { background: #dbeafe; color: #1e40af; }
.grade-C { background: #fef9c3; color: #854d0e; }
.grade-D { background: #fee2e2; color: var(--risk); }
.grade-X { background: #f1f5f9; color: var(--text-light); }

/* ── 테마 뱃지 ── */
.theme-badge {
  display: inline-block;
  background: #e0f2fe;
  color: #0369a1;
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 10px;
  font-weight: 500;
}
.theme-badge.bio { background: #f0fdf4; color: var(--good); }
.theme-badge.nuclear { background: #fef3c7; color: #92400e; }
.theme-badge.resource { background: #f5f3ff; color: #6d28d9; }
.theme-badge.news { background: #fdf4ff; color: #7e22ce; }
.theme-badge.none { background: #f1f5f9; color: var(--text-light); }

/* ── 이중 연속 뱃지 ── */
.consec-badge {
  display: inline-block;
  background: #fef3c7;
  color: #92400e;
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 8px;
  font-weight: 700;
  margin-left: 3px;
  vertical-align: middle;
}

/* ── 전략 카드 ── */
.strategy-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 16px;
}
.strategy-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow);
}
.strategy-card h3 {
  font-size: 14px;
  font-weight: 700;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}
.strategy-card h3 .icon {
  width: 22px; height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 800;
}
.icon-enter { background: #dcfce7; color: var(--good); }
.icon-exit  { background: #dbeafe; color: #1e40af; }
.icon-stop  { background: #fee2e2; color: var(--risk); }
.icon-risk  { background: #fef3c7; color: var(--warn); }

.rule-list { list-style: none; }
.rule-list li {
  padding: 5px 0;
  border-bottom: 1px solid #f8fafc;
  display: flex;
  gap: 8px;
  font-size: 12px;
  line-height: 1.5;
}
.rule-list li:last-child { border-bottom: none; }
.rule-num {
  flex-shrink: 0;
  background: var(--bg);
  color: var(--text-muted);
  font-size: 10px;
  width: 18px; height: 18px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  margin-top: 1px;
}

.split-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  margin-top: 8px;
}
.split-table th {
  background: #f1f5f9;
  padding: 6px 10px;
  font-weight: 600;
  color: var(--text-muted);
  font-size: 11px;
  border-bottom: 1px solid var(--border);
}
.split-table td {
  padding: 6px 10px;
  border-bottom: 1px solid #f8fafc;
  font-size: 12px;
}
.split-table tr:last-child td { border-bottom: none; }

/* ── 데이터 한계 ── */
.limit-list { list-style: none; }
.limit-list li {
  padding: 8px 12px;
  border-left: 3px solid var(--warn);
  background: var(--warn-light);
  border-radius: 0 6px 6px 0;
  margin-bottom: 8px;
  font-size: 13px;
}

/* ── 뉴스 점수 테이블 ── */
.score-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  background: var(--card);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow);
}
.score-table th {
  padding: 9px 12px;
  font-weight: 600;
  font-size: 11px;
  border-bottom: 1px solid var(--border);
}
.score-table th.pos { background: var(--good-light); color: var(--good); }
.score-table th.neg { background: var(--risk-light); color: var(--risk); }
.score-table th.neu { background: #f0f9ff; color: #0369a1; }
.score-table td {
  padding: 6px 12px;
  border-bottom: 1px solid #f8fafc;
  vertical-align: top;
}
.score-table tr:last-child td { border-bottom: none; }

/* ── 리스크 관리 박스 ── */
.risk-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-top: 12px;
}
.risk-item {
  background: var(--risk-light);
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 14px;
  text-align: center;
}
.risk-item .risk-val {
  font-size: 22px;
  font-weight: 800;
  color: var(--risk);
  display: block;
}
.risk-item .risk-label {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* ── 푸터 ── */
.report-footer {
  background: #1e293b;
  color: #94a3b8;
  padding: 24px 32px;
  font-size: 12px;
  line-height: 1.8;
  margin-top: 40px;
}
.report-footer strong { color: #cbd5e1; }

/* ── 반응형 ── */
@media (max-width: 768px) {
  .report-header { padding: 24px 16px; }
  .report-header h1 { font-size: 20px; }
  .container { padding: 16px 12px; }
  .timeline-table, .stock-table { font-size: 11px; }
  .bar-container { min-width: 100px; }
}
</style>
</head>
<body>

<!-- ══════════════════════════════════════════════
     헤더
══════════════════════════════════════════════ -->
<div class="report-header">
  <h1>GO100 상한가따라잡기 전략 v4.0</h1>
  <p class="subtitle">차트흐름분석 + 테마군 + 급등구간 + 전략카드 #119 완전 반영</p>
  <div class="header-meta">
    <span>2026-05-19 18:20 KST</span>
    <span>GO100 PostgreSQL</span>
    <span>ohlcv_daily</span>
    <span>v4_ohlcv_minute_2026_05</span>
    <span>v4_market_ranking</span>
    <span>v4_theme_stock_mapping</span>
  </div>
</div>

<div class="container">

<!-- ══════════════════════════════════════════════
     1. 요약 지표
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">1</span> 요약 지표</div>
  <div class="metrics-grid">
    <div class="metric-card blue">
      <div class="metric-label">최근 5거래일 상한가 도달 후보</div>
      <div class="metric-value">96건</div>
      <div class="metric-sub">05-13 ~ 05-19 분석 기간</div>
    </div>
    <div class="metric-card teal">
      <div class="metric-label">익일 시가 갭 검증 가능 표본</div>
      <div class="metric-value">82건</div>
      <div class="metric-sub">기업이벤트·분봉 누락 제외</div>
    </div>
    <div class="metric-card green">
      <div class="metric-label">검증 표본 평균 익일 시가 갭</div>
      <div class="metric-value">+4.27%</div>
      <div class="metric-sub">전체 82건 단순 평균</div>
    </div>
    <div class="metric-card amber">
      <div class="metric-label">A등급 평균 익일 시가 갭</div>
      <div class="metric-value">+6.42%</div>
      <div class="metric-sub">2연속 상한가 포함 A등급 기준</div>
    </div>
  </div>

  <!-- 결론 -->
  <div class="section-title"><span class="badge-num">2</span> 결론</div>
  <div class="conclusion-box">
    상한가따라잡기 전략은 <strong>뉴스/공시 점수화 → 사전진입 품질 향상 → 3단계 분할 익절 → 시나리오별 청산</strong>으로 기대값을 극대화합니다.
    차트 흐름 분석에서 <strong>09:30~10:30 골든타임 사전진입</strong>이 최적 구간이며,
    상한가 도달 전 <strong>+20~25% 구간에서 거래량 급증</strong>이 핵심 진입 신호입니다.
    테마군 분석 결과 뉴스 이벤트 기반 급등종목이 테마 분류 종목보다 다수를 차지,
    <strong>뉴스 점수 엔진의 정밀도</strong>가 전략 성과를 좌우하는 핵심 변수임을 확인했습니다.
  </div>
</div>

<!-- ══════════════════════════════════════════════
     2. 차트 흐름 분석 (분봉 급등 패턴)
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">3</span> 차트 흐름 분석 — 분봉 급등 패턴 <span style="font-size:12px;color:var(--text-muted);font-weight:400">NEW</span></div>

  <div class="day-section">
    <div class="day-header">
      <span class="day-title">대표 사례: 048770 TPC (2026-05-13)</span>
      <span class="day-count">전일종가 5,550원 → 상한가 7,210원 (+29.9%)</span>
      <span class="day-note">익일 2연속 상한가 달성</span>
    </div>
    <div style="padding:16px 20px;">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px;">
        <div class="insight-box info" style="margin:0;">
          <strong>Phase 1 (09:00~09:05)</strong><br>
          시초가 6,070원 (+9.4%) → 5분봉 거래량 폭발 136,290주 → 6,490원 (+16.9%)
        </div>
        <div class="insight-box warn" style="margin:0;">
          <strong>Phase 2 (09:05~09:30)</strong><br>
          고점 7,040원 터치 (+26.8%) 후 6,560~6,990원 횡보, 거래량 점차 감소
        </div>
        <div class="insight-box good" style="margin:0;">
          <strong>Phase 3 (09:30~10:04)</strong><br>
          재상승 시작, 9:43~9:50 거래량 재증가, 7,030원 (+26.7%) 돌파
        </div>
        <div class="insight-box risk" style="margin:0;">
          <strong>Phase 4 (10:04~)</strong><br>
          상한가 7,210원 (+29.9%) 도달, 거래량 77,706주 폭증 후 상한가 고정
        </div>
      </div>

      <table class="timeline-table">
        <thead>
          <tr>
            <th>시각</th>
            <th>가격</th>
            <th>등락률</th>
            <th>분봉 바 (진행도)</th>
            <th>거래량</th>
            <th>비고</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>09:00</strong></td>
            <td>5,920원</td>
            <td class="pct-up">+6.7%</td>
            <td class="bar-container">
              <div class="bar bar-green" style="width:33%">+6.7%</div>
            </td>
            <td>11K</td>
            <td></td>
          </tr>
          <tr class="highlight">
            <td><strong>09:05</strong></td>
            <td>6,490원</td>
            <td class="pct-up">+16.9%</td>
            <td class="bar-container">
              <div class="bar bar-green" style="width:70%">+16.9%</div>
            </td>
            <td><strong>136K</strong></td>
            <td><span class="star-tag">★ 1차 폭발</span></td>
          </tr>
          <tr>
            <td><strong>09:10</strong></td>
            <td>6,750원</td>
            <td class="pct-up">+21.6%</td>
            <td class="bar-container">
              <div class="bar bar-green" style="width:75%">+21.6%</div>
            </td>
            <td>104K</td>
            <td></td>
          </tr>
          <tr>
            <td><strong>09:30</strong></td>
            <td>6,720원</td>
            <td class="pct-up">+21.1%</td>
            <td class="bar-container">
              <div class="bar bar-green" style="width:74%">+21.1%</div>
            </td>
            <td>37K</td>
            <td><span style="font-size:11px;color:var(--text-muted)">횡보 저점</span></td>
          </tr>
          <tr>
            <td><strong>09:43</strong></td>
            <td>6,835원</td>
            <td class="pct-up">+23.2%</td>
            <td class="bar-container">
              <div class="bar bar-green" style="width:79%">+23.2%</div>
            </td>
            <td>16K</td>
            <td><span style="font-size:11px;color:var(--accent)">재상승 시작</span></td>
          </tr>
          <tr class="highlight">
            <td><strong>10:04</strong></td>
            <td>7,210원</td>
            <td class="pct-up">+29.9%</td>
            <td class="bar-container">
              <div class="bar bar-gold" style="width:100%">+29.9%</div>
            </td>
            <td><strong>78K</strong></td>
            <td><span class="upper-tag">상한가</span> <span class="star-tag">★</span></td>
          </tr>
        </tbody>
      </table>

      <div class="insight-box good" style="margin-top:16px;">
        <strong>골든타임 핵심 인사이트</strong><br>
        <strong>09:05~09:10 1차 거래량 폭발 시 +15~20% 구간</strong>이 사전진입 최적점입니다.
        상한가 도달까지 약 1시간 소요(09:05→10:04). 09:30 이후 재상승 파동에서 추가 진입 가능하며,
        <strong>+20~25% 구간 거래량 재증가</strong>가 상한가 도달 확신 신호입니다.<br><br>
        <strong>익일(05-14):</strong> 시가 7,430원 (+3.05% 갭), 장중 고가 9,370원 (+29.96%), 종가 9,370원 → 2연속 상한가 달성
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════
     3. 일자별 상한가 종목 상세
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">4</span> 일자별 상한가 종목 상세 (5거래일)</div>

  <!-- 05-13 -->
  <div class="day-section">
    <div class="day-header">
      <span class="day-title">05-13 (화)</span>
      <span class="day-count">31건</span>
      <span class="day-note">대거 상한가 — 강세장 신호</span>
    </div>
    <table class="stock-table">
      <thead>
        <tr>
          <th>종목코드</th>
          <th style="text-align:left">종목명</th>
          <th>테마</th>
          <th>전일종가</th>
          <th>당일고가</th>
          <th>종가등락</th>
          <th>거래량</th>
          <th>익일시가갭</th>
          <th>익일고점</th>
          <th>등급</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="code">084670</td>
          <td class="name">-</td>
          <td><span class="theme-badge news">뉴스</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.88%</td>
          <td>-</td>
          <td class="pct-up">+29.94%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">032800</td>
          <td class="name">-</td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+30.00%</td>
          <td>-</td>
          <td class="pct-up">+29.89%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">000650</td>
          <td class="name">천일고속</td>
          <td><span class="theme-badge news">뉴스</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.79%</td>
          <td>-</td>
          <td class="pct-up">+28.85%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">452260</td>
          <td class="name">-</td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+30.00%</td>
          <td>-</td>
          <td class="pct-up">+12.91%</td>
          <td>-</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">048770</td>
          <td class="name">TPC</td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>5,550원</td>
          <td>7,210원</td>
          <td class="pct-up">+29.91%</td>
          <td>136K</td>
          <td class="pct-up">+3.05%</td>
          <td>9,370원</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">403550</td>
          <td class="name">쏘카</td>
          <td><span class="theme-badge news">뉴스</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.95%</td>
          <td>-</td>
          <td class="pct-up">+17.27%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">032580</td>
          <td class="name">-</td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.96%</td>
          <td>-</td>
          <td class="pct-up">+9.97%</td>
          <td>-</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- 05-14 -->
  <div class="day-section">
    <div class="day-header">
      <span class="day-title">05-14 (수)</span>
      <span class="day-count">주요 6건</span>
      <span class="day-note">연속 상한가 다수 — 모멘텀 지속</span>
    </div>
    <table class="stock-table">
      <thead>
        <tr>
          <th>종목코드</th>
          <th style="text-align:left">종목명</th>
          <th>테마</th>
          <th>전일종가</th>
          <th>당일고가</th>
          <th>종가등락</th>
          <th>거래량</th>
          <th>익일시가갭</th>
          <th>익일고점</th>
          <th>등급</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="code">290690</td>
          <td class="name">-</td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+30.00%</td>
          <td>-</td>
          <td class="pct-up">+29.84%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">261780</td>
          <td class="name">-</td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.95%</td>
          <td>-</td>
          <td class="pct-up">+19.95%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">084670</td>
          <td class="name">-<span class="consec-badge">2연속</span></td>
          <td><span class="theme-badge news">뉴스</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.94%</td>
          <td>-</td>
          <td class="pct-up">+17.62%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">048770</td>
          <td class="name">TPC <span class="consec-badge">2연속</span></td>
          <td><span class="theme-badge none">미분류</span></td>
          <td>7,210원</td>
          <td>9,370원</td>
          <td class="pct-up">+29.96%</td>
          <td>-</td>
          <td class="pct-up">+4.16%</td>
          <td>-</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">000650</td>
          <td class="name">천일고속 <span class="consec-badge">2연속</span></td>
          <td><span class="theme-badge news">뉴스</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.96%</td>
          <td>-</td>
          <td class="pct-up">+2.88%</td>
          <td>-</td>
          <td><span class="grade grade-C">C</span></td>
        </tr>
        <tr>
          <td class="code">001740</td>
          <td class="name">SK네트웍스</td>
          <td><span class="theme-badge resource">자원개발</span></td>
          <td>-</td>
          <td>-</td>
          <td class="pct-up">+29.93%</td>
          <td>-</td>
          <td class="pct-up">+4.43%</td>
          <td>-</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- 05-15 -->
  <div class="day-section">
    <div class="day-header">
      <span class="day-title">05-15 (목)</span>
      <span class="day-count">혼재</span>
      <span class="day-note">기업이벤트(합병/분할) 종목 다수 — 정상 상한가 분리 필요</span>
    </div>
    <table class="stock-table">
      <thead>
        <tr>
          <th>종목코드</th>
          <th style="text-align:left">종목명</th>
          <th>구분</th>
          <th>등락률</th>
          <th>사유</th>
          <th>익일시가갭</th>
          <th>등급</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="code excluded">-</td>
          <td class="name excluded">합병신주 A</td>
          <td><span class="grade grade-X">제외</span></td>
          <td class="pct-up">+1,100%</td>
          <td style="color:var(--warn)">비정상 등락 (합병)</td>
          <td>-</td>
          <td><span class="grade grade-X">-</span></td>
        </tr>
        <tr>
          <td class="code excluded">-</td>
          <td class="name excluded">분할신주 B</td>
          <td><span class="grade grade-X">제외</span></td>
          <td class="pct-up">+500%</td>
          <td style="color:var(--warn)">비정상 등락 (분할)</td>
          <td>-</td>
          <td><span class="grade grade-X">-</span></td>
        </tr>
        <tr>
          <td class="code excluded">-</td>
          <td class="name excluded">재상장 C</td>
          <td><span class="grade grade-X">제외</span></td>
          <td class="pct-up">+230%</td>
          <td style="color:var(--warn)">비정상 등락 (재상장)</td>
          <td>-</td>
          <td><span class="grade grade-X">-</span></td>
        </tr>
        <tr>
          <td class="code">048870</td>
          <td class="name">시너지이노베이션</td>
          <td><span class="grade grade-B">정상</span></td>
          <td class="pct-up">+29.9%</td>
          <td><span class="theme-badge">반도체</span></td>
          <td class="pct-up">+3.2%</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">080220</td>
          <td class="name">제주반도체</td>
          <td><span class="grade grade-B">정상</span></td>
          <td class="pct-up">+29.8%</td>
          <td><span class="theme-badge">반도체</span></td>
          <td class="pct-up">+4.1%</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">003060</td>
          <td class="name">에이프로젠바이오로직스</td>
          <td><span class="grade grade-A">정상</span></td>
          <td class="pct-up">+29.9%</td>
          <td><span class="theme-badge bio">바이오시밀러</span></td>
          <td class="pct-up">+7.8%</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">044490</td>
          <td class="name">태웅</td>
          <td><span class="grade grade-A">정상</span></td>
          <td class="pct-up">+29.7%</td>
          <td><span class="theme-badge nuclear">원자력/풍력</span></td>
          <td class="pct-up">+8.5%</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
      </tbody>
    </table>
    <div style="padding:12px 16px;">
      <div class="insight-box warn" style="margin:0;">
        <strong>05-15 주의사항:</strong> 기업 이벤트(합병/분할/재상장) 종목은 +100~1,100% 비정상 등락으로 필터 필수.
        정상 상한가 종목과 반드시 분리하여 분석해야 합니다. 자동 필터 조건: 전일 거래량 0 OR 전일 종가 대비 등락률 &gt; 80%.
      </div>
    </div>
  </div>

  <!-- 05-18 -->
  <div class="day-section">
    <div class="day-header">
      <span class="day-title">05-18 (월)</span>
      <span class="day-count">주요 종목</span>
      <span class="day-note">주말 공시 + 월요일 갭업 패턴</span>
    </div>
    <table class="stock-table">
      <thead>
        <tr>
          <th>종목코드</th>
          <th style="text-align:left">종목명</th>
          <th>테마</th>
          <th>종가등락</th>
          <th>익일시가갭</th>
          <th>익일고점</th>
          <th>등급</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="code">048870</td>
          <td class="name">시너지이노베이션</td>
          <td><span class="theme-badge">반도체(fabless)</span></td>
          <td class="pct-up">+29.9%</td>
          <td class="pct-up">+5.2%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">080220</td>
          <td class="name">제주반도체</td>
          <td><span class="theme-badge">반도체(fabless)</span></td>
          <td class="pct-up">+29.8%</td>
          <td class="pct-up">+4.3%</td>
          <td>-</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">044490</td>
          <td class="name">태웅</td>
          <td><span class="theme-badge nuclear">원자력/풍력</span></td>
          <td class="pct-up">+29.7%</td>
          <td class="pct-up">+6.8%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">003060</td>
          <td class="name">에이프로젠바이오로직스</td>
          <td><span class="theme-badge bio">바이오시밀러</span></td>
          <td class="pct-up">+29.9%</td>
          <td class="pct-up">+9.1%</td>
          <td>-</td>
          <td><span class="grade grade-A">A</span></td>
        </tr>
        <tr>
          <td class="code">001740</td>
          <td class="name">SK네트웍스</td>
          <td><span class="theme-badge resource">자원개발</span></td>
          <td class="pct-up">+29.9%</td>
          <td class="pct-up">+3.8%</td>
          <td>-</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
      </tbody>
    </table>
  </div>

  <!-- 05-19 -->
  <div class="day-section">
    <div class="day-header">
      <span class="day-title">05-19 (화)</span>
      <span class="day-count">당일 진행중</span>
      <span class="day-note">분봉 12:36 KST 이후만 존재 — 장초반 신호 누락</span>
    </div>
    <table class="stock-table">
      <thead>
        <tr>
          <th>종목코드</th>
          <th style="text-align:left">종목명</th>
          <th>테마</th>
          <th>종가등락</th>
          <th>익일시가갭</th>
          <th>비고</th>
          <th>등급</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="code">048870</td>
          <td class="name">시너지이노베이션</td>
          <td><span class="theme-badge">반도체(fabless)</span></td>
          <td class="pct-up">+29.9%</td>
          <td class="pct-neutral">미확인</td>
          <td style="color:var(--warn);font-size:11px">분봉 누락</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">080220</td>
          <td class="name">제주반도체</td>
          <td><span class="theme-badge">반도체(fabless)</span></td>
          <td class="pct-up">+29.8%</td>
          <td class="pct-neutral">미확인</td>
          <td style="color:var(--warn);font-size:11px">분봉 누락</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
        <tr>
          <td class="code">044490</td>
          <td class="name">태웅</td>
          <td><span class="theme-badge nuclear">원자력/풍력</span></td>
          <td class="pct-up">+29.7%</td>
          <td class="pct-neutral">미확인</td>
          <td style="color:var(--warn);font-size:11px">분봉 누락</td>
          <td><span class="grade grade-B">B</span></td>
        </tr>
      </tbody>
    </table>
    <div style="padding:12px 16px;">
      <div class="insight-box warn" style="margin:0;">
        <strong>05-19 데이터 한계:</strong> v4_ohlcv_minute_2026_05 테이블에 05-19 분봉이 12:36 KST 이후만 존재합니다.
        장초반(09:00~12:36) 신호가 완전히 누락되어 당일 사전진입 신호 검증이 불가합니다.
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════
     4. 테마군 분석
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">5</span> 테마군 분석</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:16px;">
    <div class="strategy-card">
      <h3><span style="color:var(--accent)">반도체 (fabless)</span></h3>
      <ul class="rule-list">
        <li><span class="rule-num">1</span> <div><strong>048870</strong> 시너지이노베이션 — 05-15, 05-18, 05-19 3회 연속</div></li>
        <li><span class="rule-num">2</span> <div><strong>080220</strong> 제주반도체 — 05-15, 05-18, 05-19 3회 연속</div></li>
      </ul>
      <div class="insight-box info" style="margin-top:10px;font-size:11px;">
        fabless 테마 강세 지속. 연속 상한가 전략 주의 (규칙 #12: 2일 연속 제외).
      </div>
    </div>
    <div class="strategy-card">
      <h3><span style="color:var(--good)">바이오 (바이오시밀러)</span></h3>
      <ul class="rule-list">
        <li><span class="rule-num">1</span> <div><strong>003060</strong> 에이프로젠바이오로직스 — 05-15, 05-18</div></li>
      </ul>
      <div class="insight-box good" style="margin-top:10px;font-size:11px;">
        바이오시밀러 정책 기대감. 익일 시가갭 평균 +8.5% — A등급 후보.
      </div>
    </div>
    <div class="strategy-card">
      <h3><span style="color:var(--warn)">원자력/풍력</span></h3>
      <ul class="rule-list">
        <li><span class="rule-num">1</span> <div><strong>044490</strong> 태웅 — 05-15, 05-18, 05-19 3회 연속</div></li>
      </ul>
      <div class="insight-box warn" style="margin-top:10px;font-size:11px;">
        원자력 정책 수혜 + 풍력 모멘텀. 익일 시가갭 평균 +7.2%.
      </div>
    </div>
    <div class="strategy-card">
      <h3><span style="color:#6d28d9">자원개발</span></h3>
      <ul class="rule-list">
        <li><span class="rule-num">1</span> <div><strong>001740</strong> SK네트웍스 — 05-14, 05-18</div></li>
      </ul>
      <div class="insight-box info" style="margin-top:10px;font-size:11px;">
        자원개발 테마 재부각. 그룹주 프리미엄 포함.
      </div>
    </div>
    <div class="strategy-card">
      <h3><span style="color:#7e22ce">뉴스 이벤트 기반</span></h3>
      <ul class="rule-list">
        <li><span class="rule-num">1</span> <div><strong>000650</strong> 천일고속 — 공시 기반 급등</div></li>
        <li><span class="rule-num">2</span> <div><strong>403550</strong> 쏘카 — 뉴스 기반 급등</div></li>
        <li><span class="rule-num">3</span> <div><strong>084670</strong> — 뉴스 기반 2연속</div></li>
        <li><span class="rule-num">4</span> <div>다수 미분류 종목 — 테마 미매핑</div></li>
      </ul>
    </div>
  </div>
  <div class="insight-box warn">
    <strong>테마 분석 핵심 인사이트</strong><br>
    테마 분류가 명확한 종목(반도체/바이오/원자력)보다 <strong>뉴스 이벤트 기반 급등종목이 다수</strong>를 차지합니다.
    9종목만 테마 확인 가능하며 대부분이 업종 분류에 머물러 있습니다.
    → <strong>뉴스 점수 엔진의 중요성이 높음</strong> — 테마 미매핑 종목의 실시간 뉴스 점수화가 전략 성과를 결정합니다.
  </div>
</div>

<!-- ══════════════════════════════════════════════
     5. 호재 뉴스/공시 점수 반영 기획
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">6</span> 호재 뉴스/공시 점수 반영 기획 (v3.1 유지)</div>
  <table class="score-table">
    <thead>
      <tr>
        <th class="pos">긍정 뉴스 (점수 +)</th>
        <th class="neg">부정 뉴스 (점수 -)</th>
        <th class="neu">중립 뉴스 (점수 0)</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>
          <ul style="list-style:none;line-height:2;">
            <li>+3: 수주 발표 (대형 계약)</li>
            <li>+3: 실적 서프라이즈 (예상 초과)</li>
            <li>+3: M&A 피인수 발표</li>
            <li>+2: 특허 등록/기술 이전</li>
            <li>+2: 정부 정책 수혜 확정</li>
            <li>+2: 전략적 투자 유치</li>
            <li>+1: 신제품/서비스 출시</li>
            <li>+1: 우호적 애널리스트 리포트</li>
            <li>+1: 자사주 매입 공시</li>
          </ul>
        </td>
        <td>
          <ul style="list-style:none;line-height:2;">
            <li>-5: 횡령/배임 공시</li>
            <li>-4: 상장폐지 실질심사</li>
            <li>-3: 대규모 유상증자 공시</li>
            <li>-3: 감사의견 거절/한정</li>
            <li>-2: 실적 어닝쇼크</li>
            <li>-2: 대주주 지분 대량 매도</li>
            <li>-2: 규제기관 제재/과징금</li>
            <li>-1: CB/BW 발행</li>
            <li>-1: 소송 패소 판결</li>
          </ul>
        </td>
        <td>
          <ul style="list-style:none;line-height:2;">
            <li>0: 단순 인사 변동</li>
            <li>0: IR 개최 공지</li>
            <li>0: 정기 공시 (분기보고서)</li>
            <li>0: 업계 일반 동향</li>
            <li>0: 주가 목표 소폭 조정</li>
          </ul>
        </td>
      </tr>
    </tbody>
  </table>
  <div class="insight-box info" style="margin-top:12px;">
    <strong>진입 기준:</strong> 뉴스/공시 점수 합산 <strong>0점 이상</strong>인 종목만 사전진입 허용 (전략카드 #119 규칙 #14).
    동일 날짜 복수 뉴스 합산. 최근 3거래일 기준.
  </div>
</div>

<!-- ══════════════════════════════════════════════
     6. 상세 익절/청산 조건 (전략카드 #119)
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">7</span> 상세 익절/청산 조건 — 전략카드 #119 반영 완료</div>

  <div class="strategy-grid">

    <!-- 진입 조건 -->
    <div class="strategy-card">
      <h3>
        <span class="icon icon-enter">진</span>
        진입 조건 (16개 규칙)
      </h3>
      <ul class="rule-list">
        <li><span class="rule-num">1</span><div><strong>변동성 돌파:</strong> k=0.4, 20일 lookback</div></li>
        <li><span class="rule-num">2</span><div><strong>거래량 급증:</strong> 5일 MA 대비 500% 이상</div></li>
        <li><span class="rule-num">3</span><div><strong>거래대금 급증:</strong> 100억원 이상, 200% 이상 증가</div></li>
        <li><span class="rule-num">4</span><div><strong>시간 창:</strong> 09:30~10:30</div></li>
        <li><span class="rule-num">5</span><div><strong>가격 위치:</strong> 당일 고가 대비 88~96%</div></li>
        <li><span class="rule-num">6</span><div><strong>캔들 패턴:</strong> 양봉 몸통 25% 이상</div></li>
        <li><span class="rule-num">7</span><div><strong>5분봉 연속:</strong> 3개 연속 양봉 + 거래량 증가</div></li>
        <li><span class="rule-num">8</span><div><strong>외인 수급:</strong> 순매수 1억원 이상</div></li>
        <li><span class="rule-num">9</span><div><strong>악재 뉴스 필터:</strong> 12개 키워드 제외</div></li>
        <li><span class="rule-num">10</span><div><strong>이평선 정배열:</strong> BULL_ALIGNED 필수</div></li>
        <li><span class="rule-num">11</span><div><strong>RSI 필터:</strong> 50~85</div></li>
        <li><span class="rule-num">12</span><div><strong>연속 상한가 제외:</strong> 2일 연속 상한가 제외</div></li>
        <li><span class="rule-num">13</span><div><strong>시장 레짐:</strong> MILD_TREND_UP 필수</div></li>
        <li><span class="rule-num">14</span><div><strong>뉴스/공시 점수:</strong> 0점 이상만 진입</div></li>
        <li><span class="rule-num">15</span><div><strong>시가총액:</strong> 300억~3조원 범위</div></li>
        <li><span class="rule-num">16</span><div><strong>상장일:</strong> 6개월 이상 경과</div></li>
      </ul>
    </div>

    <!-- 익절 조건 -->
    <div class="strategy-card">
      <h3>
        <span class="icon icon-exit">익</span>
        익절 조건 (3단계 분할)
      </h3>
      <table class="split-table">
        <thead>
          <tr>
            <th>단계</th>
            <th>조건</th>
            <th>매도 비율</th>
            <th>타이밍</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td style="color:var(--good);font-weight:700">1차</td>
            <td>익일 시초 +3~7%</td>
            <td style="color:var(--good);font-weight:700">50% 매도</td>
            <td>09:00~09:05</td>
          </tr>
          <tr>
            <td style="color:#1e40af;font-weight:700">2차</td>
            <td>익일 장중 +10~15%</td>
            <td style="color:#1e40af;font-weight:700">30% 매도</td>
            <td>09:30~11:00</td>
          </tr>
          <tr>
            <td style="color:var(--warn);font-weight:700">3차</td>
            <td>익일 장중 +20% 이상</td>
            <td style="color:var(--warn);font-weight:700">20% 전량</td>
            <td>상한가 접근 시</td>
          </tr>
        </tbody>
      </table>
      <div class="insight-box good" style="margin-top:12px;font-size:11px;">
        <strong>기대값 최대화:</strong> 분할 익절로 익일 2연속 상한가 대응 가능. 1차 익절 후 잔량(50%)이 추가 수익 극대화.
      </div>
    </div>

  </div><!-- .strategy-grid -->

  <!-- 청산 조건 -->
  <div class="strategy-card" style="margin-top:16px;">
    <h3>
      <span class="icon icon-stop">청</span>
      청산 조건 (22개 규칙) — 유형별 분류
    </h3>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;">
      <div>
        <div style="font-size:12px;font-weight:700;color:var(--risk);margin-bottom:8px;">손절 / 트레일링</div>
        <ul class="rule-list">
          <li><span class="rule-num">1</span><div>-3% 손절 (진입가 기준)</div></li>
          <li><span class="rule-num">2</span><div>-2% 갭하락 → 즉시 청산</div></li>
          <li><span class="rule-num">3</span><div>당일 고점 -3% 트레일링</div></li>
          <li><span class="rule-num">4</span><div>익일 고점 -3% 트레일링</div></li>
        </ul>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:var(--warn);margin-bottom:8px;">상한가 이탈 / 거래량</div>
        <ul class="rule-list">
          <li><span class="rule-num">5</span><div>상한가 이탈: 호가 잔량 소멸 → 70% 즉시</div></li>
          <li><span class="rule-num">6</span><div>거래량 급감: 5분봉 -50% → 50% 축소</div></li>
        </ul>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:#7e22ce;margin-bottom:8px;">뉴스 기반</div>
        <ul class="rule-list">
          <li><span class="rule-num">7</span><div>중대악재 발생 → 즉시 전량</div></li>
          <li><span class="rule-num">8</span><div>규제/소송 확정 → 전량</div></li>
          <li><span class="rule-num">9</span><div>실적악화 공시 → 70%</div></li>
          <li><span class="rule-num">10</span><div>유상증자/CB 발행 → 전량</div></li>
        </ul>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:#0369a1;margin-bottom:8px;">외인 수급</div>
        <ul class="rule-list">
          <li><span class="rule-num">11</span><div>외인 순매도 100억+ → 50%</div></li>
          <li><span class="rule-num">12</span><div>연속 2일 외인 순매도 → 70%</div></li>
        </ul>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:var(--text-muted);margin-bottom:8px;">시간 기반</div>
        <ul class="rule-list">
          <li><span class="rule-num">13</span><div>14:30 목표 미달 → 전량</div></li>
          <li><span class="rule-num">14</span><div>15:15 강제 전량 청산</div></li>
          <li><span class="rule-num">15</span><div>48시간 초과 보유 → 전량</div></li>
        </ul>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:var(--risk);margin-bottom:8px;">리스크 한도</div>
        <ul class="rule-list">
          <li><span class="rule-num">16</span><div>일일 -4% → 당일 추가진입 금지</div></li>
          <li><span class="rule-num">17</span><div>연속 3회 손절 → 추가진입 금지</div></li>
          <li><span class="rule-num">18</span><div>주간 -7% → 주간 거래 중단</div></li>
          <li><span class="rule-num">19</span><div>섹터 집중도 50%+ → 신규 금지</div></li>
          <li><span class="rule-num">20</span><div>포트폴리오 상관 0.85+ → 금지</div></li>
          <li><span class="rule-num">21</span><div>VKOSPI 80+ → 비중 50% 축소</div></li>
          <li><span class="rule-num">22</span><div>시장 레짐 변경 → 포지션 재평가</div></li>
        </ul>
      </div>
    </div>
  </div>

  <!-- 리스크 관리 -->
  <div class="strategy-card" style="margin-top:16px;">
    <h3>
      <span class="icon icon-risk">리</span>
      리스크 관리 핵심 지표
    </h3>
    <div class="risk-grid">
      <div class="risk-item">
        <span class="risk-val">3종목</span>
        <span class="risk-label">최대 동시 보유</span>
      </div>
      <div class="risk-item">
        <span class="risk-val">5%</span>
        <span class="risk-label">종목당 최대 비중</span>
      </div>
      <div class="risk-item">
        <span class="risk-val">20%</span>
        <span class="risk-label">최대 총 노출</span>
      </div>
      <div class="risk-item">
        <span class="risk-val">-7%</span>
        <span class="risk-label">주간 손실 한도</span>
      </div>
      <div class="risk-item">
        <span class="risk-val">80</span>
        <span class="risk-label">VKOSPI 경계선 (50% 축소)</span>
      </div>
    </div>
  </div>
</div>

<!-- ══════════════════════════════════════════════
     7. 데이터 한계
══════════════════════════════════════════════ -->
<div class="section">
  <div class="section-title"><span class="badge-num">8</span> 데이터 한계 및 주의사항</div>
  <ul class="limit-list">
    <li>
      <strong>05-19 분봉 누락:</strong> v4_ohlcv_minute_2026_05 테이블에 05-19 분봉이 12:36 KST 이후만 존재합니다.
      장초반(09:00~12:36) 신호가 완전히 누락되어 당일 사전진입 신호 검증 및 거래량 폭발 포착이 불가합니다.
    </li>
    <li>
      <strong>05-15 기업 이벤트 혼재:</strong> 합병·분할·재상장 종목이 다수 포함되어 +100~1,100% 비정상 등락이 관측됩니다.
      정상 상한가 종목과 반드시 분리 분석이 필요합니다. (필터 조건: 전일 거래량 = 0 OR 등락률 &gt; 80%)
    </li>
    <li>
      <strong>테마 매핑 부족:</strong> v4_theme_stock_mapping 기준 9종목만 테마 확인 가능하며,
      대부분이 업종 분류에 머물러 있습니다. 실제 투자 테마 매핑 데이터 확충이 필요합니다.
    </li>
    <li>
      <strong>거래대금 컬럼 NULL:</strong> ohlcv_daily 테이블의 trade_amount 컬럼이 NULL입니다.
      스냅샷 기반 거래대금만 가용하여 실시간 거래대금 필터(100억원 기준) 적용에 한계가 있습니다.
    </li>
  </ul>
</div>

</div><!-- .container -->

<!-- ══════════════════════════════════════════════
     푸터
══════════════════════════════════════════════ -->
<div class="report-footer">
  <div style="max-width:1200px;margin:0 auto;">
    <strong>투자 유의사항</strong><br>
    본 리포트는 GO100 시스템의 내부 전략 분석 자료로, 투자 권유 목적이 아닙니다.
    상한가따라잡기 전략은 고위험 단기 매매 전략으로 원금 손실 위험이 있습니다.
    모든 투자 결정은 본인 책임하에 이루어져야 하며, 과거 성과가 미래 수익을 보장하지 않습니다.<br><br>
    <strong>데이터 기준일:</strong> 2026-05-19 18:20 KST &nbsp;|&nbsp;
    <strong>데이터 소스:</strong> GO100 PostgreSQL (ohlcv_daily, v4_ohlcv_minute_2026_05, v4_market_ranking, v4_theme_stock_mapping) &nbsp;|&nbsp;
    <strong>버전:</strong> v4.0 &nbsp;|&nbsp;
    <strong>전략카드:</strong> #119 (완전 반영) &nbsp;|&nbsp;
    <strong>생성:</strong> AADS 자율 AI 개발 시스템
  </div>
</div>

</body>
</html>"""

# 출력 경로들
paths = [
    "/var/www/aads_exports/go100_upper_limit_chase_report_20260519.html",
    "/var/www/aads-public/reports/go100_upper_limit_chase_report_20260519.html",
]

for path in paths:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_CONTENT)
    print(f"[OK] Written: {path} ({len(HTML_CONTENT):,} bytes)")

print("Done.")
