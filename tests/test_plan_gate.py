"""纲领质量门：执行前把"这一步没法判定 / 粒度失控"的问题说清楚。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.schemas.plan import ArchitecturePlan
from app.services import plan_gate
from tests.conftest import FakeRelay
from tests.test_api_flow import TERMINAL, build_client, wait_for_status


def _plan(steps: list[dict[str, Any]]) -> ArchitecturePlan:
    return ArchitecturePlan.model_validate({"goal": "目标", "summary": "说明", "steps": steps})


def test_healthy_plan_has_no_warnings():
    plan = _plan(
        [
            {
                "id": 1,
                "title": "写契约文档",
                "goal": "写契约",
                "deliverables": ["docs/contract.md"],
                "acceptance": ["文件里出现 project_id"],
                "checks": [{"type": "file_exists", "path": "docs/contract.md"}],
            },
            {
                "id": 2,
                "title": "写实现",
                "goal": "写实现",
                "deliverables": ["app/main.py"],
                "acceptance": ["app/main.py 能编译"],
            },
        ]
    )

    assert plan_gate.review(plan) == []


def test_step_without_judgeable_acceptance_is_flagged():
    """既没有路径交付物、也没有 checks：系统根本判定不了这一步做没做完。"""

    plan = _plan(
        [
            {
                "id": 1,
                "title": "优化体验",
                "goal": "让体验更好",
                "deliverables": ["把整体体验做到令人满意"],
                "acceptance": ["质量良好"],
            }
        ]
    )

    warnings = plan_gate.review(plan)

    assert any("无法客观判定" in item for item in warnings), warnings
    assert any("无法验证的说法" in item for item in warnings), warnings


def test_too_many_deliverables_and_steps_are_flagged():
    many = [
        {
            "id": index + 1,
            "title": f"第 {index + 1} 步",
            "goal": "做点事",
            "deliverables": [f"docs/f{index}-{n}.md" for n in range(7)],
            "acceptance": ["文件存在"],
        }
        for index in range(9)
    ]

    warnings = plan_gate.review(_plan(many))

    assert any("超过建议的 8 步" in item for item in warnings), warnings
    assert any("超过建议的 6 个" in item for item in warnings), warnings


def test_gate_warnings_land_in_plan_open_questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """接进主链路：告警必须写进 run.plan.open_questions，界面才看得见。"""

    bad_plan = {
        "goal": "做个东西",
        "summary": "先优化一下，再做点别的。",
        "steps": [
            {
                "id": 1,
                "title": "优化体验",
                "goal": "让体验更好",
                "deliverables": ["整体体验"],
                "acceptance": ["质量良好"],
            }
        ],
    }
    monkeypatch.setattr("tests.conftest.plan_payload", lambda: bad_plan)

    relay = FakeRelay()
    with build_client(tmp_path, relay) as client:
        run_id = client.post("/api/v1/runs", json={"task": "随便做点什么"}).json()["run"]["id"]
        wait_for_status(client, run_id, {"awaiting_approval"})
        run = client.get(f"/api/v1/runs/{run_id}").json()["run"]

        questions = run["plan"]["open_questions"]
        assert any("无法客观判定" in item for item in questions), questions

        # 告警只是提醒，不能拦住执行
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        done = wait_for_status(client, run_id, TERMINAL)
        assert done["status"] == "done", done.get("error")
