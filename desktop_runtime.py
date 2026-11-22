# plugins/desktop/desktop_runtime.py
# ========================================================================
# 桌面常驻运行时：空闲只观察，目标到达才执行。
#
# 它不是“第二个意识”：不能自己创建目标、不能说话、不能写长期记忆。
# 认知层通过 perform_task() 提交一个带 deadline/步数预算的目标，运行时
# 在单一线程里完成；空闲时只刷新桌面概览，不调用任何模型。
# ========================================================================

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import uia
from utils.monitor import append_log

_OPEN_RE = re.compile(
    r"^(?:请|帮我|麻烦|给我)?\s*(?:打开|启动|运行|开一下|开个)\s*[「“\"']?(.+?)[」”\"']?\s*$"
)
_CLOSE_RE = re.compile(
    r"^(?:请|帮我|麻烦|给我)?\s*(?:关闭|关掉|退出|结束)\s*[「“\"']?(.+?)[」”\"']?\s*$"
)
_DESKTOP_OPEN_RE = re.compile(
    r"^(?:请|帮我|麻烦|给我)?\s*打开\s*桌面(?:上|里)的\s*[「“\"']?(.+?)[」”\"']?\s*$"
)
_COMPLEX_RE = re.compile(
    r"(并|然后|接着|之后|输入|写|搜索|发给|告诉|查看|看看|点|选择|登录|下载|复制|粘贴|切到|会话|聊天|进程|任务管理器)"
)


@dataclass
class DesktopGoal:
    goal_id: str
    text: str
    context: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "queued"       # queued / running / done / failed / cancelled
    cancel: threading.Event = field(default_factory=threading.Event)
    result: dict = field(default_factory=dict)


def _clean_name(text: str) -> str:
    name = str(text or "").strip().strip("「」“”\"'。.!！?？,，;；")
    name = re.sub(r"^(一下|那个|这个|我的)\s*", "", name)
    return name.strip()


def _find_window(plugin, name: str, prefer: str = "") -> int:
    """按程序名/窗口标题找现有窗口；只用于“打开/关闭”这类快路径。"""
    if not name:
        return 0
    try:
        hwnd = int(plugin._find_app_window(name, prefer) or 0)
        if hwnd:
            return hwnd
    except Exception:
        pass
    needle = name.lower()
    try:
        for item in uia.list_windows(only_titled=True):
            title = str(item.get("title") or "")
            if needle and needle in title.lower():
                return int(item["hwnd"])
    except Exception:
        pass
    return 0


def _open_desktop_icon(plugin, name: str) -> Optional[dict]:
    icons = uia.desktop_icons(limit=120)
    target = next(
        (item for item in icons if name.lower() in str(item.get("name") or "").lower()),
        None,
    )
    if target is None:
        return None
    before = {int(item["hwnd"]) for item in uia.list_windows(only_titled=True)}
    result = uia.backend().click(target["ref"])
    if not result.get("ok"):
        return None
    from .desktop_operator import _find_new_window

    new_hwnd = _find_new_window(0, before, prefer=name, timeout=4.0)
    if not new_hwnd:
        foreground = uia.foreground_window()
        if foreground and int(foreground) not in before:
            new_hwnd = int(foreground)
    if not new_hwnd:
        return {
            "ok": False,
            "summary": f"点了桌面上的{name}，但没有看到新窗口",
            "lines": [],
        }
    return {
        "ok": True,
        "summary": f"从桌面图标打开了{name}",
        "hwnd": int(new_hwnd),
        "lines": [],
    }


def try_fast_task(plugin, task: str, context: str = "") -> Optional[dict]:
    """常用单步任务走确定性路径；不适用时返回 None，交给操作手。

    只处理“打开/关闭某个程序”“打开桌面上的某个图标”这类一句话任务，
    带“然后/输入/搜索/发给/切到会话”等后续动作的仍走完整任务循环。
    """
    text = str(task or "").strip()
    if not text or _COMPLEX_RE.search(text):
        return None

    desktop_match = _DESKTOP_OPEN_RE.match(text)
    if desktop_match:
        name = _clean_name(desktop_match.group(1))
        if name:
            result = _open_desktop_icon(plugin, name)
            if result is not None:
                return result
            return {
                "ok": False,
                "summary": f"桌面上没有找到{name}的图标，或者图标点不开",
                "lines": [],
            }

    open_match = _OPEN_RE.match(text)
    if open_match:
        name = _clean_name(open_match.group(1))
        if name and not any(key in name for key in ("电脑", "屏幕", "系统", "关机")):
            existing = _find_window(plugin, name, f"{task} {context}")
            if existing:
                try:
                    from plugins.desktop import input as desktop_input

                    desktop_input.focus_window(existing)
                except Exception:
                    pass
                return {
                    "ok": True,
                    "summary": f"{name} 已经打开，我直接切到它",
                    "hwnd": existing,
                    "lines": [],
                }
            before = {int(item["hwnd"]) for item in uia.list_windows(only_titled=True)}
            launched = uia.backend().launch_app(name)
            if launched.get("ok"):
                from .desktop_operator import _find_new_window

                hwnd = _find_new_window(
                    int(launched.get("pid") or 0), before,
                    prefer=name, timeout=5.0,
                )
                if hwnd:
                    return {
                        "ok": True,
                        "summary": f"我打开了{name}",
                        "hwnd": int(hwnd),
                        "lines": [],
                    }
                return {
                    "ok": False,
                    "summary": f"启动了{name}，但没找到它的窗口",
                    "lines": [],
                }
            return {
                "ok": False,
                "summary": f"打不开{name}：{launched.get('error') or '未知原因'}",
                "lines": [],
            }

    close_match = _CLOSE_RE.match(text)
    if close_match:
        name = _clean_name(close_match.group(1))
        if name and not any(key in name for key in ("电脑", "屏幕", "系统", "关机")):
            hwnd = _find_window(plugin, name, f"{task} {context}")
            if not hwnd:
                return {
                    "ok": False,
                    "summary": f"没有找到{name}的窗口",
                    "lines": [],
                }
            result = uia.backend().close_window(hwnd)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "summary": f"关闭{name}失败：{result.get('error') or '未知原因'}",
                    "lines": [],
                }
            deadline = time.time() + 4.0
            while time.time() < deadline and uia.window_exists(hwnd):
                time.sleep(0.2)
            if uia.window_exists(hwnd):
                return {
                    "ok": False,
                    "summary": f"已发出关闭请求，但{name}的窗口还在",
                    "lines": [],
                }
            return {"ok": True, "summary": f"我关闭了{name}", "lines": []}

    return None


