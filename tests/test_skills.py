"""Skills：SKILL.md 解析、资格校验、三种安装来源与"按需注入"。

形态对齐事实标准（Codex / Claude 的 skill 都是 SKILL.md + 可选 scripts/references/assets），
所以这里把"什么算合格 skill""装进来之后放哪""什么时候注入"都钉死。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from app.capabilities.skills import (
    build_skill_section,
    install_local,
    load_skill_dir,
    parse_github_location,
    parse_skill_md,
    read_skill_body,
    select_skills,
)
from app.schemas.capability import Capability, CapabilityKind
from tests.conftest import FakeRelay, build_project_client

BUNDLED_SKILL = Path(__file__).resolve().parents[1] / "skills" / "code-review"

GOOD_SKILL = """---
name: PDF 处理
description: 从 PDF 里抽文本、拆表格，处理扫描件时用得上。
triggers: [pdf, 扫描件]
version: 2.1.0
---

## 步骤
1. 先看是不是扫描件；
2. 抽取文本并保留页码。
"""


def test_parse_skill_md_reads_frontmatter_and_body():
    parsed = parse_skill_md(GOOD_SKILL, fallback_id="pdf-tool")
    assert parsed.name == "PDF 处理"
    assert parsed.version == "2.1.0"
    assert parsed.triggers == ["pdf", "扫描件"]
    assert parsed.body.startswith("## 步骤")
    # 没写 id 时用目录名当 ID（目录名通常是稳定的英文）
    assert parsed.id == "pdf-tool"
    # 没有目录名时用名字压出的 slug
    assert parse_skill_md(GOOD_SKILL).id == "pdf"
    # 名字全是中文（压不出合法 ID）→ 稳定短号，同一个名字每次一样
    chinese_only = GOOD_SKILL.replace("name: PDF 处理", "name: 处理文档")
    hashed = parse_skill_md(chinese_only)
    assert hashed.id.startswith("skill-")
    assert hashed.id == parse_skill_md(chinese_only).id


def test_parse_skill_md_requires_name_and_description():
    for text, keyword in (
        ("---\ndescription: 只有说明\n---\n正文", "name"),
        ("---\nname: 只有名字\n---\n正文", "description"),
        ("", "空"),
        ("---\nname: [坏的\n---\n正文", "YAML"),
    ):
        with pytest.raises(Exception) as exc:  # noqa: BLE001 - 断言错误码
            parse_skill_md(text)
        assert getattr(exc.value, "code", "") == "invalid_skill"
        assert keyword in str(exc.value)


def test_load_skill_dir_requires_entry_and_detects_scripts():
    parsed = load_skill_dir(BUNDLED_SKILL)
    assert parsed.name == "代码审查清单"
    assert "scripts/checklist.py" in parsed.scripts
    assert "SKILL.md" in parsed.files

    with pytest.raises(Exception) as exc:
        load_skill_dir(BUNDLED_SKILL.parent)
    assert getattr(exc.value, "code", "") == "invalid_skill"


def test_install_local_copies_and_describes(tmp_path: Path):
    capability = install_local(BUNDLED_SKILL, root=tmp_path / "capabilities")
    assert capability.kind is CapabilityKind.SKILL
    assert capability.name == "代码审查清单"
    assert "scripts" in capability.permissions  # 脚本被登记
    installed = Path(str(capability.meta["path"]))
    assert (installed / "SKILL.md").is_file()
    assert (installed / "scripts" / "checklist.py").is_file()
    # 安装只复制，不执行：脚本还在原地，没有被跑过的痕迹
    assert read_skill_body(installed).startswith("## 什么时候用")


def test_install_local_moves_with_data_dir(tmp_path: Path):
    """源目录消失后，已装技能仍然可用（读的是数据目录里的副本）。"""

    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")
    capability = install_local(source, root=tmp_path / "capabilities")
    source.rename(tmp_path / "gone")
    assert read_skill_body(Path(str(capability.meta["path"]))).startswith("## 步骤")


def test_parse_github_location_accepts_common_forms():
    assert parse_github_location("owner/repo") == ("owner", "repo", "HEAD", "")
    assert parse_github_location("owner/repo#v1/skills/pdf") == (
        "owner",
        "repo",
        "v1",
        "skills/pdf",
    )
    assert parse_github_location("https://github.com/owner/repo/tree/main/skills/x") == (
        "owner",
        "repo",
        "HEAD",
        "tree/main/skills/x",
    )
    for bad in ("", "only-owner"):
        with pytest.raises(Exception) as exc:  # noqa: BLE001
            parse_github_location(bad)
        assert getattr(exc.value, "code", "") == "invalid_skill_source"


def test_select_skills_matches_declared_triggers():
    capability = Capability(
        id="code-review",
        kind=CapabilityKind.SKILL,
        name="代码审查清单",
        description="写代码时按清单自查",
        meta={"triggers": ["代码", "修复"], "path": str(BUNDLED_SKILL)},
    )
    other = Capability(id="pdf", kind=CapabilityKind.SKILL, name="PDF 处理", meta={"path": ""})
    picked = select_skills([capability, other], text="第 2 步：修复登录接口的代码")
    assert [item.id for item in picked] == ["code-review"]
    assert select_skills([capability], text="写一份产品说明文档") == []


def test_build_skill_section_is_bounded():
    capability = Capability(
        id="code-review",
        kind=CapabilityKind.SKILL,
        name="代码审查清单",
        meta={"path": str(BUNDLED_SKILL)},
    )
    section = build_skill_section([capability])
    assert section.startswith("## 可用技能")
    assert "代码审查清单" in section
    assert len(section) < 2600


def test_install_via_api_and_step_injects_skill(tmp_path: Path):
    """端到端：装技能 → 执行段拿到技能指令 → 步骤记录用了哪个技能。"""

    relay = FakeRelay()
    with build_project_client(tmp_path, relay) as client:
        installed = client.post(
            "/api/v1/capabilities/skills/install",
            json={"source": "local", "location": str(BUNDLED_SKILL)},
        )
        assert installed.status_code == 201, installed.text
        capability = installed.json()["capability"]
        assert capability["kind"] == "skill"
        assert capability["enabled"] is True

        listed = client.get("/api/v1/capabilities?kind=skill").json()
        assert [item["id"] for item in listed["capabilities"]] == [capability["id"]]

        body = client.get(f"/api/v1/capabilities/{capability['id']}/body").json()
        assert "清单" in body["body"]

        run_id = client.post("/api/v1/runs", json={"task": "实现一个可回滚的数据迁移模块"}).json()[
            "run"
        ]["id"]
        for _ in range(60):
            run = client.get(f"/api/v1/runs/{run_id}").json()["run"]
            if run["status"] == "awaiting_approval":
                break
            import time

            time.sleep(0.1)
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        for _ in range(80):
            run = client.get(f"/api/v1/runs/{run_id}").json()["run"]
            if run["status"] in {"done", "failed", "blocked"}:
                break
            import time

            time.sleep(0.1)

        # 步骤记下了用了哪个技能
        assert any(step["skills_used"] for step in run["steps"]), run["steps"]
        # 执行段真的收到了技能指令
        prompts = [
            item["body"]["messages"][-1]["content"]
            for item in relay.requests
            if "执行工程师" in item["body"]["messages"][0]["content"]
        ]
        assert any("## 可用技能" in text for text in prompts), prompts[:1]


def test_skill_uninstall_removes_files(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        capability = client.post(
            "/api/v1/capabilities/skills/install",
            json={"source": "local", "location": str(BUNDLED_SKILL)},
        ).json()["capability"]
        path = Path(str(capability["meta"]["path"]))
        assert path.is_dir()
        client.delete(f"/api/v1/capabilities/{capability['id']}")
        # 注册表条目没了；文件目录保留（卸载能力不等于删用户数据，可手工清理）
        assert client.get("/api/v1/capabilities").json()["counts"]["skill"] == 0


def test_install_from_zip_url_uses_download_transport(tmp_path: Path):
    """zip 来源：下载 → 解压 → 校验 → 装（测试里注入假 transport，不碰真实网络）。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("skill-x/SKILL.md", GOOD_SKILL)
    payload = buffer.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("skill.zip")
        return httpx.Response(200, content=payload)

    with build_project_client(tmp_path) as client:
        client.app.state.capability_transport = httpx.MockTransport(handler)
        response = client.post(
            "/api/v1/capabilities/skills/install",
            json={"source": "zip", "location": "https://example.test/skill.zip"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["capability"]["name"] == "PDF 处理"


def test_install_rejects_bad_source(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        missing = client.post(
            "/api/v1/capabilities/skills/install",
            json={"source": "local", "location": str(tmp_path / "not-there")},
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "invalid_skill"

        bad_github = client.post(
            "/api/v1/capabilities/skills/install",
            json={"source": "github", "location": "only-owner"},
        )
        assert bad_github.status_code == 400
        assert bad_github.json()["error"]["code"] == "invalid_skill_source"


def test_skill_scope_can_be_limited_to_project(tmp_path: Path):
    with build_project_client(tmp_path) as client:
        client.post("/api/v1/projects", json={"name": "Alpha", "project_id": "alpha"})
        capability = client.post(
            "/api/v1/capabilities/skills/install",
            json={"source": "local", "location": str(BUNDLED_SKILL)},
        ).json()["capability"]

        scoped = client.post(
            f"/api/v1/capabilities/{capability['id']}/scope",
            json={"scope": "project", "project_id": "alpha"},
        ).json()["capability"]
        assert scoped["scope"] == "project"
        assert scoped["project_id"] == "alpha"

        missing_project = client.post(
            f"/api/v1/capabilities/{capability['id']}/scope", json={"scope": "project"}
        )
        assert missing_project.status_code == 400

        globals_again = client.post(
            f"/api/v1/capabilities/{capability['id']}/scope", json={"scope": "global"}
        ).json()["capability"]
        assert globals_again["scope"] == "global"
        assert globals_again["project_id"] == ""
        assert globals_again["enabled"] is True


def test_disabled_skill_is_not_injected(tmp_path: Path):
    capability = Capability(
        id="code-review",
        kind=CapabilityKind.SKILL,
        name="代码审查清单",
        enabled=False,
        meta={"triggers": ["代码"], "path": str(BUNDLED_SKILL)},
    )
    assert select_skills([capability], text="改这段代码") == []
