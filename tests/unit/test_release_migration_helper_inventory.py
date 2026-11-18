import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = r'\$\(dirname\s+["\']?\$\{BASH_SOURCE\[0\]\}["\']?\)'
FILENAME = r'([\w.-]+\.(?:py|sh|txt))'


def referenced_scripts(source):
    """Find release-tree paths and paths relative to a shell script's directory."""
    paths = set(re.findall(r'\$(?:\{ROOT\}|ROOT)/' + r'(scripts/[\w./-]+\.(?:py|sh|txt))', source))
    paths.update(re.findall(r"\broot\s*/\s*['\"](scripts/[\w./-]+\.(?:py|sh|txt))['\"]", source))
    paths.update('scripts/' + name for name in re.findall(SCRIPT_DIR + '/' + FILENAME, source))
    variables = re.findall(r'\b(\w+)\s*=\s*["\']?' + SCRIPT_DIR, source)
    for variable in variables:
        var = r'\$(?:\{' + re.escape(variable) + r'\}|' + re.escape(variable) + r'\b)'
        paths.update('scripts/' + name for name in re.findall(var + '/' + FILENAME, source))
    return paths


def test_script_directory_reference_forms():
    source = '''
    python3 "$(dirname "${BASH_SOURCE[0]}")/direct.py"
    d=$(dirname "${BASH_SOURCE[0]}")
    python3 "$d/indirect.py" "${d}/other.sh"
    '''
    assert referenced_scripts(source) == {
        'scripts/direct.py', 'scripts/indirect.py', 'scripts/other.sh'
    }


def test_release_migration_helper_inventory():
    deploy = (ROOT / 'deploy.sh').read_text()
    inventory = re.search(r'RELEASE_MIGRATION_HELPERS=\(\s*(.*?)\n\)', deploy, re.S)
    assert inventory is not None
    helpers = set(re.findall(r'scripts/[\w./-]+', inventory.group(1)))
    assert 'scripts/release_migration_destructive.py' in referenced_scripts(
        (ROOT / 'scripts/apply_release_migrations.sh').read_text()
    )
    pending = ['scripts/apply_release_migrations.sh']
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        assert path in helpers, f'{path} is missing from RELEASE_MIGRATION_HELPERS'
        source = (ROOT / path).read_text()
        for reference in referenced_scripts(source):
            assert reference in helpers, f'{path} references unbundled {reference}'
            if (ROOT / reference).suffix in {'.sh', '.py'}:
                pending.append(reference)
