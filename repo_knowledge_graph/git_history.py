"""
Git history support: commit nodes linked to the files they modified, and file diffs
between two revisions of a local repository.
"""
import base64
import logging
import os
import re
import subprocess
import urllib.parse
from typing import Iterable, List, Optional

from .node import CommitNode

logger = logging.getLogger(__name__)

_FIELD_SEP = '\x1f'
_RECORD_SEP = '\x1e'
# Revisions accepted from callers (MCP tools): hashes, branches, tags, HEAD~2, v1.0^ ...
_SAFE_REVISION = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_./~^@{}-]*$')


class GitError(RuntimeError):
    pass


def is_git_repository(path: Optional[str]) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        result = subprocess.run(['git', '-C', path, 'rev-parse', '--is-inside-work-tree'],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == 'true'


def _git(repo_path: str, *args: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(['git', '-C', repo_path, *args], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f'git {args[0]} failed: {e}') from None
    if result.returncode != 0:
        raise GitError(f'git {args[0]} failed: {result.stderr.strip()[:500]}')
    return result.stdout


def validate_revision(revision: str) -> str:
    if not revision or not _SAFE_REVISION.match(revision) or '..' in revision:
        raise ValueError(f"Invalid git revision: '{revision}'")
    return revision


def collect_commits(repo_path: str, max_commits: int = 200,
                    known_files: Optional[Iterable[str]] = None) -> List[CommitNode]:
    """
    Read the last `max_commits` commits of the current branch.
    `files_changed` only keeps paths present in `known_files` when it is given.
    """
    known = set(known_files) if known_files is not None else None
    fmt = _RECORD_SEP + _FIELD_SEP.join(['%H', '%h', '%an', '%aI', '%s'])
    output = _git(repo_path, 'log', f'--max-count={int(max_commits)}', '--no-merges', '--name-only',
                  f'--format={fmt}', timeout=300)

    commits = []
    for record in output.split(_RECORD_SEP):
        record = record.strip('\n')
        if not record:
            continue
        header, _, files_block = record.partition('\n')
        parts = header.split(_FIELD_SEP)
        if len(parts) != 5:
            continue
        full_hash, short_hash, author, date, subject = parts
        files = [f.strip() for f in files_block.splitlines() if f.strip()]
        if known is not None:
            files = [f for f in files if f in known]
        commits.append(CommitNode(
            id=f'commit:{full_hash}',
            name=short_hash,
            commit_hash=full_hash,
            author=author,
            date=date,
            message=subject,
            files_changed=files,
        ))
    return commits


def current_revision(repo_path: str) -> Optional[str]:
    try:
        return _git(repo_path, 'rev-parse', 'HEAD').strip()
    except GitError:
        return None


def diff_file(repo_path: str, file_path: str, rev_a: str, rev_b: str = 'HEAD', max_chars: int = 50_000) -> str:
    """Unified diff of one file between two revisions."""
    validate_revision(rev_a)
    validate_revision(rev_b)
    diff = _git(repo_path, 'diff', '--no-color', rev_a, rev_b, '--', file_path)
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f'\n... (diff truncated, {len(diff) - max_chars} more characters)'
    return diff


def file_at_revision(repo_path: str, file_path: str, revision: str) -> str:
    validate_revision(revision)
    return _git(repo_path, 'show', f'{revision}:{file_path}')


# ------------------------------------------------------------------ cloning

def _redact_url(url: str) -> str:
    """Remove any user/password/token embedded in a URL so it can be logged safely."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return '<unparseable url>'
    if parts.username or parts.password:
        host = parts.hostname or ''
        if parts.port:
            host = f'{host}:{parts.port}'
        parts = parts._replace(netloc=f'***@{host}')
    return urllib.parse.urlunsplit(parts)


def _git_auth_env(token: Optional[str]) -> dict:
    """
    Build the environment for a git subprocess. The token is passed through
    GIT_CONFIG_* variables (git >= 2.31) as an HTTP Authorization header, so it never
    appears in the command line, the remote URL, the cloned repo's .git/config or the logs.
    """
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'  # never block waiting for credentials
    if token:
        basic = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        env['GIT_CONFIG_COUNT'] = '1'
        env['GIT_CONFIG_KEY_0'] = 'http.extraHeader'
        env['GIT_CONFIG_VALUE_0'] = f'Authorization: Basic {basic}'
    return env


def _git_clone(clone_url: str, dest: str, token: Optional[str] = None) -> None:
    subprocess.run(['git', 'clone', clone_url, dest], check=True, env=_git_auth_env(token))
