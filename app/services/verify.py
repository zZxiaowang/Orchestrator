"""步骤级**客观验收**：用可判定的检查替代「模型自己说完成了」。

设计约束（为什么只做这几类检查）：

* 只做**无副作用、不执行任意代码**的判定：文件/目录是否存在、内容是否包含某片段、
  Python 能否编译（``compile()`` 只编译不执行）、JSON 是否合法、通配是否匹配。
* ``py_import`` 是唯一**会执行代码**的检查（在工作区内真的导入一次模块），
  因此它复用「允许执行验证命令」这个开关：开关关着时降级为语法编译检查，
  并在结果里写明"没有真正导入"，不假装验过。
* 需要跑测试 / 构建脚本的验收走 ``commands``（白名单 + 超时 + 回灌报错），不在这一层。
* 检查全部走 ``Workspace.resolve``，因此和文件落地共用同一套越界防护。

验收不通过时，步骤**不得**标记为完成；编排器会把它标成 ``blocked`` 并把失败原因摊开，
用户可以补充说明后只重跑这一步。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.core.errors import WorkspaceError
from app.schemas.plan import CheckResult, StepCheck
from app.schemas.project import (
    DEFAULT_PROJECT_ID,
    ensure_same_project,
)
from app.services.gitguard import safe_relative
from app.services.workspace import Workspace

#: 单个文件最多读取多少字符再判定（避免一个巨大的产物把验收拖死）
MAX_INSPECT_CHARS = 2_000_000

#: 一步最多自动跑多少条检查，防止纲领里塞进几十条把执行拖慢
MAX_CHECKS_PER_STEP = 8

#: 给"本步真的写过的 .py 文件"预留的「能编译」名额。
#: 为什么要预留：纲领自己声明的检查排在前面，一旦把名额占满，语法错误这类
#: 最该拦住的问题反而永远排不上——而这一步恰好是"写代码类步骤"的最低门槛。
SYNTAX_CHECKS_RESERVE = 2

#: 一步最多补多少条语法检查（写了 20 个 .py 也不必验 20 次）
MAX_SYNTAX_CHECKS = 6

#: 导入检查的超时（秒）：导入会执行模块顶层代码，必须有上限
IMPORT_TIMEOUT_SECONDS = 30.0

#: 导入检查跑的脚本：只接受一个模块名，不接受任意代码
_IMPORT_SCRIPT = "import importlib, sys; importlib.import_module(sys.argv[1])"

#: 隐藏子进程窗口（Windows）：桌面版没有控制台，否则每跑一次检查都会弹黑窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def parse_checks(raw: Any) -> list[StepCheck]:
    """把模型/旧记录给的任意形状收敛成检查项列表（坏数据直接丢弃）。"""

    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    checks: list[StepCheck] = []
    for item in items:
        try:
            checks.append(StepCheck.model_validate(item))
        except Exception:  # noqa: BLE001 - 单条坏数据不该让整步失败
            continue
    return [check for check in checks if check.path]


def _looks_like_path(text: str) -> bool:
    """判断一个交付物描述是不是「文件路径」，而不是一句自然语言。"""

    value = (text or "").strip()
    if not value or len(value) > 120:
        return False
    if any(ch in value for ch in "，。；、 　"):
        return False
    if value.endswith(("/", "\\")):
        return False
    tail = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return "." in tail and not tail.startswith(".")


def derive_checks(deliverables: Iterable[str]) -> list[StepCheck]:
    """从交付物派生出「文件存在」检查。

    这是最有价值、也最不依赖模型自觉的一条：纲领说这一步要产出某个文件，
    执行完就必须真的存在。写错路径、只写空壳、或者干脆忘了写都会被挡住。
    """

    checks: list[StepCheck] = []
    seen: set[str] = set()
    for raw in deliverables or []:
        text = str(raw or "").strip()
        if not _looks_like_path(text):
            continue
        path = safe_relative(text)
        if path is None:
            continue
        if path.lower() in seen:
            continue
        seen.add(path.lower())
        checks.append(StepCheck(type="file_exists", path=path, label=f"交付物存在：{path}"))
    return checks


def derive_syntax_checks(
    paths: Iterable[str], *, limit: int = MAX_SYNTAX_CHECKS
) -> list[StepCheck]:
    """从"这一步真的写过/改过的文件"派生「能编译」检查。

    真实教训：``app/services/navigation_migration.py`` 少写了两个引号（三引号没收尾），
    8 个测试文件的收集全部因为 SyntaxError 挂掉；而当时的客观验收只检查
    "文件里含指定文字"，于是这一步照样被标成了完成。

    **能编译**是写代码类步骤的最低门槛，而且用 ``compile()`` 判定不需要执行任何代码、
    没有副作用，所以由系统默认补上，不等纲领自己想起来写。
    """

    checks: list[StepCheck] = []
    seen: set[str] = set()
    for raw in paths or []:
        path = safe_relative(str(raw or "").strip())
        if not path or not path.lower().endswith(".py"):
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        checks.append(StepCheck(type="py_compile", path=path, label=f"能编译：{path}"))
        if len(checks) >= max(1, limit):
            break
    return checks


def _check_key(check: StepCheck) -> tuple[str, str, str]:
    return (check.type, check.path.replace("\\", "/").lower(), check.text)


def effective_checks(
    declared: Sequence[StepCheck] | None,
    deliverables: Iterable[str],
    *,
    changed_paths: Iterable[str] = (),
    limit: int = MAX_CHECKS_PER_STEP,
) -> list[StepCheck]:
    """本步真正要跑的检查 = 纲领声明的 + 交付物派生的 + 本步改动派生的（去重、限量）。

    ``changed_paths`` 是本步实际写过的文件：其中的 ``.py`` 会补一条「能编译」检查，
    并**预留名额**，避免被纲领自己声明的检查挤掉。
    """

    merged: list[StepCheck] = []
    seen: set[tuple[str, str, str]] = set()
    for check in [*list(declared or []), *derive_checks(deliverables)]:
        key = _check_key(check)
        if key in seen:
            continue
        seen.add(key)
        merged.append(check)

    syntax = [
        check for check in derive_syntax_checks(changed_paths) if _check_key(check) not in seen
    ]
    room = max(1, limit - min(len(syntax), SYNTAX_CHECKS_RESERVE)) if syntax else limit
    return [*merged[:room], *syntax][: max(1, limit)]


def run_checks(
    workspace: Workspace,
    checks: Sequence[StepCheck],
    *,
    allow_import: bool = False,
) -> list[CheckResult]:
    """逐条执行检查；任何一条抛异常都被收敛成「不通过 + 原因」，不会中断整步。

    ``allow_import`` 决定 ``py_import``（会真的执行工作区里的代码）能不能跑：
    与「允许执行验证命令」共用同一个开关，默认关闭时它降级为语法编译检查。
    """

    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(_run_one(workspace, check, allow_import=allow_import))
        except Exception as exc:  # noqa: BLE001 - 检查本身出错也不能让执行段崩掉
            results.append(
                CheckResult(
                    type=check.type,
                    path=check.path,
                    text=check.text,
                    label=check.label,
                    ok=False,
                    detail=f"检查执行异常：{exc.__class__.__name__}: {exc}",
                )
            )
    return results


def summarize(results: Sequence[CheckResult]) -> dict[str, Any]:
    """给界面/报告用的汇总：没有检查项时是「未验证」，而不是「通过」。"""

    total = len(results)
    passed = sum(1 for item in results if item.ok)
    failed = total - passed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "unverified": total == 0,
        "passed_all": total > 0 and failed == 0,
    }


def failure_text(results: Sequence[CheckResult], *, limit: int = 4) -> str:
    """把失败项拼成一句可直接展示给用户的说明。"""

    failed = [item for item in results if not item.ok]
    parts = [f"{item.label or item.path}（{item.detail or '未通过'}）" for item in failed[:limit]]
    extra = len(failed) - len(parts)
    if extra > 0:
        parts.append(f"另有 {extra} 项未通过")
    return "；".join(parts)


def _run_one(workspace: Workspace, check: StepCheck, *, allow_import: bool = False) -> CheckResult:
    label = check.label or _default_label(check)
    base = CheckResult(type=check.type, path=check.path, text=check.text, label=label)

    if check.type == "glob":
        matched = _glob(workspace, check.path)
        base.ok = bool(matched)
        base.detail = (
            f"匹配到 {len(matched)} 个：{'、'.join(matched[:5])}" if matched else "没有任何文件匹配"
        )
        return base

    try:
        target = workspace.resolve(check.path)
    except WorkspaceError as exc:
        base.detail = f"路径不合法：{exc.message}"
        return base

    if check.type == "file_exists":
        base.ok = target.is_file()
        base.detail = "" if base.ok else "文件不存在"
        return base

    if check.type == "dir_exists":
        base.ok = target.is_dir()
        base.detail = "" if base.ok else "目录不存在"
        return base

    if check.type == "py_import":
        base.ok, base.detail = _run_import(workspace, target, check.path, allow_import=allow_import)
        return base

    if not target.is_file():
        base.detail = "文件不存在"
        return base

    if check.type == "file_contains":
        if not check.text:
            base.detail = "检查项缺少要查找的内容（text）"
            return base
        content = _read(target)
        base.ok, loose = _contains(content, check.text)
        if base.ok:
            # 严格匹配失败、宽松匹配通过时要说清楚，别让人以为写错了
            base.detail = "宽松匹配（忽略大小写与 _ - 空格）" if loose else ""
        else:
            base.detail = f"文件里没有找到：{' '.join(check.text.split())[:60]}"
        return base

    if check.type == "py_compile":
        base.ok, base.detail = _compile_source(target)
        return base

    if check.type == "json_valid":
        try:
            json.loads(_read(target))
        except ValueError as exc:
            base.detail = f"JSON 解析失败：{' '.join(str(exc).split())[:80]}"
            return base
        base.ok = True
        return base

    base.detail = f"未知的检查类型：{check.type}"
    return base


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:MAX_INSPECT_CHARS]


def _compile_source(path: Path) -> tuple[bool, str]:
    """只编译、不执行：语法错误是"写代码类步骤"最便宜的拦截面。"""

    try:
        compile(_read(path), path.name, "exec")
    except SyntaxError as exc:
        return False, f"语法错误：第 {exc.lineno} 行 {exc.msg}"
    except ValueError as exc:  # 例如源码里含空字节
        return False, f"无法编译：{' '.join(str(exc).split())[:80]}"
    return True, ""


def module_name_of(path: str) -> str:
    """把工作区相对路径换算成可导入的模块名；换算不出来时返回空串。

    ``app/services/verify.py`` → ``app.services.verify``；
    ``app/services/__init__.py`` → ``app.services``；
    ``my-tool/foo.py`` → 空串（带连号的目录不是合法的 Python 包名）。
    """

    text = str(path or "").strip().replace("\\", "/").strip("/")
    if text.lower().endswith(".py"):
        text = text[:-3]
    if text.endswith("/__init__"):
        text = text[: -len("/__init__")]
    parts = [part for part in text.split("/") if part]
    if not parts or any(not part.isidentifier() for part in parts):
        return ""
    return ".".join(parts)


def _run_import(
    workspace: Workspace, target: Path, path: str, *, allow_import: bool
) -> tuple[bool, str]:
    """判定"这个模块能不能被导入"。

    导入会执行模块顶层代码，因此默认**不跑**：没开「允许执行验证命令」时降级为语法
    编译检查，并在结果里写明"没有真正导入"，免得看起来验过了其实没验。
    """

    if not target.is_file():
        return False, "文件不存在"
    if target.suffix.lower() != ".py":
        return False, "py_import 只支持 .py 文件（包请指向其中的 __init__.py）"

    module = module_name_of(path)
    if not module:
        return False, f"无法从路径推断模块名（每层目录都要是合法标识符）：{path}"

    if not allow_import:
        ok, reason = _compile_source(target)
        if not ok:
            return False, reason
        return True, "未开启「允许执行验证命令」：只做了语法编译，没有真正导入"

    argv = [sys.executable, "-c", _IMPORT_SCRIPT, module]
    try:
        completed = subprocess.run(  # noqa: S603 - 参数列表执行，不经过 shell
            argv,
            cwd=str(workspace.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=IMPORT_TIMEOUT_SECONDS,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"导入超时（>{int(IMPORT_TIMEOUT_SECONDS)} 秒），已终止"
    except OSError as exc:
        return False, f"无法执行导入检查：{' '.join(str(exc).split())[:80]}"

    if completed.returncode == 0:
        return True, f"导入成功：import {module}"
    detail = _last_error_line((completed.stderr or "") + (completed.stdout or ""))
    return False, f"导入失败：{detail}"


def _last_error_line(output: str, *, limit: int = 160) -> str:
    """从 traceback 里取最后一行有内容的那句（就是异常类型与消息）。"""

    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "导入过程没有输出，退出码非 0")[:limit]


def normalize_marker(text: str) -> str:
    """把标识符归一化：忽略大小写与 ``_`` ``-`` 空格。

    真实教训：架构段要求文档里出现 ``project_id``，而执行段写的是 ``:projectId``——
    同一个东西，严格子串匹配却判成"没写"。这类标识符不一致不该把运行卡住。
    """

    return "".join(ch for ch in text.lower() if ch not in "_- \t")


def _contains(content: str, needle: str) -> tuple[bool, bool]:
    """返回 ``(是否命中, 是否为宽松命中)``。先严格子串，再归一化后匹配。"""

    if needle in content:
        return True, False
    normalized = normalize_marker(needle)
    if normalized and normalized in normalize_marker(content):
        return True, True
    return False, False


def _glob(workspace: Workspace, pattern: str) -> list[str]:
    normalized = safe_relative(pattern) or ""
    return [
        item
        for item in workspace.tree(limit=400, max_depth=8)
        if fnmatch(item, normalized) or fnmatch(item.rsplit("/", 1)[-1], normalized)
    ]


def _default_label(check: StepCheck) -> str:
    if check.type == "file_exists":
        return f"文件存在：{check.path}"
    if check.type == "dir_exists":
        return f"目录存在：{check.path}"
    if check.type == "glob":
        return f"存在匹配：{check.path}"
    if check.type == "file_contains":
        return f"{check.path} 含指定内容"
    if check.type == "py_compile":
        return f"{check.path} 语法可编译"
    if check.type == "py_import":
        return f"{check.path} 可被导入"
    if check.type == "json_valid":
        return f"{check.path} 是合法 JSON"
    return check.path


# --- 项目边界（第 4 步：验证请求同样属于某个项目） ---
#: 检查项上的项目字段名；老记录缺该字段时按契约回落到默认项目。
CHECK_PROJECT_FIELD = "project_id"


def check_project_id(check: Any) -> str:
    """读取检查项所属项目；缺字段时回落 project:default。"""

    raw: Any = None
    if isinstance(check, Mapping):
        raw = check.get(CHECK_PROJECT_FIELD)
    else:
        raw = getattr(check, CHECK_PROJECT_FIELD, None)
    if raw is None and hasattr(check, "model_dump"):
        try:
            dumped = check.model_dump()
        except Exception:  # noqa: BLE001 - 老记录可能不允许导出
            dumped = None
        if isinstance(dumped, Mapping):
            raw = dumped.get(CHECK_PROJECT_FIELD)
    value = str(raw or "").strip()
    return value or DEFAULT_PROJECT_ID


def ensure_check_project(
    check: Any, requested_project_id: str, *, context_id: str | None = None
) -> None:
    """跨项目验证请求在跑检查前就被拒绝，不触碰目标项目数据。"""

    ensure_same_project(check_project_id(check), requested_project_id, context_id=context_id)


def ensure_checks_project(
    checks: Iterable[Any], requested_project_id: str, *, context_id: str | None = None
) -> list[Any]:
    """整批检查项都归属同一项目时才放行。"""

    materialized = list(checks)
    for check in materialized:
        ensure_check_project(check, requested_project_id, context_id=context_id)
    return materialized
