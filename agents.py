# plugins/desktop/agents.py
# ========================================================================
# 桌面任务子 Agent 路由：把“聊天读取”“明确网址打开”这类任务交给
# 专用、确定性的处理路径，不再让通用操作手从 UIA 树里硬猜。
# ========================================================================

from __future__ import annotations

import asyncio
import re
import webbrowser
from typing import Optional

from utils.monitor import append_log

_READ_VERBS = ("看看", "看一下", "读一下", "读读", "总结", "有哪些", "说了什么", "未读", "内容")
_SEND_VERBS = ("发给", "发送", "回复", "粘贴", "输入", "打字")
_URL_RE = re.compile(r"https?://[^\s，。；;\"'）)]+")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _running_plugin(plugin_id: str):
    try:
        from plugins import registry

        plugin = registry.get_plugin(plugin_id)
        if plugin is not None and plugin.is_running():
            return plugin
    except Exception:
        pass
    return None


def _chat_target(task: str) -> str:
    """从“看看QQ里夜鹭说了什么”这类句子里提取会话名。"""
    text = str(task or "")
    patterns = (
        r"(?:看|读|打开|切到|进入)\s*([^\s，。；;]{1,20}?)\s*(?:的)?(?:对话|会话|聊天)",
        r"(?:QQ|qq|微信)(?:里|中|上)?(?:的)?\s*([^\s，。；;]{1,20}?)\s*(?:说|聊|发|那边|的)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            name = match.group(1).strip("「」“”\"'")
            if name and name not in ("一下", "那个", "这个", "当前", "现在"):
                return name
    return ""


class ChatAgent:
    agent_id = "chat"
    label = "聊天读取"

    def can_handle(self, task: str) -> bool:
        text = _normalize(task)
        if not text:
            return False
        if "QQ音乐" in text:
            return False
        if not any(key in text for key in ("QQ", "qq", "微信", "聊天", "对话", "会话", "未读")):
            return False
        if not any(verb in text for verb in _READ_VERBS):
            return False
        return not any(verb in text for verb in _SEND_VERBS)

    def run(self, plugin, task: str, context: str = "") -> Optional[dict]:
        qq = _running_plugin("qq_desktop")
        if qq is None or not hasattr(qq, "read_current"):
            return None
        target = _chat_target(task)
        try:
            if target and hasattr(qq, "open_conversation"):
                opened = asyncio.run(qq.open_conversation(target))
                content = str((opened or {}).get("content") or "")
                conversation = str((opened or {}).get("conversation") or target)
                ok = bool((opened or {}).get("ok", True))
            else:
                current = asyncio.run(qq.read_current())
                content = str((current or {}).get("content") or "")
                conversation = str((current or {}).get("conversation") or "")
                ok = bool((current or {}).get("ok"))
        except Exception as exc:
            append_log(f"[电脑/聊天] 读取失败: {exc}")
            return None
        content = content.strip()
        if not content:
            return None
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        try:
            from core.apps import APP_CONTEXT

            for line in lines[:12]:
                APP_CONTEXT.remember(line)
        except Exception:
            pass
        return {
            "ok": ok,
            "summary": content[:4000],
            "content_lines": lines[:12],
            "agent": self.agent_id,
            "conversation": conversation,
            "detail": f"已读取{conversation or '当前会话'}：{content[:200]}",
        }


class UrlOpenAgent:
    agent_id = "url"
    label = "打开网址"

    def can_handle(self, task: str) -> bool:
        text = str(task or "")
        return bool(_URL_RE.search(text)) and any(
            verb in text for verb in ("打开", "访问", "看看", "读一下", "打开网页")
        )

    def run(self, plugin, task: str, context: str = "") -> Optional[dict]:
        match = _URL_RE.search(str(task or ""))
        if not match:
            return None
        url = match.group(0).rstrip("。.,，；;")
        try:
            opened = bool(webbrowser.open(url, new=2))
        except Exception as exc:
            append_log(f"[电脑/网址] 打开失败: {exc}")
            return None
        if not opened:
            return None
        return {
            "ok": True,
            "summary": f"已在浏览器打开 {url}",
            "lines": [],
            "agent": self.agent_id,
        }


AGENTS = (ChatAgent(), UrlOpenAgent())


def try_agents(plugin, task: str, context: str = "") -> Optional[dict]:
    for agent in AGENTS:
        try:
            if not agent.can_handle(task):
                continue
            result = agent.run(plugin, task, context)
            if result is not None:
                append_log(f"[电脑] 子 Agent {agent.label} 接手：{task[:60]}")
                return result
        except Exception as exc:
            append_log(f"[电脑] 子 Agent {agent.label} 异常: {exc}")
    return None


__all__ = ["ChatAgent", "UrlOpenAgent", "try_agents"]
