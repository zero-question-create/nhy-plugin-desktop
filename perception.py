# plugins/desktop/perception.py
# ========================================================================
# 桌面 ACI（Agent-Computer Interface）感知层。
#
# 目标：不再把整棵 UIA 树原样塞给模型，而是给：
#   - 稳定控件 ID（跨快照尽量不变）
#   - 可操作控件清单（角色/名字/值/动作/位置）
#   - 可见文本摘要
#   - 最近变化
# 同时提供统一的错误分类，让模型知道“是元素失效、窗口不对，还是没加载完”。
# ========================================================================

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_INTERACTIVE_ROLES = {
    "Button", "Edit", "Hyperlink", "ListItem", "ComboBox", "TabItem",
    "MenuItem", "TreeItem", "CheckBox", "RadioButton", "Slider",
    "Spinner", "SplitButton", "ToolBar", "List", "Document",
}
_TEXT_ROLES = {"Text", "Document", "ListItem", "Edit", "Hyperlink"}

_LINE_RE = re.compile(r'^\s*(\S+)\s+(\w+)\s+"(.*?)"(.*)$')
_VALUE_RE = re.compile(r'\bvalue="([^"]*)"')
_ACTIONS_RE = re.compile(r'\bactions=([^\s@]+)')


@dataclass
class ElementRecord:
    sid: str
    key: Tuple
    ref: str = ""
    role: str = ""
    name: str = ""
    value: str = ""
    automation_id: str = ""
    actions: List[str] = field(default_factory=list)
    bounds: Optional[List[int]] = None
    enabled: bool = True
    hwnd: int = 0
    depth: int = 0
    gen: int = 0
    seen_at: float = 0.0

    def interactive(self) -> bool:
        if self.name or self.value:
            return True
        # 空容器只有 expand/scroll 时对模型没有意义，直接折叠。
        if self.role in ("Pane", "Group", "Document", "Edit", "Custom"):
            return bool(set(self.actions) & {"invoke", "select"})
        return bool(set(self.actions) & {"invoke", "setValue", "select", "toggle"})

    def target(self) -> str:
        if self.name:
            return f"{self.role}:{self.name}" if self.role else self.name
        if self.automation_id:
            return f"{self.role}:{self.automation_id}"
        return self.sid


class ElementRegistry:
    """同一任务内维护稳定控件 ID；快照变化时只更新 ref/属性，不轻易换 ID。"""

    def __init__(self):
        self._records: Dict[Tuple, List[ElementRecord]] = {}
        self._by_sid: Dict[str, ElementRecord] = {}
        self._counter = 0
        self._gen = 0

    def update(self, snap: dict) -> List[ElementRecord]:
        self._gen += 1
        hwnd = int(snap.get("hwnd") or 0)
        raw_elements = snap.get("elements")
        if not isinstance(raw_elements, list) or not raw_elements:
            raw_elements = _elements_from_text(snap.get("text") or "", hwnd)
        current: List[ElementRecord] = []
        used: Dict[Tuple, int] = {}
        for raw in raw_elements:
            role = str(raw.get("role") or "")
            name = str(raw.get("name") or "")
            value = str(raw.get("value") or "")
            actions = list(raw.get("actions") or [])
            automation_id = str(raw.get("automation_id") or "")
            if not _relevant(role, name, value, actions):
                continue
            key = (hwnd, role, name, automation_id)
            index = used.get(key, 0)
            used[key] = index + 1
            bucket = self._records.setdefault(key, [])
            if index < len(bucket):
                record = bucket[index]
            else:
                self._counter += 1
                record = ElementRecord(sid=f"e{self._counter}", key=key)
                bucket.append(record)
            record.ref = str(raw.get("ref") or record.ref)
            record.role = role
            record.name = name
            record.value = value
            record.automation_id = automation_id
            record.actions = actions
            record.bounds = raw.get("bounds") or None
            record.enabled = bool(raw.get("enabled", True))
            record.hwnd = hwnd
            record.depth = int(raw.get("depth") or 0)
            record.gen = self._gen
            record.seen_at = time.time()
            self._by_sid[record.sid] = record
            current.append(record)
        return current

    def resolve(self, sid: str) -> Optional[ElementRecord]:
        record = self._by_sid.get(str(sid or ""))
        if record is None or record.gen != self._gen:
            return None
        return record

    def target(self, sid: str, fallback: str = "") -> str:
        record = self.resolve(sid)
        return record.target() if record else fallback


def _relevant(role: str, name: str, value: str, actions: list) -> bool:
    if name or value or actions:
        return True
    return role not in ("Pane", "Group", "TitleBar", "ScrollBar", "Custom", "")


