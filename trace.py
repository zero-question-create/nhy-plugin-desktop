# plugins/desktop/trace.py
# ========================================================================
# 桌面任务轨迹：每一步写一行 JSONL，供面板/回归分析使用。
#
# 不记录整棵 UIA 树，只记录：动作、控件 ID、摘要大小、耗时、错误类型、
# 验证结果和最终状态。文件超过上限自动轮转一份，避免长期运行撑满磁盘。
# ========================================================================

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parents[2]
TRACE_DIR = PROJECT_DIR / "run" / "logs"
TRACE_FILE = TRACE_DIR / "desktop_trace.jsonl"
MAX_BYTES = 8 * 1024 * 1024


class DesktopTrace:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or TRACE_FILE)
        self._lock = threading.RLock()

    def _rotate_if_needed(self):
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                backup = self.path.with_suffix(".jsonl.1")
                try:
                    backup.unlink()
                except OSError:
                    pass
                self.path.replace(backup)
        except OSError:
            pass

    def event(self, kind: str, **fields) -> None:
        payload = {
            "ts": round(time.time(), 3),
            "event": str(kind or ""),
        }
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value[:500] if isinstance(value, str) else value
            else:
                payload[key] = str(value)[:500]
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            # 轨迹是观测，不应该影响任务执行。
            pass


TRACE = DesktopTrace()


def read_trace_tasks(limit: int = 30) -> list:
    """读取轨迹文件尾部，按 goal 汇总成面板任务列表。"""
    path = TRACE_FILE
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - 512 * 1024)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read().decode("utf-8", "ignore")
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]
    except OSError:
        return []
    tasks = {}
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        gid = str(event.get("goal_id") or "").strip()
        if not gid:
            continue
        record = tasks.get(gid)
        if record is None:
            record = {
                "goal_id": gid, "task": "", "state": "unknown",
                "step": 0, "step_window": 0, "elapsed": 0.0,
                "summary": "", "verified": False, "events": [],
                "_last_ts": 0.0,
            }
            tasks[gid] = record
        ts = float(event.get("ts") or 0)
        record["_last_ts"] = max(record["_last_ts"], ts)
        for key in ("task", "state", "step", "step_window", "elapsed", "summary"):
            if event.get(key) not in (None, ""):
                record[key] = event[key]
        if event.get("verified"):
            record["verified"] = True
        name = str(event.get("event") or "")
        if name == "runtime_goal_start":
            record["state"] = "running"
        elif name == "goal_pause":
            record["state"] = "still_trying"
        elif name == "runtime_goal_end":
            if event.get("cancelled"):
                record["state"] = "cancelled"
            elif event.get("ok"):
                record["state"] = "done"
            else:
                record["state"] = "failed"
        step = event.get("step")
        action = str(event.get("action") or "")
        target = str(event.get("target") or "")
        if name == "step" and step:
            label = f"第{step}步 {action or '动作'} {target}".strip()
        elif name == "plan":
            label = "规划完成"
        elif name == "goal_pause":
            label = "达到步数窗口，暂停"
        elif name == "receipt":
            label = "回执 " + str(event.get("state") or "")
        elif name == "result":
            label = ("结果 " + ("成功" if event.get("ok") else "失败")
                     + (f"（{event.get('error_class')}）" if event.get("error_class") else ""))
        else:
            label = name or "事件"
        try:
            time_text = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else ""
        except (ValueError, OSError):
            time_text = ""
        record["events"].append({
            "time": time_text,
            "event": name,
            "label": label,
            "ok": event.get("ok"),
            "error_class": event.get("error_class") or "",
        })
    result = sorted(tasks.values(), key=lambda item: item["_last_ts"], reverse=True)[: max(1, int(limit))]
    for item in result:
        item.pop("_last_ts", None)
        item["events"] = item["events"][-150:]
    return result


__all__ = ["DesktopTrace", "TRACE", "read_trace_tasks"]
