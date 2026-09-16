"""纲领质量门：执行**之前**检查"这一步到底能不能被判定"。

真实教训来自两次真实运行：

* 20 步的纲领里有几步既没有路径交付物、也没有 ``checks``，执行段只要写一句
  "已完成"就会被当成完成——客观验收形同虚设；
* 另有一整步塞进 8 个交付物，单步产出 42–152KB，没人看得清它到底做完没有。

这里**只告警、不拦截**：结果写进 ``plan.open_questions``，在「架构」模块里直接
显示给用户，同时也会进执行段的上下文。硬拦会把"用户已经确认过的流程"打断，
比告警更糟。
"""

from __future__ import annotations

from app.schemas.plan import ArchitecturePlan, PlanStep
from app.services.verify import derive_checks

#: 一步最多合理的交付物数量：再多就很难在同一轮里做好，也很难验收
MAX_DELIVERABLES_PER_STEP = 6

#: 建议的步数上限（超出只告警；截断会丢内容，那是更糟的结果）
MAX_RECOMMENDED_STEPS = 8

#: 一眼看去就是"没办法验证"的验收措辞
_VAGUE_WORDS = ("质量", "尽量", "合理", "美观", "完善", "优化一下", "良好", "清晰")


def review(plan: ArchitecturePlan) -> list[str]:
    """返回告警列表；空列表 = 质量门通过。"""

    warnings: list[str] = []
    if len(plan.steps) > MAX_RECOMMENDED_STEPS:
        warnings.append(
            f"纲领共 {len(plan.steps)} 步，超过建议的 {MAX_RECOMMENDED_STEPS} 步："
            "单步产出容易失控，也不方便中途验收；建议把同类步骤合并。"
        )
    for step in plan.steps:
        warnings.extend(_review_step(step))
    return warnings


def _review_step(step: PlanStep) -> list[str]:
    out: list[str] = []
    label = f"第 {step.id} 步「{step.title or step.goal or '未命名'}」"

    path_deliverables = derive_checks(step.deliverables)
    if len(step.deliverables) > MAX_DELIVERABLES_PER_STEP:
        out.append(
            f"{label}：交付物有 {len(step.deliverables)} 个，超过建议的"
            f" {MAX_DELIVERABLES_PER_STEP} 个；一步之内很难都做好，考虑拆成两步。"
        )
    if not path_deliverables and not step.checks:
        out.append(
            f"{label}：既没有路径形态的交付物，也没有 checks，系统无法客观判定"
            "这一步是否完成。请补一条 file_exists / file_contains / py_compile 之类的检查，"
            "或把交付物写成具体文件路径。"
        )
    if not step.acceptance:
        out.append(f"{label}：没有写验收条件（acceptance），执行段只能凭感觉判断做没做完。")
    elif all(any(word in item for word in _VAGUE_WORDS) for item in step.acceptance):
        out.append(
            f"{label}：验收条件都是「{'/'.join(_VAGUE_WORDS[:3])}」这类无法验证的说法，"
            "请改成可判定的描述（例如「文件 X 里出现 Y」）。"
        )
    return out
