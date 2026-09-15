"""客观验收：检查项解析、派生、执行与汇总。

这一层是「模型说完成」与「确实完成」之间的分界线，所以每条规则都要有测试钉住：
哪些能自动判、哪些必须拒绝、没有检查项时绝不能算通过。
"""

from __future__ import annotations

from pathlib import Path

from app.schemas.plan import StepCheck
from app.services.verify import (
    derive_checks,
    effective_checks,
    failure_text,
    parse_checks,
    run_checks,
    summarize,
)
from app.services.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    return Workspace(root)


# ── 解析与派生 ───────────────────────────────────────────────────────────


def test_parse_checks_accepts_strings_and_aliases():
    checks = parse_checks(
        ["src/app.py", {"type": "compile", "path": "src/app.py"}, {"type": "瞎写", "path": "a.txt"}]
    )
    assert [item.type for item in checks] == ["file_exists", "py_compile", "file_exists"]
    assert checks[0].path == "src/app.py"


def test_parse_checks_drops_items_without_path():
    assert parse_checks([{"type": "file_exists"}, {}]) == []


def test_derive_checks_takes_paths_and_skips_prose():
    derived = derive_checks(
        [
            "docs/plan.md",
            "把结论写成一段说明，不落盘",
            "scripts/build.ps1",
            "src/",
            "Makefile",
        ]
    )
    assert [item.path for item in derived] == ["docs/plan.md", "scripts/build.ps1"]


def test_effective_checks_merges_declared_and_derived_without_duplicates():
    declared = [StepCheck(type="file_exists", path="docs/plan.md", label="声明过的")]
    merged = effective_checks(declared, ["docs/plan.md", "README.md"])
    assert [(item.type, item.path) for item in merged] == [
        ("file_exists", "docs/plan.md"),
        ("file_exists", "README.md"),
    ]
    assert merged[0].label == "声明过的"


def test_effective_checks_respects_limit():
    merged = effective_checks(None, [f"a{i}.txt" for i in range(20)], limit=3)
    assert len(merged) == 3


# ── 逐类检查 ─────────────────────────────────────────────────────────────


def test_file_exists_and_dir_exists(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace.root / "sub").mkdir()
    (workspace.root / "a.txt").write_text("hello", encoding="utf-8")
    results = run_checks(
        workspace,
        [
            StepCheck(type="file_exists", path="a.txt"),
            StepCheck(type="file_exists", path="missing.txt"),
            StepCheck(type="dir_exists", path="sub"),
        ],
    )
    assert [item.ok for item in results] == [True, False, True]
    assert results[1].detail == "文件不存在"


def test_file_contains_checks_actual_content(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace.root / "README.md").write_text("# 安装\npip install x\n", encoding="utf-8")
    ok, bad, empty = run_checks(
        workspace,
        [
            StepCheck(type="file_contains", path="README.md", text="安装"),
            StepCheck(type="file_contains", path="README.md", text="部署"),
            StepCheck(type="file_contains", path="README.md"),
        ],
    )
    assert ok.ok is True
    assert bad.ok is False and "部署" in bad.detail
    assert empty.ok is False and "text" in empty.detail


def test_file_contains_is_lenient_about_identifier_style(tmp_path: Path):
    """回归：file_contains 对标识符要宽容（project_id ≡ projectId）。

    真实教训：纲领要求文档含 ``project_id``，执行段写的是 ``:projectId``，
    严格子串匹配把对的产出判成没写，运行被卡住。
    """

    workspace = _workspace(tmp_path)
    (workspace.root / "contract.md").write_text(
        "路由：`#/projects/:projectId` 由 projectId 标识项目\n", encoding="utf-8"
    )
    loose, strict, missing = run_checks(
        workspace,
        [
            StepCheck(type="file_contains", path="contract.md", text="project_id"),
            StepCheck(type="file_contains", path="contract.md", text="projectId"),
            StepCheck(type="file_contains", path="contract.md", text="project_number"),
        ],
    )
    assert loose.ok is True and "宽松" in loose.detail
    assert strict.ok is True and strict.detail == ""  # 完全相同就是严格命中
    assert missing.ok is False


def test_py_compile_catches_syntax_error_without_executing(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace.root / "good.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace.root / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (workspace.root / "side_effect.py").write_text(
        "raise RuntimeError('编译不该执行这段代码')\n", encoding="utf-8"
    )

    results = run_checks(
        workspace,
        [
            StepCheck(type="py_compile", path="good.py"),
            StepCheck(type="py_compile", path="bad.py"),
            StepCheck(type="py_compile", path="side_effect.py"),
        ],
    )
    assert results[0].ok is True
    assert results[1].ok is False and "语法错误" in results[1].detail
    # 只编译不执行：会抛异常的文件依然算"语法通过"
    assert results[2].ok is True


def test_json_valid_rejects_broken_json(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace.root / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    (workspace.root / "bad.json").write_text("{a: 1,}", encoding="utf-8")
    ok, bad = run_checks(
        workspace,
        [
            StepCheck(type="json_valid", path="ok.json"),
            StepCheck(type="json_valid", path="bad.json"),
        ],
    )
    assert ok.ok is True
    assert bad.ok is False and "JSON" in bad.detail


def test_glob_matches_by_pattern(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace.root / "docs").mkdir()
    (workspace.root / "docs" / "a.md").write_text("x", encoding="utf-8")
    hit, miss = run_checks(
        workspace,
        [StepCheck(type="glob", path="docs/*.md"), StepCheck(type="glob", path="docs/*.rst")],
    )
    assert hit.ok is True
    assert miss.ok is False


def test_path_escape_is_rejected_not_treated_as_present(tmp_path: Path):
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    results = run_checks(workspace, [StepCheck(type="file_exists", path="../outside.txt")])
    assert results[0].ok is False
    assert "不合法" in results[0].detail


def test_check_exception_becomes_failure_not_crash(tmp_path: Path, monkeypatch):
    workspace = _workspace(tmp_path)
    (workspace.root / "a.txt").write_text("x", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("磁盘错误")

    monkeypatch.setattr(Workspace, "resolve", boom)
    results = run_checks(workspace, [StepCheck(type="file_exists", path="a.txt")])
    assert results[0].ok is False
    assert "检查执行异常" in results[0].detail


# ── 汇总 ────────────────────────────────────────────────────────────────


def test_summarize_never_counts_empty_as_passed():
    verdict = summarize([])
    assert verdict["unverified"] is True
    assert verdict["passed_all"] is False


def test_summarize_counts_failures(tmp_path: Path):
    workspace = _workspace(tmp_path)
    (workspace.root / "a.txt").write_text("x", encoding="utf-8")
    results = run_checks(
        workspace,
        [StepCheck(type="file_exists", path="a.txt"), StepCheck(type="file_exists", path="b.txt")],
    )
    verdict = summarize(results)
    assert verdict == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "unverified": False,
        "passed_all": False,
    }


def test_failure_text_lists_reasons():
    from app.schemas.plan import CheckResult

    text = failure_text(
        [
            CheckResult(
                type="file_exists",
                path="a.md",
                label="交付物存在：a.md",
                ok=False,
                detail="文件不存在",
            ),
            CheckResult(type="file_exists", path="b.md", ok=True, label="b.md"),
        ]
    )
    assert "a.md" in text and "文件不存在" in text
    assert "b.md" not in text
