# plugins/desktop/planner.py
# ========================================================================
# 桌面任务规划器：把一句任务拆成 3~6 个高层步骤 + 验收条件。
#
# 只做“做什么、按什么顺序”，不产出 UIA id/坐标。具体怎么点由 Executor
# 每一步看当前界面决定，避免一次性计划里的控件引用过期。
# ========================================================================

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from core.llm_interface import call_api_thinking
from utils.monitor import append_log
from .task_memory import find_plan

_PLAN_SYSTEM = """你是桌面任务规划器，只负责把任务拆成高层步骤，不负责点具体控件。
输出一个 JSON，不要解释：
{"acceptance":["可验证的完成条件"],"steps":[{"text":"一步做什么","acceptance":"这一步完成后应看到什么"}]}
要求：
1. 最多 6 步，能 1~2 步完成就不要硬拆；
2. 步骤用自然语言写“打开/切换/输入/点击/确认”，不要写 UIA id、坐标或具体 ref；
3. 每步验收条件必须是界面上能观察到的事实；
4. 不确定的步骤写“根据界面确认”，不要编造。
"""

_JSON_RE = re.compile(r"\{[\s\S]*\}")
_MARKERS = (
    "并", "然后", "接着", "先", "再", "之后", "输入", "写", "搜索", "发给",
    "告诉", "查看", "看看", "整理", "切换", "打开", "关闭", "登录", "下载",
)


@dataclass
class PlanStep:
    text: str
    acceptance: str = ""
    status: str = "pending"       # pending / active / done / failed


@dataclass
class TaskPlan:
    goal: str
    steps: List[PlanStep] = field(default_factory=list)
    acceptance: List[str] = field(default_factory=list)
    current: int = 0
    attempts: int = 0

    def __post_init__(self):
        if self.steps:
            self.steps[0].status = "active"

    def found(self) -> bool:
        return bool(self.steps)

    def prompt_text(self) -> str:
        if not self.steps:
            return ""
        lines = [f"【计划】{self.goal}"]
        for index, step in enumerate(self.steps, 1):
            mark = {"pending": " ", "active": ">", "done": "x", "failed": "!"}.get(
                step.status, " "
            )
            line = f"{index}. [{mark}] {step.text}"
            if step.status == "active" and step.acceptance:
                line += f"（验收：{step.acceptance}）"
            lines.append(line)
        if self.acceptance:
            lines.append("【验收】" + "；".join(self.acceptance[:4]))
        return "\n".join(lines)

    def advance(self, action: str, result: dict, todo_done: bool = False) -> None:
        """成功执行一个里程碑动作后推进 Todo；模型显式 todo_done 时也推进。"""
        if not result.get("ok") or self.current >= len(self.steps):
            return
        milestones = {
            "launch", "open_url", "open_conversation", "close_window",
            "close_app", "send", "send_clipboard", "set_value", "type_text",
        }
        kind = str(action or "").lower()
        if not todo_done and kind not in milestones:
            return
        self.steps[self.current].status = "done"
        self.current += 1
        if self.current < len(self.steps):
            self.steps[self.current].status = "active"

    def mark_failed(self) -> None:
        if self.current < len(self.steps):
            self.steps[self.current].status = "failed"

    def completed(self) -> bool:
        return bool(self.steps) and self.current >= len(self.steps)

    def summary(self) -> dict:
        return {
            "goal": self.goal,
            "current": self.current,
            "acceptance": list(self.acceptance),
            "steps": [
                {
                    "text": step.text,
                    "acceptance": step.acceptance,
                    "status": step.status,
                }
                for step in self.steps
            ],
        }


def should_plan(task: str, context: str = "") -> bool:
    """短任务/单步任务不规划，避免多花一次模型调用。"""
    text = f"{task} {context}".strip()
    if len(text) < 16:
        return False
    return any(marker in text for marker in _MARKERS)


def _parse_plan(raw: str) -> Optional[dict]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```").removeprefix("json").removesuffix("```").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def plan_task(task: str, context: str = "", profile=None,
              max_steps: int = 6) -> Optional[TaskPlan]:
    """调用一次轻量规划模型；失败返回 None，调用方继续 ReAct。"""
    if not should_plan(task, context):
        return None
    cached = find_plan(task)
    if cached:
        steps = [
            PlanStep(
                text=str(item.get("text") or "").strip(),
                acceptance=str(item.get("acceptance") or "").strip(),
                status=str(item.get("status") or "pending"),
            )
            for item in (cached.get("steps") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if steps:
            plan = TaskPlan(
                goal=task, steps=steps,
                acceptance=[str(item) for item in (cached.get("acceptance") or [])],
            )
            # 记忆是“计划模板”，不是上次的进度：新任务从第一步重新走。
            plan.current = 0
            for index, step in enumerate(plan.steps):
                step.status = "active" if index == 0 else "pending"
            append_log(f"[电脑] 复用相似任务计划：{task[:60]}")
            return plan
    messages = [
        {"role": "system", "content": _PLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                f"【任务】{task}\n"
                f"【背景】{context or '没有额外背景'}\n"
                "请给出高层计划和验收条件。"
            ),
        },
    ]
    try:
        raw = call_api_thinking(
            messages, max_tokens=500, thinking=False, profile=profile
        )
    except Exception as exc:
        append_log(f"[电脑] 规划调用失败，继续逐步操作: {exc}")
        return None
    data = _parse_plan(raw)
    if not data:
        append_log("[电脑] 规划返回无法解析，继续逐步操作")
        return None
    steps = []
    for item in (data.get("steps") or [])[: max(1, int(max_steps))]:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            acceptance = str(item.get("acceptance") or "").strip()
        else:
            text = str(item or "").strip()
            acceptance = ""
        if text:
            steps.append(PlanStep(text=text, acceptance=acceptance))
    acceptance = [
        str(item).strip() for item in (data.get("acceptance") or [])
        if str(item).strip()
    ]
    if not steps:
        return None
    return TaskPlan(goal=task, steps=steps, acceptance=acceptance)


__all__ = ["PlanStep", "TaskPlan", "should_plan", "plan_task"]
