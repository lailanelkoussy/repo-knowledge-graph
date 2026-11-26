"""
Single source of truth for language support.

Every component that needs to know "what language is this file" (code splitter,
entity extractor, entity/chunk mapper, display name) reads this table, so adding a
language only requires one new entry here.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LanguageSpec:
    name: str                  # display name, stored on nodes (e.g. 'python', 'c++')
    splitter: Optional[str]    # langchain_text_splitters.Language member name, None = generic splitter
    extractor: Optional[str]   # key of the AST entity extractor, None = no entity extraction
    mapper: str                # EntityChunkMapper mode: 'python' | 'c' | 'cpp' | 'java' | 'generic'


_PY = LanguageSpec('python', 'PYTHON', 'python', 'python')
_C = LanguageSpec('c', 'C', 'c', 'c')
_C_HEADER = LanguageSpec('c', 'C', 'cpp', 'c')  # headers may be C or C++: use the C++ extractor
_CPP = LanguageSpec('c++', 'CPP', 'cpp', 'cpp')
_JAVA = LanguageSpec('java', 'JAVA', 'java', 'java')
_JS = LanguageSpec('javascript', 'JS', 'javascript', 'generic')
_TS = LanguageSpec('typescript', 'TS', 'typescript', 'generic')
_RUST = LanguageSpec('rust', 'RUST', 'rust', 'generic')
_GO = LanguageSpec('go', 'GO', None, 'generic')
_HTML = LanguageSpec('html', 'HTML', 'html', 'generic')
_MD = LanguageSpec('markdown', 'MARKDOWN', None, 'generic')

LANGUAGES = {
    'py': _PY, 'pyw': _PY,
    'c': _C, 'h': _C_HEADER,
    'cpp': _CPP, 'cc': _CPP, 'cxx': _CPP, 'hpp': _CPP, 'hh': _CPP, 'hxx': _CPP,
    'java': _JAVA,
    'js': _JS, 'mjs': _JS, 'cjs': _JS, 'jsx': _JS,
    'ts': _TS, 'tsx': _TS, 'mts': _TS, 'cts': _TS,
    'rs': _RUST,
    'go': _GO,
    'html': _HTML, 'htm': _HTML,
    'md': _MD, 'markdown': _MD,
}


def get_extension(file_name: str) -> str:
    """Lower-cased extension without the dot ('' when the file has none)."""
    base = file_name.replace('\\', '/').rsplit('/', 1)[-1]
    if '.' not in base.lstrip('.'):
        return ''
    return base.rsplit('.', 1)[-1].lower()


def get_language_spec(file_name: str) -> Optional[LanguageSpec]:
    return LANGUAGES.get(get_extension(file_name))


def get_language_name(file_name: str) -> str:
    """Display language of a file; falls back to the raw extension for unknown files."""
    spec = get_language_spec(file_name)
    return spec.name if spec else get_extension(file_name)


def has_entity_extractor(file_name: str) -> bool:
    spec = get_language_spec(file_name)
    return bool(spec and spec.extractor)
