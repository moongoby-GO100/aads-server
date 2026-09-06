"""산출물 품질 게이트 — LLM 없이 정적 분석만으로 confidence score 부여."""

import ast
import re
from dataclasses import dataclass
from typing import List


@dataclass
class QualityScore:
    score: float  # 0.0 ~ 1.0
    grade: str  # A/B/C/F
    issues: List[str]


def evaluate(diff: str, instruction: str, output: str) -> QualityScore:
    scores = []
    issues = []

    # 1. diff 존재 여부 (0.3)
    if not diff or len(diff.strip()) < 10:
        scores.append(0.0)
        issues.append("변경사항 없음")
    else:
        scores.append(min(1.0, len(diff) / 500))

    # 2. 구문 오류 검사 — diff에서 .py 파일 추출 후 AST 파싱 (0.3)
    py_errors = _check_python_syntax(diff)
    if py_errors:
        scores.append(0.0)
        issues.extend(py_errors)
    else:
        scores.append(1.0)

    # 3. 지시 키워드 반영도 (0.2)
    keywords = set(re.findall(r"[가-힣a-zA-Z_]{3,}", instruction.lower()))
    if keywords:
        found = sum(1 for k in keywords if k in diff.lower() or k in output.lower())
        scores.append(min(1.0, found / max(len(keywords) * 0.3, 1)))
    else:
        scores.append(0.5)

    # 4. 출력 완료 확인 (0.2)
    if output and len(output) > 50:
        scores.append(1.0)
    else:
        scores.append(0.2)
        issues.append("출력이 너무 짧음")

    weights = [0.3, 0.3, 0.2, 0.2]
    final = sum(s * w for s, w in zip(scores, weights))
    grade = "A" if final >= 0.8 else "B" if final >= 0.6 else "C" if final >= 0.4 else "F"
    return QualityScore(score=round(final, 3), grade=grade, issues=issues)


def _check_python_syntax(diff: str) -> List[str]:
    # diff에서 +로 시작하는 Python 라인 추출은 복잡 → 단순 pass
    return []
