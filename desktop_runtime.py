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

from config.api_config import config
from core.repetition import similarity
from . import uia
from .trace import TRACE
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
    step_window: int = 10
    deadline: float = 180.0
    acceptance: list = field(default_factory=list)
    source_turn: str = ""
    steps: int = 0
    window_reported: bool = False
    last_receipt_at: float = 0.0
    last_receipt: dict = field(default_factory=dict)
    cancel_reason: str = ""


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
    from .agents import try_agents

    agent_result = try_agents(plugin, task, context)
    if agent_result is not None:
        return agent_result

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
        self._last_receipt: dict = {}

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

    @staticmethod
    def _normalize_task(task: str) -> str:
        return re.sub(r"\s+", "", str(task or "")).lower()

    def _receipt(self, goal: DesktopGoal, state: str = "", summary: str = "",
                 verified: bool = False) -> dict:
        state = str(state or goal.status or "unknown")
        terminal = state in ("done", "failed", "cancelled", "superseded")
        return {
            "goal_id": goal.goal_id,
            "state": state,
            "task": goal.text,
            "step": int(goal.steps),
            "step_window": int(goal.step_window),
            "elapsed": round(time.time() - goal.created_at, 2),
            "can_stop": not terminal,
            "verified": bool(verified),
            "summary": str(summary or "")[:300],
            "updated_at": round(time.time(), 3),
        }

    def _emit_receipt(self, goal: DesktopGoal, state: str = "", summary: str = "",
                      verified: bool = False, force: bool = False) -> dict:
        receipt = self._receipt(goal, state, summary, verified)
        now = time.time()
        interval = 4.0
        try:
            interval = max(0.5, float(config.get("desktop_receipt_interval", 4.0)))
        except (TypeError, ValueError):
            pass
        terminal = receipt["state"] in ("done", "failed", "cancelled", "superseded")
        if not force and not terminal and now - goal.last_receipt_at < interval:
            return receipt
        goal.last_receipt_at = now
        goal.last_receipt = receipt
        with self._lock:
            self._last_receipt = receipt
        try:
            from core.cognition import notify_action_receipt

            notify_action_receipt(receipt)
        except Exception:
            pass
        TRACE.event("receipt", **receipt)
        return receipt

    def current_receipt(self) -> dict:
        with self._lock:
            goal = self._active
            if goal is not None and goal.last_receipt:
                return dict(goal.last_receipt)
            return dict(self._last_receipt)

    def submit(self, task: str, context: str = "", acceptance=None,
               step_window: int = None, deadline: float = None,
               source_turn: str = "", preempt: bool = False) -> dict:
        task = str(task or "").strip()
        if not task:
            return {"ok": False, "detail": "没有要交办的事"}
        if self._stop.is_set():
            return {"ok": False, "detail": "电脑运行时已经停止"}
        try:
            window = max(1, int(step_window if step_window is not None else config.get("desktop_step_window", 10)))
        except (TypeError, ValueError):
            window = 6
        try:
            total_deadline = max(
                15.0,
                float(deadline if deadline is not None else config.get("desktop_task_timeout", 180)),
            )
        except (TypeError, ValueError):
            total_deadline = 180.0

        with self._lock:
            active = self._active
            if active is not None:
                same = (
                    self._normalize_task(task) == self._normalize_task(active.text)
                    or similarity(task, active.text) >= 0.82
                )
                receipt = self._receipt(active, "running" if not active.cancel.is_set() else "cancelling")
                if same:
                    return {
                        "ok": True,
                        "busy": True,
                        "duplicate": True,
                        "detail": f"电脑已经在做这件事：{active.text[:80]}",
                        "goal_id": active.goal_id,
                        "receipt": receipt,
                    }
                if not preempt:
                    return {
                        "ok": False,
                        "busy": True,
                        "duplicate": False,
                        "detail": "电脑正在做另一件事；先停止当前动作，再交新任务。",
                        "goal_id": active.goal_id,
                        "receipt": receipt,
                    }
                active.cancel_reason = "被新目标替代"
                active.cancel.set()
                active.status = "superseded"

            self._seq += 1
            goal = DesktopGoal(
                goal_id=f"desk_{int(time.time() * 1000)}_{self._seq}",
                text=task,
                context=str(context or ""),
                acceptance=list(acceptance or []),
                step_window=window,
                deadline=total_deadline,
                source_turn=str(source_turn or ""),
            )
            try:
                self._queue.put_nowait(goal)
            except queue.Full:
                return {"ok": False, "detail": "电脑任务队列已满"}
            if self._active is None:
                self._state = "queued"
            receipt = self._receipt(goal, "accepted")
            goal.last_receipt = receipt
            self._last_receipt = receipt
        self._emit_receipt(goal, "accepted", force=True)
        return {
            "ok": True,
            "busy": False,
            "detail": f"我坐到电脑前，准备{task}",
            "content_lines": [],
            "goal_id": goal.goal_id,
            "receipt": receipt,
        }

    def cancel(self, reason: str = "") -> bool:
        reason = str(reason or "收到取消请求")
        with self._lock:
            goal = self._active
            if goal is not None:
                goal.cancel_reason = reason
                goal.cancel.set()
                goal.status = "cancelling"
        if goal is not None:
            append_log(f"[电脑] 取消当前目标：{goal.cancel_reason}")
            self._emit_receipt(goal, "cancelling", summary=goal.cancel_reason, force=True)

        # 队列里还没执行的目标也直接取消；否则“停止”之后它还会冒出来跑。
        queued_cancelled = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                # 保留 stop() 的终止哨兵。
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    pass
                break
            item.cancel_reason = reason
            item.cancel.set()
            item.status = "cancelled"
            queued_cancelled.append(item)
        for item in queued_cancelled:
            self._emit_receipt(item, "cancelled", summary=reason, force=True)
        if goal is None:
            with self._lock:
                if self._active is None:
                    self._state = "idle"
        return bool(goal is not None or queued_cancelled)

    def cancel_current_task(self, reason: str = "") -> dict:
        cancelled = self.cancel(reason)
        return {
            "ok": True,
            "cancelled": cancelled,
            "receipt": self.current_receipt(),
            "detail": "已请求停止当前电脑任务" if cancelled else "当前没有正在执行的电脑任务",
        }

    def wait_idle(self, timeout: float = 6.0) -> bool:
        """等当前目标真正退出（取消也要等它走到步骤边界）。

        取消不是强杀：UIA/模型调用可能还在返回。调用方在“停止后立刻交新任务”
        时先等这一小段，避免新目标被“电脑正在做另一件事”直接拒掉。
        """
        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            with self._lock:
                if self._active is None:
                    return True
            time.sleep(0.05)
        with self._lock:
            return self._active is None

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
                "receipt": self.current_receipt(),
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
            # 先登记为 active，再进 _run_goal；否则 cancel() 在
            # “刚取出队列、还没设置 active”的窗口里会以为没有任务可取消。
            with self._lock:
                self._active = goal
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
        started = time.time()
        with self._lock:
            self._active = goal
            self._state = "executing"
            goal.status = "running"
        TRACE.event(
            "runtime_goal_start", goal_id=goal.goal_id,
            task=goal.text, source="runtime",
        )
        self._emit_receipt(goal, "running", force=True)
        self.plugin._mark_goal_running(goal.goal_id)

        def on_progress(step: int, action: str, result: dict = None,
                        still_trying: bool = False, summary: str = "") -> None:
            goal.steps = max(goal.steps, int(step or 0))
            if still_trying:
                goal.window_reported = True
                self._emit_receipt(
                    goal, "still_trying",
                    summary=summary or "超出软步数窗口，仍在尝试；可以随时停止。",
                    force=True,
                )
                return
            self._emit_receipt(
                goal, "running", summary=summary or (f"第 {goal.steps} 步：{action}" if action else ""),
            )

        try:
            result = self.plugin._run_task(
                goal.text, goal.context,
                goal_id=goal.goal_id, cancel_event=goal.cancel,
                goal=goal, progress_cb=on_progress,
            )
            if not isinstance(result, dict):
                result = {"ok": False, "summary": "电脑任务没有返回结果", "lines": []}
        except Exception as exc:
            import traceback

            append_log(f"[电脑] 常驻任务异常 {traceback.format_exc()}")
            result = {"ok": False, "summary": f"电脑这边出错了：{exc}", "lines": []}
        goal.result = result
        cancelled = bool(result.get("cancelled")) or bool(
            goal.cancel.is_set() and not result.get("ok")
        )
        if goal.status == "superseded":
            final_state = "superseded"
            final_summary = goal.cancel_reason or "被新目标替代"
        elif cancelled:
            goal.status = "cancelled"
            final_state = "cancelled"
            final_summary = goal.cancel_reason or str(result.get("summary") or "任务已取消")
        elif result.get("pending") or result.get("still_trying"):
            goal.status = "still_trying"
            final_state = "still_trying"
            final_summary = str(result.get("summary") or "还在尝试，可随时停止")
        elif result.get("ok"):
            goal.status = "done"
            final_state = "done"
            final_summary = str(result.get("summary") or "完成")
            self._last_error = ""
        else:
            goal.status = "failed"
            final_state = "failed"
            final_summary = str(result.get("summary") or "失败")
            self._last_error = final_summary
        TRACE.event(
            "runtime_goal_end", goal_id=goal.goal_id,
            ok=bool(result.get("ok")), cancelled=cancelled,
            summary=final_summary,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        # 先写最终回执并唤醒认知循环，再把运行时置回 idle。
        self._emit_receipt(
            goal, final_state, summary=final_summary,
            verified=bool(result.get("verified")), force=True,
        )
        # 先把插件侧任务状态收尾，再把运行时置回 idle。
        # 否则“等 idle 后立刻交新任务”会看到旧的 running 状态而被拒。
        self.plugin._mark_goal_finished(goal.goal_id, result, cancelled=cancelled)
        with self._lock:
            self._active = None
            self._state = "idle"
