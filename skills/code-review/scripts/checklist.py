"""示例脚本：skill 可以带脚本，但**默认不会被执行**。

要跑它，得在设置里开启「允许执行验证命令」并把命令加进白名单
（例如 `python skills/code-review/scripts/checklist.py`），或由执行段在命令里显式提出。
"""

from __future__ import annotations

CHECKLIST = (
    "边界（空/超长/缺字段）",
    "错误处理（可操作）",
    "可测性（纯函数/可注入）",
    "命名与注释",
    "改动面",
    "回归测试",
)


def main() -> None:
    print("代码审查清单：")
    for index, item in enumerate(CHECKLIST, start=1):
        print(f"  {index}. {item}")


if __name__ == "__main__":
    main()
