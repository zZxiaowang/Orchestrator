"""别名入口（复数命名）：用例主体在 ``tests/test_project_boundary.py``。

保留本文件是为了让不同命名约定下的测试发现都能命中同一组边界断言。
"""

from __future__ import annotations

try:  # 默认：pytest 会把 tests 目录加入 sys.path
    from test_project_boundary import *  # noqa: F401,F403
except ImportError:  # pragma: no cover - tests 被当作包时的回退
    from tests.test_project_boundary import *  # noqa: F401,F403
