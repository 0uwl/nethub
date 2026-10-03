"""Checks that the docs and the code still point at each other.

docs/index.md §2 sets the format: every rule is an H3 headed by its slug,
every rule names the tests that pin it, and code comments cite rules as
`docs/<file>.md [slug]`. These tests fail when a citation names a test,
file or rule that does not exist, so a rename or a deletion cannot leave
the docs quietly wrong.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted((ROOT / 'docs').glob('*.md'))
DOC_TEXT = {path.name: path.read_text() for path in DOCS}
CLAUDE = (ROOT / 'CLAUDE.md').read_text()
CODE = [path for pattern in ('nethub/**/*.py', 'nethub/**/*.html', 'nethub/**/*.css',
                             'tests/*.py', 'scripts/*', 'quadlet/*')
        for path in ROOT.glob(pattern) if path.is_file() and path.name != 'test_docs.py']

#: `### 2.3 [eligible-by-cursor]`
RULE_HEADING = re.compile(r'^### [0-9a-z.]+ \[([a-z0-9-]+)\]$', re.MULTILINE)
#: A rule slug: lowercase words joined by hyphens, in brackets, not a link.
SLUG_REF = re.compile(r'\[([a-z0-9]+(?:-[a-z0-9]+)+)\](?!\()')
#: `dispatch.md [slug]`, `(docs/dispatch.md) [slug], [slug2]`
QUALIFIED_REF = re.compile(
    r'([a-z-]+)\.md\)?((?:,?[ \t]*\n?[ \t]*(?://|#:?)?[ \t]*\[[a-z0-9-]+\](?!\())+)')
TEST_ID = re.compile(r'tests/(test_[a-z0-9_]+\.py)(?:::([A-Za-z0-9_:]+))?')


def rules_by_file():
    return {name: RULE_HEADING.findall(text) for name, text in DOC_TEXT.items()}


ALL_RULES = {slug for slugs in rules_by_file().values() for slug in slugs}


def test_every_rule_slug_is_unique():
    slugs = [slug for slugs in rules_by_file().values() for slug in slugs]
    duplicates = {slug for slug in slugs if slugs.count(slug) > 1}
    assert not duplicates


def test_every_rule_says_what_pins_it():
    missing = []
    for name, text in DOC_TEXT.items():
        for block in re.split(r'^### ', text, flags=re.MULTILINE)[1:]:
            heading = block.splitlines()[0]
            if RULE_HEADING.match('### ' + heading) and 'Pinned by:' not in block:
                missing.append(f'{name}: {heading}')
    assert not missing


def _test_names(filename):
    """`{'test_x', 'TestY::test_z', ...}` defined in tests/<filename>."""
    tree = ast.parse((ROOT / 'tests' / filename).read_text())
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(f'{node.name}::{item.name}')
    return names


@pytest.mark.parametrize('name', sorted(DOC_TEXT) + ['CLAUDE.md'])
def test_every_cited_test_exists(name):
    text = CLAUDE if name == 'CLAUDE.md' else DOC_TEXT[name]
    missing = []
    for filename, test in TEST_ID.findall(text):
        if not (ROOT / 'tests' / filename).is_file():
            missing.append(filename)
        elif test and test not in _test_names(filename):
            missing.append(f'{filename}::{test}')
    assert not missing


@pytest.mark.parametrize('name', sorted(DOC_TEXT) + ['CLAUDE.md'])
def test_every_cited_path_exists(name):
    text = CLAUDE if name == 'CLAUDE.md' else DOC_TEXT[name]
    cited = set(re.findall(r'(?:docs|scripts|quadlet)/[A-Za-z0-9_.-]+\.(?:md|sh|py|container)',
                           text))
    cited |= {f'docs/{doc}' for doc in re.findall(r'\]\(([a-z-]+\.md)\)', text)}
    assert not [path for path in cited if not (ROOT / path).is_file()]


def _qualified_refs(text):
    for doc, slugs in QUALIFIED_REF.findall(text):
        for slug in re.findall(r'\[([a-z0-9-]+)\]', slugs):
            yield f'{doc}.md', slug


def test_file_qualified_rule_citations_name_a_rule_in_that_file():
    rules = rules_by_file()
    sources = [CLAUDE, *DOC_TEXT.values(), *(path.read_text() for path in CODE)]
    broken = {f'{doc} [{slug}]' for text in sources for doc, slug in _qualified_refs(text)
              if doc in rules and slug not in rules[doc]}
    assert not broken


def test_every_slug_cited_anywhere_is_a_rule():
    # Code is checked by the file-qualified test above; a bare `[a-z]` in code
    # is as likely a regex as a rule.
    sources = {'CLAUDE.md': CLAUDE, **DOC_TEXT}
    unknown = {f'{where}: [{slug}]' for where, text in sources.items()
               for slug in SLUG_REF.findall(text) if slug not in ALL_RULES}
    assert not unknown


def test_no_workstream_tags_or_design_doc_sections_in_the_code():
    offenders = []
    for path in CODE:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r'\bWS-\d|PLAN\.md|design[- ]doc', line) or (
                    '§' in line and not re.search(r'docs/[a-z-]+\.md §', line)):
                offenders.append(f'{path.relative_to(ROOT)}:{number}')
    assert not offenders
