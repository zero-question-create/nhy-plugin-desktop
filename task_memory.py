# plugins/desktop/task_memory.py
# ========================================================================
# 桌面任务计划记忆：只存“成功过的高层计划”，不存 UIA id/坐标。
#
# 下次遇到相似任务时直接复用计划，省掉一次规划模型调用；计划里的步骤
# 仍然每一步看当前界面执行，因此不会因为旧 ref 过期而点错。
# ========================================================================

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from core.repetition import similarity

PROJECT_DIR = Path(__file__).resolve().parents[2]
MEMORY_FILE = PROJECT_DIR / "data" / "test" / "desktop_task_memory.json"
_MAX_ITEMS = 120
_MIN_SCORE = 0.78
_LOCK = threading.RLock()


def _load() -> list:
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_FILE.write_text(
            json.dumps(items[-_MAX_ITEMS:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def find_plan(task: str) -> Optional[dict]:
    """找相似任务最近一次成功计划；没有就返回 None。"""
    text = str(task or "").strip()
    if not text:
        return None
    with _LOCK:
        items = _load()
    best = None
    best_score = 0.0
    for item in items:
        score = similarity(text, str(item.get("task") or ""))
        if score > best_score:
            best, best_score = item, score
    if best is not None and best_score >= _MIN_SCORE:
        plan = best.get("plan")
        if isinstance(plan, dict) and plan.get("steps"):
            return plan
    return None


def remember_plan(task: str, plan) -> None:
    """记住一次成功的计划；同一任务只保留最新一份。"""
    text = str(task or "").strip()
    if not text or plan is None:
        return
    try:
        payload = plan.summary()
    except Exception:
        return
    if not payload.get("steps"):
        return
    with _LOCK:
        items = _load()
        items = [
            item for item in items
            if similarity(text, str(item.get("task") or "")) < 0.92
        ]
        items.append({
            "task": text,
            "plan": payload,
            "saved_at": round(time.time(), 3),
        })
        _save(items[-_MAX_ITEMS:])


__all__ = ["find_plan", "remember_plan"]
