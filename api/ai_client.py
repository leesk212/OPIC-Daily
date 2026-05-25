"""
Wrapper for calling the locally installed Claude Code CLI.

Requires:
- `claude` CLI installed and in PATH (https://docs.claude.com/en/docs/claude-code/quickstart)
- User authenticated (one-time `claude login` or env-configured API key)
"""
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Map our short names → Claude Code model aliases.
MODEL_MAP = {
    'haiku': 'haiku',
    'sonnet': 'sonnet',
    'opus': 'opus',
}


class ClaudeCodeError(Exception):
    """Raised when calling Claude Code CLI fails."""


def _build_cmd(model_arg: str) -> list[str]:
    return ['claude', '-p', '--model', model_arg]


def call_claude(prompt: str, model: str = 'haiku', timeout: int = 180) -> str:
    """
    Call `claude -p --model <model>` via subprocess, sending prompt on stdin.
    Returns raw stdout. Raises ClaudeCodeError on failure.
    """
    model_arg = MODEL_MAP.get(model, MODEL_MAP['haiku'])
    cmd = _build_cmd(model_arg)

    logger.info(f'Invoking Claude: model={model_arg}, prompt_len={len(prompt)}, cmd={cmd}')

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ClaudeCodeError(
            "'claude' 명령어를 찾을 수 없어요. "
            "Claude Code가 설치되어 있고 PATH에 있는지 확인하세요. "
            "설치: https://docs.claude.com/en/docs/claude-code/quickstart"
        )
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(f'Claude 호출이 {timeout}초 안에 끝나지 않았어요.')
    except Exception as e:
        raise ClaudeCodeError(f'Claude 호출 중 예외: {type(e).__name__}: {e}')

    stdout = (result.stdout or '').strip()
    stderr = (result.stderr or '').strip()

    logger.info(
        f'Claude exit={result.returncode}, stdout_len={len(stdout)}, stderr_len={len(stderr)}'
    )

    if result.returncode != 0:
        # Include BOTH stdout and stderr — Claude sometimes writes errors to stdout
        parts = [f'Claude CLI exit {result.returncode}']
        if stderr:
            parts.append(f'stderr: {stderr[:1500]}')
        if stdout:
            parts.append(f'stdout: {stdout[:1500]}')
        if not stderr and not stdout:
            parts.append('(stderr/stdout 둘 다 비어있음 — 인증 문제일 가능성. `claude` 명령어를 터미널에서 직접 실행해보세요.)')
        raise ClaudeCodeError(' | '.join(parts))

    if not stdout:
        raise ClaudeCodeError(
            f'Claude가 빈 출력을 반환했어요. stderr: {stderr[:500] if stderr else "(empty)"}'
        )

    return stdout


def diagnose() -> dict:
    """진단용. Claude CLI가 동작 가능한지 단계별 체크."""
    info: dict = {}

    info['which_claude'] = shutil.which('claude')
    if not info['which_claude']:
        info['error'] = 'claude 명령어가 PATH에 없음'
        return info

    # 1. Version check
    try:
        v = subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=10)
        info['version'] = {
            'returncode': v.returncode,
            'stdout': (v.stdout or '').strip()[:500],
            'stderr': (v.stderr or '').strip()[:500],
        }
    except Exception as e:
        info['version_error'] = f'{type(e).__name__}: {e}'
        return info

    # 2. Help check (verifies CLI is callable)
    try:
        h = subprocess.run(['claude', '--help'], capture_output=True, text=True, timeout=10)
        info['help_returncode'] = h.returncode
    except Exception as e:
        info['help_error'] = str(e)

    # 3. Tiny generation test
    try:
        t = subprocess.run(
            ['claude', '-p', '--model', 'haiku'],
            input='Reply with exactly one word: hello',
            capture_output=True,
            text=True,
            timeout=60,
        )
        info['tiny_test'] = {
            'returncode': t.returncode,
            'stdout': (t.stdout or '').strip()[:500],
            'stderr': (t.stderr or '').strip()[:500],
        }
    except Exception as e:
        info['tiny_test_error'] = f'{type(e).__name__}: {e}'

    return info
