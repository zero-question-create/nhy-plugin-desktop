# plugins/desktop/verifier.py
# ========================================================================
# 桌面动作/任务验收：不让执行模型自己宣布完成。
#
# 能确定性检查的就确定性检查；检查不了的标为 unverified，由下一步状态
# 指纹或动作阀继续判断，不强行给假结论。
# ========================================================================

from __future__ import annotations

import re
import time

from . import uia


def _window_alive(hwnd: int) -> bool:
    try:
        return bool(hwnd and uia.window_exists(hwnd))
    except Exception:
        return False


def verify_action(action: dict, result: dict, backend, registry=None) -> dict:
    """检查一步动作的直接后置条件。"""
    kind = str(action.get("action") or "").lower()
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "verified": False, "reason": "动作本身失败"}

    if kind in ("close_window", "close_app"):
        hwnd = int(result.get("hwnd") or 0)
        if not hwnd:
            sid = str(action.get("id") or action.get("sid") or "")
            record = registry.resolve(sid) if (registry is not None and sid) else None
            hwnd = int(record.hwnd) if record else 0
        if hwnd:
            deadline = time.time() + 1.2
            while time.time() < deadline and _window_alive(hwnd):
                time.sleep(0.1)
            if _window_alive(hwnd):
                return {
                    "ok": False, "verified": True,
                    "reason": "窗口还没有关闭",
                    "error_class": "close_failed",
                }
        return {"ok": True, "verified": True, "reason": "窗口已关闭"}

    if kind in ("launch", "open_url"):
        hwnd = int(result.get("hwnd") or 0)
        if hwnd:
            if _window_alive(hwnd):
                return {"ok": True, "verified": True, "reason": "目标窗口已出现"}
            return {
                "ok": False, "verified": True,
                "reason": "动作返回了窗口，但窗口不存在",
                "error_class": "window_not_found",
            }
        return {"ok": True, "verified": False, "reason": "没有返回窗口，等待下一步确认"}

    if kind in ("set_value",):
        ref = str(action.get("ref") or "")
        if not ref and registry is not None:
            sid = str(action.get("id") or action.get("sid") or "")
            record = registry.resolve(sid) if sid else None
            ref = record.ref if record else ""
        expected = str(action.get("text") or "")
        if ref and hasattr(backend, "element_value"):
            current = backend.element_value(ref)
            if current.get("ok"):
                actual = str(current.get("value") or "")
                if actual == expected:
                    return {"ok": True, "verified": True, "reason": "输入框值已写入"}
                return {
                    "ok": False, "verified": True,
                    "reason": f"输入框值没有写入（当前 {actual[:40]!r}）",
                    "error_class": "value_not_written",
                }
        return {"ok": True, "verified": False, "reason": "无法回读输入框，等待下一步确认"}

    # click / set_value / key / scroll 等：直接结果只能证明“动作被接受”，
    # 是否真的生效由下一步状态指纹和动作阀确认。
    return {"ok": True, "verified": False, "reason": "等待下一步状态变化"}


def _extract_target(task: str, verbs: tuple) -> str:
    pattern = r"(?:" + "|".join(verbs) + r")\s*[「“\"]?(.+?)[」”\"]?\s*$"
    match = re.search(pattern, str(task or "").strip())
    if not match:
        return ""
    text = match.group(1).strip(" 。.!！?？,，;；")
    if any(marker in text for marker in ("并", "然后", "接着", "之后", "输入", "写", "搜索", "发给")):
        return ""
    return text


def verify_done(task: str, summary: str, plugin, plan=None) -> dict:
    """对“做完了”做最低限度的事实检查。"""
    summary = str(summary or "").strip()
    task = str(task or "").strip()
    if not summary:
        return {"ok": False, "reason": "done 没有给出完成说明"}

    open_target = _extract_target(task, ("打开", "启动", "运行"))
    if open_target:
        try:
            hwnd = int(plugin._find_app_window(open_target, task) or 0)
        except Exception:
            hwnd = 0
        if not hwnd:
            return {"ok": False, "reason": f"没有找到“{open_target}”的窗口"}

    close_target = _extract_target(task, ("关闭", "关掉", "退出", "结束"))
    if close_target:
        try:
            hwnd = int(plugin._find_app_window(close_target, task) or 0)
        except Exception:
            hwnd = 0
        if hwnd and _window_alive(hwnd):
            return {"ok": False, "reason": f"“{close_target}”的窗口还在"}

    if plan is not None and plan.completed():
        # 计划走完但没有 done 的情况由主循环处理；这里只确认 summary 不是空的。
        pass
    return {"ok": True, "reason": "验收通过"}


__all__ = ["verify_action", "verify_done"]
