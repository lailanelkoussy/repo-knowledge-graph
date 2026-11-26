import logging
import os
import re
from typing import Dict, Iterable, Optional

from ..languages import get_language_name

logger = logging.getLogger(__name__)

# Files and directories that never carry useful source code
DEFAULT_SKIP_PATTERN = (
    r"(?:\.log$|\.json$|\.lock$|\.min\.js$|\.map$"
    r"|(?:^|/)(?:\.git|\.hg|\.svn|\.idea|\.vscode|__pycache__|\.cache|\.mypy_cache|\.pytest_cache"
    r"|node_modules|\.venv|\.tox)(?:/|$)"
    r"|(?:^|/)(?:changelog|ChangeLog)(?:\.[a-z0-9]+)?$|\.cache$)"
)
DEFAULT_SKIP_DIRS = ['.git']
BINARY_SNIFF_BYTES = 8192


def _load_gitignore(directory_path: str):
    """Return a matcher for the root .gitignore, or None (requires the optional `pathspec` package)."""
    gitignore = os.path.join(directory_path, '.gitignore')
    if not os.path.isfile(gitignore):
        return None
    try:
        import pathspec
    except ImportError:
        logger.debug('pathspec not installed: .gitignore is not applied')
        return None
    with open(gitignore, encoding='utf-8', errors='ignore') as f:
        return pathspec.PathSpec.from_lines('gitwildmatch', f)


def _is_binary(raw: bytes) -> bool:
    return b'\x00' in raw[:BINARY_SNIFF_BYTES]


def read_directory_files_recursively(directory_path: str, skip_dirs: Optional[Iterable[str]] = None,
                                     skip_pattern: Optional[str] = DEFAULT_SKIP_PATTERN,
                                     max_file_size: int = 1_000_000,
                                     extensions: Optional[Iterable[str]] = None,
                                     respect_gitignore: bool = True) -> Dict[str, str]:
    """
    Recursively read the text files of a directory.

    Args:
        directory_path: Root directory.
        skip_dirs: Directory names to skip anywhere in the tree.
        skip_pattern: Regex matched against POSIX relative paths (files and directories) to skip.
        max_file_size: Files larger than this many bytes are skipped (0 = no limit).
        extensions: If given, only files with one of these extensions (without dot) are kept.
        respect_gitignore: Apply the root .gitignore when the `pathspec` package is available.

    Returns:
        Mapping of POSIX relative path -> file content (UTF-8). Binary and undecodable files are skipped.
    """
    skip_dirs = set(skip_dirs or []) | set(DEFAULT_SKIP_DIRS)
    compiled_pattern = re.compile(skip_pattern) if skip_pattern else None
    allowed_ext = {e.lower().lstrip('.') for e in extensions} if extensions else None
    gitignore = _load_gitignore(directory_path) if respect_gitignore else None

    file_contents: Dict[str, str] = {}
    for root, dirs, files in os.walk(directory_path):
        rel_root = os.path.relpath(root, directory_path).replace(os.sep, '/')
        rel_root = '' if rel_root == '.' else rel_root

        kept_dirs = []
        for d in sorted(dirs):
            rel_dir = f'{rel_root}/{d}' if rel_root else d
            if d in skip_dirs:
                continue
            if compiled_pattern and compiled_pattern.search(rel_dir):
                continue
            if gitignore and gitignore.match_file(rel_dir + '/'):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for file in sorted(files):
            relative_path = f'{rel_root}/{file}' if rel_root else file
            if compiled_pattern and compiled_pattern.search(relative_path):
                continue
            if gitignore and gitignore.match_file(relative_path):
                continue
            if allowed_ext is not None and file.rsplit('.', 1)[-1].lower() not in allowed_ext:
                continue

            full_path = os.path.join(root, file)
            try:
                if max_file_size and os.path.getsize(full_path) > max_file_size:
                    logger.info(f'Skipping {relative_path}: larger than {max_file_size} bytes')
                    continue
                with open(full_path, 'rb') as f:
                    raw = f.read()
            except OSError as e:
                logger.warning(f'Failed to read {relative_path}: {e}')
                continue
            if _is_binary(raw):
                logger.debug(f'Skipping binary file {relative_path}')
                continue
            try:
                file_contents[relative_path] = raw.decode('utf-8')
            except UnicodeDecodeError:
                logger.debug(f'Skipping non UTF-8 file {relative_path}')
                continue

    return file_contents


def get_language_from_filename(file_name: str) -> str:
    """Display language of a file (kept for backward compatibility, see `languages.get_language_name`)."""
    return get_language_name(file_name)
