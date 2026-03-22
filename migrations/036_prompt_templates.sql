-- P2-10: 프롬프트 템플릿 저장 및 원클릭 실행
CREATE TABLE IF NOT EXISTS prompt_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT DEFAULT '일반',
    usage_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prompt_templates_category ON prompt_templates(category);
CREATE INDEX IF NOT EXISTS idx_prompt_templates_usage ON prompt_templates(usage_count DESC);

-- 기본 템플릿
INSERT INTO prompt_templates (title, content, category) VALUES
('전체 시스템 헬스체크', '모든 서비스(AADS, KIS, GO100, SF, NTV2)의 헬스체크를 실행하고 결과를 보고해', '운영'),
('오늘 비용 보고', '오늘 사용한 전체 API 비용을 프로젝트별로 정리해서 보고해', '운영'),
('Pipeline Runner 상태', '현재 Pipeline Runner에 대기중이거나 실행중인 작업 전체 상태를 보고해', '운영'),
('KIS 매매 현황', '@KIS 오늘 매매 현황과 수익률을 보고해', '분석'),
('코드 리뷰', '최근 커밋의 코드 변경사항을 리뷰하고 문제점이 있으면 보고해', '개발'),
('주간 요약', '이번 주 전체 프로젝트 진행 상황을 요약해서 보고해', '분석')
ON CONFLICT DO NOTHING;
