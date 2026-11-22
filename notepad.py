# plugins/desktop/notepad.py
# ========================================================================
# 记事本分页读取。
#
# 原来只看 UIA 树里的前 40 行，而且 Edit 的 value 被截到 120 字，
# 长文会直接丢内容。这里改为：
#   - 读取 Edit 的完整 ValuePattern（失败再看 TextPattern / WM_GETTEXT）；
#   - 按编辑区高度估算一页能显示多少行；
#   - 返回第 N 页 + 总页数，认知层可以继续翻页；
#   - 需要时按 EM_GETFIRSTVISIBLELINE 读取当前滚动到的页。
# ========================================================================

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

from config.api_config import config
from . import uia

_EM_GETFIRSTVISIBLELINE = 0x00CE
_WM_GETTEXTLENGTH = 0x000E
_WM_GETTEXT = 0x000D

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.SendMessageW.restype = wintypes.LPARAM


def _send_message(hwnd: int, message: int, wparam: int = 0, lparam: int = 0) -> int:
    try:
        return int(_user32.SendMessageW(int(hwnd), int(message), int(wparam), int(lparam)))
    except Exception:
        return 0


class NotepadReader:
    def __init__(self, backend=None):
        self._backend = backend

    def _be(self):
        return self._backend or uia.backend()

    def _find_edit(self, hwnd: int):
        backend = self._be()
        try:
            snap = backend.snapshot(
                hwnd, max_nodes=800, depth=18, chrome=True, with_bounds=True
            )
        except Exception:
            return None
        candidates = []
        for item in snap.get("elements") or []:
            role = str(item.get("role") or "")
            if role not in ("Edit", "Document"):
                continue
            actions = list(item.get("actions") or [])
            value = str(item.get("value") or "")
            if role == "Document" and "setValue" not in actions and not value:
                continue
            bounds = item.get("bounds") or [0, 0, 0, 0]
            area = max(0, int(bounds[2]) - int(bounds[0])) * max(
                0, int(bounds[3]) - int(bounds[1])
            )
            # Edit 优先；同样角色时取面积最大的编辑区。
            score = (0 if role == "Edit" else 1, -area)
            candidates.append((score, item))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]

    def _native_text(self, ref: str) -> str:
        try:
            handle = self._be().element_native_handle(ref)
        except Exception:
            return ""
        hwnd = int((handle or {}).get("hwnd") or 0)
        if not hwnd:
            return ""
        length = _send_message(hwnd, _WM_GETTEXTLENGTH)
        if length <= 0:
            return ""
        length = min(int(length), 4 * 1024 * 1024)
        buffer = ctypes.create_unicode_buffer(length + 1)
        _send_message(
            hwnd, _WM_GETTEXT, length + 1,
            ctypes.cast(buffer, ctypes.c_void_p).value or 0,
        )
        return str(buffer.value or "")

    def _first_visible_line(self, ref: str) -> int:
        try:
            handle = self._be().element_native_handle(ref)
        except Exception:
            return 0
        hwnd = int((handle or {}).get("hwnd") or 0)
        return max(0, _send_message(hwnd, _EM_GETFIRSTVISIBLELINE)) if hwnd else 0

    def _full_text(self, ref: str) -> str:
        backend = self._be()
        try:
            value = backend.element_value(ref)
            text = str((value or {}).get("value") or "")
            if text:
                return text
        except Exception:
            pass
        try:
            text = str((backend.element_text(ref) or {}).get("text") or "")
            if text:
                return text
        except Exception:
            pass
        return self._native_text(ref)

    def _page_lines(self, ref: str, explicit: int = None) -> int:
        if explicit:
            return max(1, min(400, int(explicit)))
        height = 0
        try:
            rect = self._be().element_rect(ref)
            height = max(0, int(rect.get("bottom") or 0) - int(rect.get("top") or 0))
        except Exception:
            pass
        try:
            line_height = max(8, int(config.get("notepad_line_height_px", 20)))
            max_lines = max(5, int(config.get("notepad_page_lines_max", 80)))
        except (TypeError, ValueError):
            line_height, max_lines = 20, 80
        if height <= 0:
            return 30
        # 去掉上下内边距，至少 5 行。
        return max(5, min(max_lines, height // line_height - 1))

    def read_page(self, hwnd: int, page: int = 1, page_lines: int = None,
                  current_page: bool = False) -> dict:
        edit = self._find_edit(int(hwnd))
        if edit is None:
            return {"ok": False, "detail": "没有找到记事本的编辑区"}
        ref = str(edit.get("ref") or "")
        text = self._full_text(ref)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "" and text.endswith("\n"):
            lines = lines[:-1]
        total_lines = len(lines)
        size = self._page_lines(ref, page_lines)
        pages = max(1, (total_lines + size - 1) // size) if total_lines else 1
        if current_page and total_lines:
            first = self._first_visible_line(ref)
            page = first // size + 1
        try:
            page = int(page or 1)
        except (TypeError, ValueError):
            page = 1
        page = max(1, min(pages, page))
        start = (page - 1) * size
        end = min(total_lines, start + size)
        page_content = "\n".join(lines[start:end])
        detail = (
            f"记事本第 {page}/{pages} 页（窗口约 {size} 行/页，共 {total_lines} 行）"
            f"{'：' + chr(10) + page_content if page_content else '：内容为空'}"
        )
        return {
            "ok": True,
            "page": page,
            "pages": pages,
            "page_lines": size,
            "total_lines": total_lines,
            "content": page_content,
            "content_lines": lines[start:end],
            "detail": detail,
            "ref": ref,
        }


__all__ = ["NotepadReader"]
