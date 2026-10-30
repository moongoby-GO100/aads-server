"""Frozen UX source fidelity checks; these do not execute models or UI."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/design_agent/ux_contract_rev2.json'
SKILLS = (
    'design-intake-interview', 'design-discovery', 'design-art-direction',
    'design-interaction-prototype', 'design-external-import',
    'design-project-onboarding', 'design-token-extraction',
    'design-contrast-audit', 'design-critique', 'design-change-impact',
)


class UXContractTests(unittest.TestCase):
    def test_frozen_source(self):
        data = json.loads(FIXTURE.read_text())
        self.assertEqual(hashlib.sha256(json.dumps(
            data, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            '3e5c29178ec1627641c1093cdb30620b9d697d499b441151eed38e95d52d7928')

    def test_exact_mapping_and_all_fields_in_skill(self):
        cases = json.loads(FIXTURE.read_text())['cases']
        expected_ids = {f'UX{i:02}-{kind}' for i in range(1, 11) for kind in 'NMCF'}
        self.assertEqual(len(cases), 40)
        self.assertEqual({c['id'] for c in cases}, expected_ids)
        for case in cases:
            with self.subTest(id=case['id']):
                name = SKILLS[int(case['id'][2:4]) - 1]
                self.assertEqual(case['skill'], name)
                self.assertEqual(case['skill_file'], f'.claude/skills/{name}/SKILL.md')
                self.assertEqual(case['fixture'], f'ux_contract_rev2.json#{case["id"]}')
                text = (ROOT / case['skill_file']).read_text()
                canonical = text.split('## 검증 fixture\n', 1)[1].split('## 추가 회귀 사례', 1)[0]
                block = canonical.split(f'ID: {case["id"]}\n', 1)[1].split('### ', 1)[0]
                for field, label in [('input', '입력'), ('expected', '기대'), ('observed', '관측')]:
                    self.assertTrue(case[field].strip())
                    self.assertIn(f'{label}: {case[field]}\n', block)
                self.assertEqual(case['execution_status'], '실행미검증')
                self.assertEqual(case['enforcement'], '지침만 있음')
                for metadata in ('status: candidate', 'risk_tier: read', 'version: 0.3.0'):
                    self.assertIn(metadata, text)

    def test_question_domains_and_choices(self):
        text = (ROOT / '.claude/skills/design-intake-interview/SKILL.md').read_text()
        domains = ('목적', '사용자', '사용 상황·빈도·기기', '핵심 행동', '정보 우선순위',
                   '진입·이동', '실패·예외', '시각 방향', '성공 기준', '제약')
        for index, domain in enumerate(domains, 1):
            block = text.split(f'- Q{index} {domain}:', 1)[1].split('\n- Q', 1)[0]
            self.assertGreaterEqual(block.count('고르면 달라지는 것:'), 3)
            self.assertIn('권장:', block)
        self.assertIn('기기·접근성 제약은 Q10', text)
        self.assertNotIn('기기·접근성 제약은 Q9', text)

    def test_critical_scenarios_remain_distinct(self):
        cases = {c['id']: c for c in json.loads(FIXTURE.read_text())['cases']}
        for ident, terms in {
            'UX01-F': ('저장', '미확정', '시안은 차단'),
            'UX04-C': ('읽기 권한', '활성 승인 버튼', '서버는 거절'),
            'UX08-N': ('44×44', '43×44', '44×43', '원시 계산값', '반올림'),
            'UX09-C': ('핵심 승인 행동이 불가능', '총점과 무관', '증거 확보 전'),
        }.items():
            payload = ' '.join(cases[ident][k] for k in ('input', 'expected', 'observed'))
            for term in terms:
                self.assertIn(term, payload)

    def test_s3_and_body_contracts(self):
        text = (ROOT / '.claude/skills/design-change-impact/SKILL.md').read_text().split('## 검증 fixture')[0]
        for stage in ('1 접수·대상 특정', '2 현재 상태 수집', '3 유형·영향 분류', '4 범위 질문',
                      '5 전후 비교 시안', '6 회귀 확인', '7 승인·인계', '8 배포 후 대조'):
            self.assertIn(stage, text)
        self.assertIn('배포가 없으면 이 단계는 미실행', text)
        for name in SKILLS:
            body = (ROOT / f'.claude/skills/{name}/SKILL.md').read_text().split('## 검증 fixture')[0]
            self.assertIn('실행 단계:', body)
            self.assertIn('반려·복구 조건:', body)
            self.assertIn('지침만 있음', body)
            self.assertIn('HTTP→API→프로세스', body)


if __name__ == '__main__':
    unittest.main()
