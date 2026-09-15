"""第 2 步验收：项目上下文数据契约的契约测试。

覆盖三类场景：

1. 无项目上下文：缺失即报错；历史数据按契约回落到 ``project:default``。
2. 有效项目上下文：项目解析成功，计划 / 运行 / 步骤 / 验证结果都能解析出 ``project_id``。
3. 跨项目访问被拒绝：无权限、上下文冲突、跨项目读取都返回可区分的错误语义。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.navigation import (  # noqa: E402
    CHAT_CONTEXT_TYPE,
    PRIMARY_NAVIGATION,
    PROJECT_CONTEXT_TYPE,
    PROJECT_ONLY_ACTIONS,
    NavEntry,
    ProjectModule,
    ProjectModuleContext,
    action_allowed,
    build_project_route,
    default_project_route,
    module_from_route,
    route_project_id,
)
from app.schemas.project import (  # noqa: E402
    DEFAULT_PROJECT_ID,
    PlanReadContract,
    Project,
    ProjectContextError,
    ProjectContextErrorCode,
    ProjectContextResolver,
    ProjectWorkspace,
    RunReadContract,
    StepReadContract,
    VerificationResultReadContract,
    chat_context_id,
    ensure_same_project,
    parse_context_id,
    project_context_id,
    project_id_from_context_id,
    project_id_from_record,
    read_project_record,
)


def build_project(project_id: str, name: str | None = None) -> Project:
    return Project(
        project_id=project_id,
        name=name or project_id,
        workspace=ProjectWorkspace(
            workspace_id=f"ws-{project_id}",
            root_path=f"/workspaces/{project_id}",
        ),
    )


@pytest.fixture()
def resolver() -> ProjectContextResolver:
    return ProjectContextResolver([build_project("alpha"), build_project("beta")])


class TestProjectContract:
    def test_project_fields_cover_id_name_status_workspace_activity(self) -> None:
        project = Project(
            project_id="alpha",
            name="Alpha",
            workspace=ProjectWorkspace(workspace_id="ws-1", root_path="/workspaces/alpha"),
        )
        assert project.project_id == "alpha"
        assert project.name == "Alpha"
        assert project.status.value == "active"
        assert project.workspace is not None
        assert project.workspace.workspace_id == "ws-1"
        assert project.created_at is not None
        assert project.updated_at is not None
        assert project.last_activity_at is not None
        assert project.context_id == "project:alpha"
        assert project.is_active is True

    def test_project_rejects_invalid_identity(self) -> None:
        with pytest.raises(ValidationError):
            Project(project_id="bad id", name="X")
        with pytest.raises(ValidationError):
            Project(project_id="alpha", name="   ")


class TestMissingProjectContext:
    def test_missing_context_raises_missing_error(self, resolver) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            resolver.resolve()
        error = excinfo.value
        assert error.code is ProjectContextErrorCode.MISSING_PROJECT_CONTEXT
        assert error.status_code == 400
        assert error.project_id is None
        assert error.to_dict()["code"] == "missing_project_context"

    def test_blank_context_id_is_treated_as_missing(self, resolver) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            resolver.resolve("   ")
        assert excinfo.value.code is ProjectContextErrorCode.MISSING_PROJECT_CONTEXT

    def test_optional_context_can_be_absent(self, resolver) -> None:
        assert resolver.resolve(required=False) is None

    def test_legacy_records_fall_back_to_default_project(self) -> None:
        plan = PlanReadContract.model_validate({"plan_id": "plan-legacy"})
        run = RunReadContract.model_validate({"run_id": "run-legacy"})
        step = StepReadContract.model_validate({"step_id": "step-legacy", "run_id": "run-legacy"})
        result = VerificationResultReadContract.model_validate(
            {"verification_id": "verify-legacy", "run_id": "run-legacy"}
        )
        for record in (plan, run, step, result):
            assert record.project_id == DEFAULT_PROJECT_ID
        assert run.context_type == PROJECT_CONTEXT_TYPE
        assert run.context_id is None


class TestValidProjectContext:
    def test_resolve_by_context_id(self, resolver) -> None:
        context = resolver.resolve("project:alpha")
        assert context is not None
        assert context.project_id == "alpha"
        assert context.context_id == "project:alpha"
        assert context.context_type == PROJECT_CONTEXT_TYPE
        assert context.project_name == "alpha"
        assert context.workspace_id == "ws-alpha"
        assert context.is_default is False

    def test_resolve_by_project_id_is_equivalent(self, resolver) -> None:
        by_id = resolver.resolve(project_id="alpha")
        by_context = resolver.resolve("project:alpha")
        assert by_id == by_context
        mixed = resolver.resolve("project:alpha", project_id="alpha")
        assert mixed is not None
        assert mixed.context_id == "project:alpha"

    def test_default_project_is_flagged(self) -> None:
        resolver = ProjectContextResolver([build_project(DEFAULT_PROJECT_ID)])
        context = resolver.resolve(f"project:{DEFAULT_PROJECT_ID}")
        assert context is not None
        assert context.is_default is True
        assert default_project_route() == "#/projects/default"

    def test_plan_run_step_verification_parse_project_id(self) -> None:
        plan = PlanReadContract.model_validate(
            {"plan_id": "plan-1", "context_id": "project:alpha", "phases": []}
        )
        run = RunReadContract.model_validate({"run_id": "run-1", "context_id": "project:alpha"})
        step = StepReadContract.model_validate(
            {"step_id": "step-1", "run_id": "run-1", "project_id": "alpha"}
        )
        result = VerificationResultReadContract.model_validate(
            {"verification_id": "verify-1", "step_id": "step-1", "project_id": "alpha"}
        )
        assert plan.project_id == "alpha"
        assert run.project_id == "alpha"
        assert run.context_id == "project:alpha"
        assert step.project_id == "alpha"
        assert result.project_id == "alpha"

    def test_read_project_record_dispatches_by_kind(self) -> None:
        record = read_project_record("run", {"run_id": "run-9", "project_id": "alpha"})
        assert isinstance(record, RunReadContract)
        assert record.project_id == "alpha"
        verification = read_project_record("verification", {"verification_id": "verify-9"})
        assert isinstance(verification, VerificationResultReadContract)
        with pytest.raises(ValueError):
            read_project_record("unknown-kind", {})

    def test_context_id_helpers(self) -> None:
        assert project_context_id("alpha") == "project:alpha"
        assert chat_context_id("session-1") == "chat:session-1"
        assert parse_context_id("project:alpha") == ("project", "alpha")
        assert project_id_from_context_id("project:alpha") == "alpha"
        assert project_id_from_context_id("chat:session-1") is None
        assert project_id_from_record({"project_id": "alpha"}) == "alpha"
        assert project_id_from_record({"context_id": "project:beta"}) == "beta"
        assert project_id_from_record({}) == DEFAULT_PROJECT_ID


class TestCrossProjectAccessDenied:
    def test_project_outside_allowed_set_is_denied(self) -> None:
        resolver = ProjectContextResolver(
            [build_project("alpha"), build_project("beta")],
            allowed_project_ids={"alpha"},
        )
        with pytest.raises(ProjectContextError) as excinfo:
            resolver.resolve("project:beta")
        error = excinfo.value
        assert error.code is ProjectContextErrorCode.PROJECT_ACCESS_DENIED
        assert error.status_code == 403
        assert error.project_id == "beta"
        assert resolver.resolve("project:alpha") is not None

    def test_context_id_conflicting_with_project_id_is_denied(self, resolver) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            resolver.resolve("project:alpha", project_id="beta")
        assert excinfo.value.code is ProjectContextErrorCode.PROJECT_ACCESS_DENIED
        assert excinfo.value.status_code == 403

    def test_error_semantics_are_distinguishable(self, resolver) -> None:
        cases = (
            (lambda: resolver.resolve(), ProjectContextErrorCode.MISSING_PROJECT_CONTEXT, 400),
            (
                lambda: resolver.resolve("project:missing"),
                ProjectContextErrorCode.PROJECT_NOT_FOUND,
                404,
            ),
            (
                lambda: resolver.resolve("project:alpha", project_id="beta"),
                ProjectContextErrorCode.PROJECT_ACCESS_DENIED,
                403,
            ),
            (
                lambda: resolver.resolve("no-prefix"),
                ProjectContextErrorCode.INVALID_PROJECT_CONTEXT,
                400,
            ),
        )
        codes = []
        for call, expected_code, expected_status in cases:
            with pytest.raises(ProjectContextError) as excinfo:
                call()
            assert excinfo.value.code is expected_code
            assert excinfo.value.status_code == expected_status
            codes.append(excinfo.value.code.value)
        assert len(set(codes)) == len(cases)

    def test_cross_project_record_read_is_denied(self) -> None:
        step = StepReadContract.model_validate({"step_id": "step-1", "project_id": "alpha"})
        ensure_same_project(step.project_id, "alpha")
        with pytest.raises(ProjectContextError) as excinfo:
            ensure_same_project(step.project_id, "beta", context_id="project:beta")
        assert excinfo.value.code is ProjectContextErrorCode.PROJECT_ACCESS_DENIED
        assert excinfo.value.status_code == 403

    def test_chat_context_cannot_read_project_records(self) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            project_id_from_record({"run_id": "run-1", "context_id": "chat:session-1"})
        assert excinfo.value.code is ProjectContextErrorCode.INVALID_PROJECT_CONTEXT
        with pytest.raises((ProjectContextError, ValidationError)):
            RunReadContract.model_validate({"run_id": "run-1", "context_id": "chat:session-1"})

    def test_chat_context_id_rejected_by_resolver(self, resolver) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            resolver.resolve("chat:session-1")
        assert excinfo.value.code is ProjectContextErrorCode.INVALID_PROJECT_CONTEXT

    def test_invalid_project_id_is_rejected(self, resolver) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            resolver.resolve(project_id="bad id")
        assert excinfo.value.code is ProjectContextErrorCode.INVALID_PROJECT_CONTEXT


class TestNavigationContract:
    def test_only_two_primary_entries(self) -> None:
        assert [entry.entry for entry in PRIMARY_NAVIGATION] == [NavEntry.CHAT, NavEntry.PROJECTS]
        chat_entry, projects_entry = PRIMARY_NAVIGATION
        assert chat_entry.context_type == CHAT_CONTEXT_TYPE
        assert chat_entry.requires_project is False
        assert chat_entry.modules == ()
        assert projects_entry.context_type == PROJECT_CONTEXT_TYPE
        assert set(projects_entry.modules) == set(ProjectModule)

    def test_project_only_actions_blocked_in_chat_context(self) -> None:
        for action in PROJECT_ONLY_ACTIONS:
            assert action_allowed(PROJECT_CONTEXT_TYPE, action) is True
            assert action_allowed(CHAT_CONTEXT_TYPE, action) is False
        assert action_allowed(CHAT_CONTEXT_TYPE, "chat") is True
        with pytest.raises(ProjectContextError):
            action_allowed("unknown", "execution")

    def test_project_module_route_and_context(self) -> None:
        assert build_project_route(ProjectModule.EXECUTION, "alpha") == "#/projects/alpha/execution"
        assert module_from_route("#/projects/alpha/plan") is ProjectModule.PLAN
        assert module_from_route("#/projects/alpha") is ProjectModule.OVERVIEW
        assert module_from_route("#/projects") is None
        assert route_project_id("#/projects/alpha/execution") == "alpha"
        assert route_project_id("#/chat") is None
        context = ProjectModuleContext.build(ProjectModule.VERIFICATION, "alpha")
        assert context.context_id == "project:alpha"
        assert context.context_type == PROJECT_CONTEXT_TYPE
        assert context.route == "#/projects/alpha/verification"
        assert context.module is ProjectModule.VERIFICATION

    def test_chat_route_is_not_a_project_module(self) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            module_from_route("#/chat")
        assert excinfo.value.code is ProjectContextErrorCode.INVALID_PROJECT_CONTEXT

    def test_project_module_route_requires_project_id(self) -> None:
        with pytest.raises(ProjectContextError) as excinfo:
            build_project_route(ProjectModule.PLAN, "")
        assert excinfo.value.code is ProjectContextErrorCode.MISSING_PROJECT_CONTEXT
