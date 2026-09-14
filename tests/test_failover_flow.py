"""主备自动切换：主用 502（Cloudflare 回源失败）时切到备用继续跑。"""

from __future__ import annotations

from pathlib import Path

import httpx

from tests.conftest import FakeRelay
from tests.test_api_flow import TERMINAL, build_client, wait_for_status


class PrimaryDownRelay(FakeRelay):
    """主用网关固定返回 502（和你遇到的 bad_response_status_code 一样）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.primary_calls = 0
        self.backup_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host.startswith("primary"):
            self.primary_calls += 1
            return httpx.Response(
                502,
                json={
                    "error": {
                        "message": "The origin web server returned an invalid or incomplete "
                        "response to Cloudflare.",
                        "type": "bad_response_status_code",
                    }
                },
            )
        self.backup_calls += 1
        return super().handler(request)


def _backup_overrides() -> dict:
    return {
        "relay_base_url": "https://primary.test/v1",
        "architect_backup_base_url": "https://backup.test/v1",
        "architect_backup_api_key": "sk-backup-architect",
        "editor_backup_base_url": "https://backup.test/v1",
        "editor_backup_api_key": "sk-backup-editor",
        "architect_backup_label": "备用中转",
    }


def test_run_switches_to_backup_when_primary_is_502(tmp_path: Path):
    relay = PrimaryDownRelay()
    with build_client(tmp_path, relay, settings_overrides=_backup_overrides()) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        wait_for_status(client, run_id, {"awaiting_approval"})
        client.post(f"/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        # 主用要先把 5xx 重试耗尽（每次带退避），所以这里给足时间
        run = wait_for_status(client, run_id, TERMINAL, timeout=60)

    assert run["status"] == "done", run.get("error")
    assert relay.primary_calls > 0, "应当先试过主用"
    assert relay.backup_calls > 0, "主用 502 后应当切到备用"

    # 指标里要留下"实际用的是哪套配置"：备用生效时 alias 应当不是主用名字
    architect = [item for item in run["metrics"] if item["phase"] == "architect"]
    assert architect, run
    assert "备用" in architect[0]["route"]["alias"]

    executor = [item for item in run["metrics"] if item["phase"] == "executor"]
    assert executor and all("备用" in item["route"]["alias"] for item in executor)


def test_no_backup_configured_keeps_5xx_error_readable(tmp_path: Path):
    """没配备用配置时：如实报错，并带上"换个配置/稍后重试"的可读提示。"""

    relay = PrimaryDownRelay()
    overrides = {"relay_base_url": "https://primary.test/v1"}
    with build_client(tmp_path, relay, settings_overrides=overrides) as client:
        run_id = client.post("/api/v1/runs", json={"task": "为示例项目建立骨架"}).json()["run"][
            "id"
        ]
        run = wait_for_status(client, run_id, {"failed"}, timeout=30)

    assert run["status"] == "failed"
    message = run["error"]["message"]
    assert "502" in message
    # 提示要指向"可操作"的动作，而不是把 Cloudflare 原文丢给用户
    hint = run["error"].get("hint", "")
    assert hint, run["error"]
    assert "备用" in hint or "重试" in hint or "切换" in hint
