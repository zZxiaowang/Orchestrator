"""端到端自检：创建运行 → 等纲领 → 确认执行 → 校验产物。

用法（需先启动服务，默认 http://127.0.0.1:8787）：

    python -m scripts.smoke_check
    python -m scripts.smoke_check --base http://127.0.0.1:8787 --target-dir D:\\tmp\\demo
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

TERMINAL = {"done", "failed", "cancelled"}


def wait_for(
    client: httpx.Client, base: str, run_id: str, expect: set[str], timeout: float
) -> dict:
    deadline = time.time() + timeout
    run: dict = {}
    while time.time() < deadline:
        run = client.get(f"{base}/api/v1/runs/{run_id}").json()["run"]
        if run["status"] in expect:
            return run
        time.sleep(0.5)
    raise SystemExit(f"超时：期望 {expect}，当前 {run.get('status')}，错误 {run.get('error')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--task", default="为示例项目建立可验证的骨架")
    parser.add_argument("--target-dir", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    with httpx.Client(timeout=60) as client:
        settings = client.get(f"{args.base}/api/v1/settings").json()
        print(
            f"架构段：{settings['architect']['model']} @ {settings['architect']['host'] or '未配置'}"
        )
        print(f"执行段：{settings['editor']['model']} @ {settings['editor']['host'] or '未配置'}")
        if not settings["ready"]:
            print("端点未配置完整，请先在界面或 .env 中填写中转信息。")
            return 2

        created = client.post(
            f"{args.base}/api/v1/runs",
            json={"task": args.task, "target_dir": args.target_dir},
        )
        created.raise_for_status()
        run_id = created.json()["run"]["id"]
        print(f"运行已创建：{run_id}")

        run = wait_for(client, args.base, run_id, {"awaiting_approval", "failed"}, args.timeout)
        if run["status"] == "failed":
            print(f"架构段失败：{run['error']}")
            return 1
        print(f"纲领已生成：{run['plan']['goal']}（{len(run['steps'])} 步）")
        for step in run["steps"]:
            print(f"  {step['id']}. {step['title']}")

        client.post(f"{args.base}/api/v1/runs/{run_id}/approve", json={"feedback": ""})
        run = wait_for(client, args.base, run_id, TERMINAL, args.timeout)
        print(f"执行结束：{run['status']}")
        for step in run["steps"]:
            files = ", ".join(
                f"{f['path']}(+{f['additions']}/-{f['deletions']})" for f in step["files"]
            )
            print(f"  {step['id']}. {step['status']} — {files or '无文件变更'}")

        docs = client.get(f"{args.base}/api/v1/runs/{run_id}/docs").json()["docs"]
        print(f"交付文档：{', '.join(doc['name'] for doc in docs) or '无'}")
        print(f"工作区：{run['workspace_dir']}")
        return 0 if run["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
