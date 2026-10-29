"""Required skill routing and immutable file evidence; no model or DB writes."""
import hashlib
import re
from pathlib import Path

import yaml

SKILLS_ROOT = Path(__file__).resolve().parents[3] / '.claude' / 'skills'
EVENT_SITUATIONS = {'project_created': 'S1', 'new_screen_requested': 'S2',
                    'screen_change_requested': 'S3', 'deploy_succeeded': 'S9'}


def load_skill(name):
    """Read once so the recorded hash describes the parsed file bytes."""
    try:
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name):
            raise ValueError('Invalid skill name')
        data = (SKILLS_ROOT / name / 'SKILL.md').read_bytes()
        text = data.decode('utf-8')
        frontmatter = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.DOTALL)
        if not frontmatter:
            raise ValueError('Missing frontmatter')
        metadata = yaml.safe_load(frontmatter.group(1))
        if not isinstance(metadata, dict) or metadata.get('name') != name:
            raise ValueError('Frontmatter name mismatch')
        if metadata.get('status') not in {'candidate', 'active'}:
            raise ValueError('Invalid skill status')
        return {'name': name, 'status': metadata['status'],
                'sha256': hashlib.sha256(data).hexdigest()}
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f'Skill {name}: {exc}') from exc


def resolve_situation(event_type, payload):
    situation = payload.get('situation') if event_type == 'manual' else EVENT_SITUATIONS.get(event_type)
    if situation not in {f'S{i}' for i in range(1, 10)}:
        raise ValueError('Missing or invalid situation for ' + event_type)
    return situation


async def required_skills(conn, situation):
    rows = await conn.fetch("""SELECT skill_name FROM design_agent_skill_routes
        WHERE situation=$1 AND required=true ORDER BY skill_name""", situation)
    if not rows:
        raise ValueError(f'No required skill routes for {situation}')
    return [row['skill_name'] for row in rows]