def _elements_from_text(text: str, hwnd: int) -> List[dict]:
    """兼容没有 elements 字段的旧快照/测试快照。"""
    elements = []
    for line in str(text or "").splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        ref, role, name, tail = match.groups()
        value_match = _VALUE_RE.search(tail)
        actions_match = _ACTIONS_RE.search(tail)
        elements.append({
            "ref": ref,
            "role": role,
            "name": name,
            "value": value_match.group(1) if value_match else "",
            "actions": actions_match.group(1).split(",") if actions_match else [],
            "automation_id": "",
            "bounds": None,
            "enabled": "[disabled]" not in tail,
            "hwnd": hwnd,
            "depth": len(line) - len(line.lstrip()),
        })
    return elements


def _bounds_text(bounds) -> str:
    if not bounds or len(bounds) < 4:
        return ""
    return f" @{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}"


def build_digest(snap: dict, registry: ElementRegistry, max_chars: int = 7000) -> str:
    """把快照压成 ACI 摘要：控件索引 + 文本摘要 + 最近变化。"""
    records = registry.update(snap)
    controls = [item for item in records if item.interactive()]
    control_ids = {id(item) for item in controls}
    texts = [
        item for item in records
        if id(item) not in control_ids and (item.name or item.value)
    ]
    controls.sort(key=lambda item: (item.depth, item.sid))
    texts.sort(key=lambda item: (item.depth, item.sid))
    links = [
        item for item in records
        if item.role == "Hyperlink" and (item.name or item.value)
    ]
    links.sort(key=lambda item: (item.depth, item.name))

    lines = [
        f"[窗口] {snap.get('title') or ''} (hwnd={snap.get('hwnd') or 0}, "
        f"nodes={snap.get('nodes') or 0})",
        f"[控件] {len(controls)} 个可操作",
    ]
    for item in controls[:60]:
        tail = ""
        if item.automation_id:
            tail += f' id="{item.automation_id}"'
        if item.value:
            tail += f' value="{item.value[:120]}"'
        if item.actions:
            tail += " actions=" + ",".join(item.actions)
        if item.bounds:
            tail += _bounds_text(item.bounds)
        if not item.enabled:
            tail += " [disabled]"
        lines.append(f"[{item.sid}] {item.role} \"{item.name}\"{tail}")

    if links:
        lines.append("[链接]")
        for item in links[:24]:
            target = item.value[:160] if item.value else ""
            lines.append(f"[{item.sid}] {item.name} — {target}")

    if texts:
        lines.append("[界面文本]")
        for item in texts:
            content = item.name or item.value
            if not content:
                continue
            lines.append(f"[{item.sid}] {item.role}: {content}")

    if not controls and not texts:
        lines.append(
            "[提示] 当前 UIA 树没有可操作控件或文本；不要反复 snapshot，"
            "可考虑 fail，或让用户在电脑插件里开启截图后用视觉兜底。"
        )

    diff = str(snap.get("diff") or "").strip()
    if diff:
        lines.append("[最近变化]")
        lines.append(diff[:1500])

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 40] + "\n…（摘要已截断，可用 snapshot 重新查看）"
    if len(text.strip()) < 40 and snap.get("text"):
        # 极端情况下退化为压缩后的原始树，保证模型不会只看到空摘要。
        raw = str(snap.get("text") or "")
        text = (text + "\n[原始树]\n" + raw)[:max_chars]
    return text


_ERROR_PATTERNS = (
    ("stale_ref", ("引用无效", "引用已过期", "重新 snapshot")),
    ("element_not_found", ("找不到", "不存在", "未找到", "没有找到")),
    ("window_not_found", ("没有目标窗口", "窗口不存在", "窗口已关闭")),
    ("disabled", ("disabled", "不可用", "已禁用", "不能直接", "不支持直接")),
    ("dialog_open", ("弹窗", "确认关闭", "保存提示", "对话框")),
    ("timeout", ("超时", "timeout", "等待", "加载中")),
    ("permission_denied", ("拒绝", "权限", "access denied", "拒绝访问")),
    ("ambiguous_target", ("多个", "歧义", "不唯一", "ambiguous")),
)


def classify_error(value) -> dict:
    """把执行结果/异常归入稳定的错误类型，供模型选择恢复策略。"""
    if isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    elif isinstance(value, dict):
        text = " ".join(
            str(value.get(key) or "")
            for key in ("error", "detail", "error_class", "summary")
        )
        if not text.strip():
            text = str(value)
    else:
        text = str(value or "")
    lowered = text.lower()
    for name, needles in _ERROR_PATTERNS:
        if any(str(needle).lower() in lowered for needle in needles):
            return {"error_class": name, "error_text": text[:300]}
    return {"error_class": "unknown", "error_text": text[:300]}


__all__ = [
    "ElementRecord",
    "ElementRegistry",
    "build_digest",
    "classify_error",
]
