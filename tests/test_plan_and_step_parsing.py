"""模型输出解析：容错、归一化、异常。"""

from __future__ import annotations

import pytest

from app.core.errors import PlanParseError
from app.services.architect import parse_plan
from app.services.executor import parse_step_output


def test_plan_from_fenced_json():
    text = (
        "```json\n"
        '{"goal":"目标","summary":"说明","steps":[{"title":"第一步","goal":"做 A",'
        '"acceptance":["A 完成"]}]}\n'
        "```"
    )
    plan = parse_plan(text)
    assert plan.goal == "目标"
    assert plan.steps[0].id == 1
    assert plan.steps[0].acceptance == ["A 完成"]


def test_plan_tolerates_surrounding_prose_and_string_steps():
    text = '好的，这是架构：\n{"goal":"G","steps":["先建目录","再写代码"]}\n希望有帮助。'
    plan = parse_plan(text)
    assert [step.title for step in plan.steps] == ["先建目录", "再写代码"]
    assert [step.id for step in plan.steps] == [1, 2]


def test_plan_remaps_dependencies_and_truncates():
    text = (
        '{"goal":"G","steps":['
        '{"id":7,"title":"A"},{"id":9,"title":"B","depends_on":[7,99]},'
        '{"id":11,"title":"C"}]}'
    )
    plan = parse_plan(text, max_steps=2)
    assert [step.id for step in plan.steps] == [1, 2]
    assert plan.steps[1].depends_on == [1]
    assert plan.open_questions and "截断" in plan.open_questions[0]


def test_plan_without_steps_gets_single_fallback_step():
    plan = parse_plan('{"goal":"只有目标","summary":"说明"}')
    assert len(plan.steps) == 1
    assert plan.steps[0].title == "只有目标"


def test_plan_raises_on_non_json():
    with pytest.raises(PlanParseError) as excinfo:
        parse_plan("我觉得应该先做 A 再做 B。")
    assert "excerpt" in excinfo.value.details


def test_step_output_parsing_and_coercion():
    text = (
        '{"summary":"改了文件","files":[{"path":"a.py","edits":[{"search":"x","replace":"y"}]},'
        '"b.md"],"commands":["pytest -q"],"notes":"单条说明"}'
    )
    output = parse_step_output(text)
    assert output is not None
    assert output.files[0].action == "update"
    assert output.files[1].action == "create"
    assert output.commands[0].cmd == "pytest -q"
    assert output.notes == ["单条说明"]


def test_step_output_returns_none_for_garbage():
    assert parse_step_output("抱歉，我无法完成。") is None