class DesktopRuntime:
    """单线程常驻执行器：空闲观察，目标到达后串行执行。"""

    def __init__(self, plugin, idle_interval: float = 5.0):
        self.plugin = plugin
        self.idle_interval = max(1.0, float(idle_interval))
        self._queue: "queue.Queue[Optional[DesktopGoal]]" = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._state = "idle"
        self._active: Optional[DesktopGoal] = None
        self._last_idle = 0.0
        self._idle_ticks = 0
        self._seq = 0
        self._last_error = ""

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="DesktopRuntime", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.cancel("运行时停止")
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None
        with self._lock:
            self._state = "stopped"
            self._active = None

    def submit(self, task: str, context: str = "") -> dict:
        task = str(task or "").strip()
        if not task:
            return {"ok": False, "detail": "没有要交办的事"}
        if self._stop.is_set():
            return {"ok": False, "detail": "电脑运行时已经停止"}
        with self._lock:
            self._seq += 1
            goal = DesktopGoal(
                goal_id=f"desk_{int(time.time() * 1000)}_{self._seq}",
                text=task,
                context=str(context or ""),
            )
            try:
                self._queue.put_nowait(goal)
            except queue.Full:
                return {"ok": False, "detail": "电脑任务队列已满"}
            if self._active is None:
                self._state = "queued"
            return {
                "ok": True,
                "detail": f"我坐到电脑前，准备{task}",
                "content_lines": [],
                "goal_id": goal.goal_id,
            }

    def cancel(self, reason: str = "") -> bool:
        with self._lock:
            goal = self._active
            if goal is None:
                return False
            goal.cancel.set()
            goal.status = "cancelling"
        append_log(f"[电脑] 取消当前目标：{reason or '收到取消请求'}")
        return True

    def status(self) -> dict:
        with self._lock:
            goal = self._active
            valve = {}
            if goal is not None:
                try:
                    from core.action_valve import ACTION_VALVE

                    valve = ACTION_VALVE.status(goal.goal_id)
                except Exception:
                    valve = {}
            return {
                "state": self._state,
                "goal_id": goal.goal_id if goal else "",
                "goal": goal.text[:120] if goal else "",
                "goal_status": goal.status if goal else "",
                "queued": self._queue.qsize(),
                "idle_ticks": self._idle_ticks,
                "last_error": self._last_error,
                "valve": valve,
            }

    # ---------- 线程 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                goal = self._queue.get(timeout=min(1.0, self.idle_interval))
            except queue.Empty:
                self._idle_tick()
                continue
            if goal is None:
                break
            if self._stop.is_set():
                goal.status = "cancelled"
                break
            self._run_goal(goal)

    def _idle_tick(self) -> None:
        now = time.time()
        if now - self._last_idle < self.idle_interval:
            return
        self._last_idle = now
        self._idle_ticks += 1
        with self._lock:
            if self._active is None:
                self._state = "idle"
        if not getattr(self.plugin, "_running", False):
            return
        try:
            # 只刷新概览（窗口标题 + 桌面图标），不调用模型。
            self.plugin._push_overview()
        except Exception:
            pass
        try:
            from core.apps import APP_CONTEXT

            with self._lock:
                state = self._state
                goal = self._active
            APP_CONTEXT.remember_state(
                "desktop_runtime",
                "电脑运行时：" + state
                + (f"，正在做：{goal.text[:60]}" if goal else "，空闲"),
            )
        except Exception:
            pass

    def _run_goal(self, goal: DesktopGoal) -> None:
        with self._lock:
            self._active = goal
            self._state = "executing"
            goal.status = "running"
        self.plugin._mark_goal_running(goal.goal_id)
        try:
            result = self.plugin._run_task(
                goal.text, goal.context,
                goal_id=goal.goal_id, cancel_event=goal.cancel,
            )
            if not isinstance(result, dict):
                result = {"ok": False, "summary": "电脑任务没有返回结果", "lines": []}
        except Exception as exc:
            import traceback

            append_log(f"[电脑] 常驻任务异常 {traceback.format_exc()}")
            result = {"ok": False, "summary": f"电脑这边出错了：{exc}", "lines": []}
        goal.result = result
        if goal.cancel.is_set():
            goal.status = "cancelled"
        else:
            goal.status = "done" if result.get("ok") else "failed"
            if not result.get("ok"):
                self._last_error = str(result.get("summary") or "")
        with self._lock:
            self._active = None
            self._state = "idle"
        self.plugin._mark_goal_finished(goal.goal_id, result, cancelled=goal.cancel.is_set())
