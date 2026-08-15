"""Resize the LDPlayer window so the whole screen is visible.

LDPlayer opens the window at the instance's full logical resolution (e.g.
1080x1920). On small displays the bottom is cut off. LDPlayer scales the
Android content to the window, so resizing the window to fit the screen shows
the entire emulated screen without losing anything.
"""

from __future__ import annotations

import ctypes
import subprocess

from ctypes import wintypes

from .config import find_ldconsole, load_config

user32 = ctypes.windll.user32

SW_RESTORE = 9


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _enum_windows(target_pid: int) -> list[tuple[int, str, tuple[int, int, int, int], bool]]:
    """Return (hwnd, title, rect, iconic) for every top-level window of pid."""
    found: list[tuple[int, str, tuple[int, int, int, int], bool]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != target_pid:
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        iconic = bool(user32.IsIconic(hwnd))
        found.append((int(hwnd), buf.value,
                      (rect.left, rect.top, rect.right, rect.bottom), iconic))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def _find_player_window() -> tuple[int, str, tuple[int, int, int, int], bool] | None:
    cfg = load_config()
    exe = (cfg.get("ldconsole") or "").rsplit("\\", 1)[0] or None
    names = ["dnplayer.exe"]
    if exe:
        names.insert(0, (exe + "\\dnplayer.exe"))
    best = None
    for cand in set(names):
        if not cand:
            continue
        try:
            out = subprocess.run(["tasklist", "/FI",
                                  f"IMAGENAME eq {cand}",
                                  "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True,
                                 timeout=15, check=False).stdout
        except Exception:
            continue
        for line in out.strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            for hwnd, title, rect, iconic in _enum_windows(pid):
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                area = w * h
                score = 0
                if "LDPlayer" in title:
                    score += 10000
                elif w > 100 and h > 100 and area > 40000:
                    score += area
                if best is None or score > best[0]:
                    best = (score, hwnd, title, rect, iconic)
    if best:
        _, hwnd, title, rect, iconic = best
        return hwnd, title, rect, iconic
    return None


def fit_window(scale: float = 1.0, center: bool = True) -> dict:
    """Restore (if minimized) and resize the LDPlayer window to fit the screen.

    `scale` lets you shrink the fitted window (e.g. 0.9 leaves a small border).
    Returns a report dict with the before/after window and screen bounds.
    """
    report = {"found": False}
    win = _find_player_window()
    if not win:
        report["error"] = "LDPlayer window not found"
        return report
    hwnd, title, rect, iconic = win
    report.update({"found": True, "title": title, "hwnd": hwnd,
                   "before": rect, "iconic": iconic})

    if iconic:
        user32.ShowWindow(hwnd, SW_RESTORE)
        rect2 = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect2))
        rect = (rect2.left, rect2.top, rect2.right, rect2.bottom)

    screen = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(screen), 0)  # SPI_GETWORKAREA
    sw = screen.right - screen.left
    sh = screen.bottom - screen.top

    w = rect[2] - rect[0]
    h = rect[3] - rect[1]
    if w <= 0 or h <= 0:
        report["error"] = "invalid window size"
        return report

    fit_w = sw * scale
    fit_h = sh * scale
    aspect = w / h
    if fit_w / fit_h > aspect:
        nw = int(fit_h * aspect)
        nh = int(fit_h)
    else:
        nw = int(fit_w)
        nh = int(fit_w / aspect)

    x = int((sw - nw) / 2) + screen.left if center else screen.left
    y = int((sh - nh) / 2) + screen.top if center else screen.top
    user32.MoveWindow(hwnd, x, y, nw, nh, True)

    after = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(after))
    report["after"] = (after.left, after.top, after.right, after.bottom)
    report["screen"] = (screen.left, screen.top, screen.right, screen.bottom)
    report["window_size"] = (nw, nh)
    return report
