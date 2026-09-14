-- 목표의 설계 문서 연결.
--
-- 2026-09-14 대표님 지시: "골의 설계와 PRD등을 작성 해당 현황에 반영되어야한다".
--
-- 지금 목표는 제목과 마일스톤뿐이다. **왜 이 목표인지, 어떻게 달성할
-- 것인지가 어디에도 없다.** #310 의 기획서·PRD 는 저장소에 파일로 있지만
-- 목표와 이어져 있지 않아서, 담당이 지시를 받아도 배경을 찾을 수 없다.
--
-- `doc_path` 는 `doc_chunks.doc_path` 와 같은 규격이다. 그래서 연결만
-- 해 두면 문서 검색(Auto-RAG)에서도 같은 문서가 잡힌다 — 사본을 만들지
-- 않는다.
CREATE TABLE IF NOT EXISTS goal_documents (
    id         bigserial PRIMARY KEY,
    goal_id    uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    kind       text NOT NULL,          -- plan | prd | report | reference
    doc_path   text NOT NULL,
    title      text,
    note       text,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    UNIQUE (goal_id, doc_path)
);

CREATE INDEX IF NOT EXISTS idx_goal_documents_goal ON goal_documents (goal_id, kind);

COMMENT ON TABLE goal_documents IS
  '목표별 설계 문서. doc_path 는 doc_chunks 와 같은 규격이라 검색에서도 잡힌다.';
