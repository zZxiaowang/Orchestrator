"""受控命令执行：白名单、危险字符、工作区边界、超时与输出截断。"""

from __future__ import annotations

import sys
from pathlib import Path

from app.services.commands import (
    check_command,
    normalize_allowlist,
    run_allowed,
    run_command,
)

PYTHON = Path(sys.executable).name  # 例如 python.exe


def test_normalize_allowlist_strips_blanks_and_duplicates():
    assert normalize_allowlist("  python -m pytest \n\n ruff \npython -m pytest\n") == [
        "python -m pytest",
        "ruff",
    ]
    assert normalize_allowlist(None) == []
    assert normalize_allowlist(["", "  "]) == []


def test_disabled_execution_refuses_everything(tmp_path: Path):
    result = run_command(
        "python -m pytest", cwd=tmp_path, allowlist=["python -m pytest"], enabled=False
    )
    assert result.skipped is True
    assert result.ok is False
    assert "未开启" in result.error


def test_command_outside_whitelist_is_refused(tmp_path: Path):
    reason = check_command("rm -rf /", ["python -m pytest"], enabled=True)
    assert reason and "不在白名单" in reason


def test_shell_metacharacters_are_refused_even_if_whitelisted(tmp_path: Path):
    for cmd in (
        "python -m pytest && rm -rf .",
        "python -m pytest | more",
        "python -m pytest > out.txt",
        "python -m pytest %TEMP%",
        "python -m pytest `whoami`",
        "python -m pytest; del *",
    ):
        result = run_command(cmd, cwd=tmp_path, allowlist=["python -m pytest"], enabled=True)
        assert result.skipped is True, cmd
        assert result.exit_code is None, cmd
        assert "禁止" in result.error, cmd


def test_prefix_match_requires_a_whole_token(tmp_path: Path):
    # "python" 不能匹配 "pythonx"，但能匹配 "python -m pytest"
    assert check_command("pythonx -m pytest", ["python"], enabled=True) != ""
    assert check_command("python -m pytest -q", ["python"], enabled=True) == ""


def test_allowed_command_runs_inside_workspace(tmp_path: Path):
    script = tmp_path / "where.py"
    script.write_text("import os; print(os.getcwd())", encoding="utf-8")
    result = run_command(
        f"{PYTHON} where.py",
        cwd=tmp_path,
        allowlist=[PYTHON],
        enabled=True,
        timeout=30,
    )
    assert result.ok is True, result.error or result.output
    assert result.exit_code == 0
    assert str(tmp_path) in result.output


def test_failing_command_reports_exit_code_and_output(tmp_path: Path):
    script = tmp_path / "boom.py"
    script.write_text("import sys\nprint('第 1 行')\nsys.exit(3)\n", encoding="utf-8")
    result = run_command(
        f"{PYTHON} boom.py", cwd=tmp_path, allowlist=[PYTHON], enabled=True, timeout=30
    )
    assert result.ok is False
    assert result.exit_code == 3
    assert "第 1 行" in result.output


def test_missing_executable_is_reported_not_raised(tmp_path: Path):
    result = run_command(
        "definitely-not-a-real-binary --version",
        cwd=tmp_path,
        allowlist=["definitely-not-a-real-binary"],
        enabled=True,
    )
    assert result.ok is False
    assert "找不到可执行文件" in result.error


def test_timeout_is_bounded_and_reported(tmp_path: Path):
    script = tmp_path / "sleepy.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    result = run_command(
        f"{PYTHON} sleepy.py", cwd=tmp_path, allowlist=[PYTHON], enabled=True, timeout=1.5
    )
    assert result.ok is False
    assert "超时" in result.error
    assert result.duration_ms >= 1000


def test_long_output_is_truncated_head_and_tail(tmp_path: Path):
    script = tmp_path / "noisy.py"
    script.write_text("print('A' * 5000)\nprint('TAIL-MARK')\n", encoding="utf-8")
    result = run_command(
        f"{PYTHON} noisy.py", cwd=tmp_path, allowlist=[PYTHON], enabled=True, timeout=30
    )
    assert result.truncated is True
    assert "省略" in result.output
    assert result.output.endswith("TAIL-MARK\n")  # 尾部必须保留：报错通常在这里


def test_run_allowed_keeps_going_after_a_failure(tmp_path: Path):
    script = tmp_path / "boom.py"
    script.write_text("import sys; sys.exit(2)\n", encoding="utf-8")
    ok_script = tmp_path / "fine.py"
    ok_script.write_text("print('fine')\n", encoding="utf-8")
    results = run_allowed(
        [f"{PYTHON} boom.py", f"{PYTHON} fine.py", "rm -rf /"],
        cwd=tmp_path,
        allowlist=[PYTHON],
        enabled=True,
        timeout=30,
    )
    assert [item.ok for item in results] == [False, True, False]
    assert results[0].exit_code == 2
    assert results[2].skipped is True
