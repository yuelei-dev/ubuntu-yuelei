# -*- coding: utf-8 -*-
"""Fail-closed HQ CLI bridge for the customer-guide Director Agent.

Only local capability discovery is exposed.  The bridge never passes customer
data, credentials, provider keys, or confirmation flags to the CLI, and it
does not permit ``run``, login, upload, or any account-bound action.
"""

import json
import os
from pathlib import Path
import subprocess
import sys


MAX_CLI_OUTPUT_BYTES = 256 * 1024
CLI_TIMEOUT_SECONDS = max(
    1, min(10, int(os.environ.get("DIRECTOR_AGENT_CLI_TIMEOUT_SECONDS", "5") or 5))
)
_LOCAL_CLI_ROOT = Path(__file__).resolve().parents[2] / "tools" / "hq-cli"
_RUNTIME_CLI_ROOT = Path("/opt/huangque-repository/tools/hq-cli")
CLI_ROOT = Path(os.environ.get(
    "DIRECTOR_AGENT_CLI_ROOT",
    str(_LOCAL_CLI_ROOT if _LOCAL_CLI_ROOT.is_dir() else _RUNTIME_CLI_ROOT),
)).expanduser()

PAGE_CAPABILITY = {
    "script": "script",
    "digital_human_oneclick": "digital-presenter-capability",
    "private_domain_video": "assets-page",
}
_REQUIRED_MODULE_FILES = (
    "__init__.py", "__main__.py", "catalog.py", "cli.py", "client.py",
)
_RUN_MODULE = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('hq_cli',run_name='__main__')"
)


class DirectorCLIError(ValueError):
    """Public-safe failure raised when the local CLI contract is unusable."""


def _cli_paths(root=None):
    candidate = Path(root or CLI_ROOT)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise DirectorCLIError("编导 CLI 路径无效")
    try:
        resolved_root = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DirectorCLIError("编导 CLI 尚未安装")
    source = resolved_root / "src"
    package = source / "hq_cli"
    for name in _REQUIRED_MODULE_FILES:
        item = package / name
        try:
            resolved = item.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            raise DirectorCLIError("编导 CLI 文件不完整")
        if item.is_symlink() or not resolved.is_file():
            raise DirectorCLIError("编导 CLI 文件不安全")
    return resolved_root, source


def is_available(root=None):
    try:
        _cli_paths(root)
        return True
    except DirectorCLIError:
        return False


def _subprocess_env():
    """Build a minimal environment that deliberately excludes all secrets."""
    result = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    # CPython on Windows needs these process settings; none are credentials.
    for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if os.environ.get(name):
            result[name] = os.environ[name]
    return result


def _run_json(arguments, root=None, runner=subprocess.run):
    if not arguments or arguments[0] not in {"capabilities", "describe"}:
        raise DirectorCLIError("编导 CLI 命令不在允许范围")
    if arguments[0] == "capabilities" and len(arguments) != 1:
        raise DirectorCLIError("编导 CLI 参数无效")
    if arguments[0] == "describe" and (
            len(arguments) != 2 or arguments[1] not in PAGE_CAPABILITY.values()):
        raise DirectorCLIError("编导 CLI 能力不在允许范围")
    cli_root, source = _cli_paths(root)
    command = [
        sys.executable, "-I", "-X", "utf8", "-c", _RUN_MODULE, str(source),
        *arguments, "--json",
    ]
    try:
        completed = runner(
            command, cwd=str(cli_root), env=_subprocess_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", timeout=CLI_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DirectorCLIError("编导 CLI 暂时不可用")
    stdout = str(completed.stdout or "")
    stderr = str(completed.stderr or "")
    if (len(stdout.encode("utf-8")) > MAX_CLI_OUTPUT_BYTES
            or len(stderr.encode("utf-8")) > MAX_CLI_OUTPUT_BYTES):
        raise DirectorCLIError("编导 CLI 返回内容过大")
    if int(completed.returncode) != 0:
        raise DirectorCLIError("编导 CLI 执行失败")
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        raise DirectorCLIError("编导 CLI 返回格式无效")
    if not isinstance(payload, dict):
        raise DirectorCLIError("编导 CLI 返回格式无效")
    return payload


def page_guide(page, root=None, runner=subprocess.run):
    """Discover and describe one page capability through the real HQ CLI."""
    capability_id = PAGE_CAPABILITY.get(str(page or ""))
    if not capability_id:
        raise DirectorCLIError("当前页面没有编导 CLI 能力")
    catalog = _run_json(["capabilities"], root=root, runner=runner)
    if catalog.get("schema") != "hq.capabilities/v1":
        raise DirectorCLIError("编导 CLI 能力目录版本无效")
    matches = [
        item for item in (catalog.get("capabilities") or [])
        if isinstance(item, dict) and item.get("id") == capability_id
    ]
    if len(matches) != 1:
        raise DirectorCLIError("编导 CLI 缺少页面能力")
    described = _run_json(
        ["describe", capability_id], root=root, runner=runner,
    )
    capability = described.get("capability")
    if (described.get("schema") != "hq.describe/v1"
            or not isinstance(capability, dict)
            or capability.get("id") != capability_id):
        raise DirectorCLIError("编导 CLI 能力说明无效")
    allowed_fields = {
        "id", "name", "kind", "description", "input_schema",
        "requires_auth", "required_scope", "target_auth", "side_effect",
        "confirmation_required", "cost", "deep_link", "next_actions",
    }
    safe_capability = {
        key: value for key, value in capability.items() if key in allowed_fields
    }
    return {
        "schema": "hq.director-page-guide/v1",
        "cli_version": str(described.get("cli_version") or ""),
        "page": page,
        "capability": safe_capability,
        "execution_policy": {
            "discovery_only": True,
            "customer_confirmation_required_for_generation": True,
        },
    }
