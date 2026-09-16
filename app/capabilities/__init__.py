"""统一能力层：skill / MCP / （旧）插件的安装、启用、作用域与审计。

分层约定（后续 P1/P2 往上长，不再动导航与设置）::

    app/schemas/capability.py   契约（Capability / ToolCallRequest）
    app/capabilities/registry.py 注册表与审计（唯一读写入口）
    app/capabilities/skills.py   P1：SKILL.md 解析与安装来源
    app/capabilities/mcp.py      P2：MCP 客户端（stdio / Streamable HTTP）
"""

from app.capabilities.registry import CapabilityRegistry

__all__ = ["CapabilityRegistry"]
