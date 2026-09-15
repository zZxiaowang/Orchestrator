"""步骤级**客观验收**：用可判定的检查替代「模型自己说完成了」。

设计约束（为什么只做这几类检查）：

* 只做**无副作用、不执行任意代码**的判定：文件/目录是否存在、内容是否包含某片段、
  Python 能否编译（``compile()`` 只编译不执行）、JSON 是否合法、通配是否匹配。
* 命令类验收（pytest、构建脚本等）会真的执行外部程序，必须配合白名单与用户逐条确认，
  属于 P1「受控命令执行」，本轮**不实现**，也不假装支持。
* 检查全部走 ``Workspace.resolve``，因此和文件落地共用同一套越界防护。

验收不通过时，步骤**不得**标记为完成；编排器会把它标成 ``blocked`` 并把失败原因摊开，
用户可以补充说明后只重跑这一步。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.core.errors import WorkspaceError
from app.schemas.plan import CheckResult, StepCheck
from app.services.gitguard import safe_relative
from app.services.workspace import Workspace

#: 单个文件最多读取多少字符再判定（避免一个巨大的产物把验收拖死）
MAX_INSPECT_CHARS = 2_000_000

#: 一步最多自动跑多少条检查，防止纲领里塞进几十条把执行拖慢
MAX_CHECKS_PER_STEP = 8


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


def effective_checks(
    declared: Sequence[StepCheck] | None,
    deliverables: Iterable[str],
    *,
    limit: int = MAX_CHECKS_PER_STEP,
) -> list[StepCheck]:
    """本步真正要跑的检查 = 纲领声明的 + 从交付物派生的（去重、限量）。"""

    merged: list[StepCheck] = []
    seen: set[tuple[str, str, str]] = set()
    for check in [*list(declared or []), *derive_checks(deliverables)]:
        key = (check.type, check.path.replace("\\", "/").lower(), check.text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(check)
        if len(merged) >= limit:
            break
    return merged


def run_checks(workspace: Workspace, checks: Sequence[StepCheck]) -> list[CheckResult]:
    """逐条执行检查；任何一条抛异常都被收敛成「不通过 + 原因」，不会中断整步。"""

    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(_run_one(workspace, check))
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


def _run_one(workspace: Workspace, check: StepCheck) -> CheckResult:
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
        source = _read(target)
        try:
            compile(source, target.name, "exec")
        except SyntaxError as exc:
            base.detail = f"语法错误：第 {exc.lineno} 行 {exc.msg}"
            return base
        base.ok = True
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
    if check.type == "json_valid":
        return f"{check.path} 是合法 JSON"
    return check.path
