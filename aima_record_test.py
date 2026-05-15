#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Author: Kawhi.He

AIMA workflow automation: launch the radar tool, follow the AIMA flow,
record for 5 seconds, stop, then analyze the generated frame.txt.

Pass criteria: frame.txt exists and contains at least one point cloud data line.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

import win32api
import win32con
import win32gui
import win32process
import psutil
from pywinauto import Application, Desktop, keyboard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXE_PATH = r"F:\Firmware\Quectel_Radar_AM100AA-MT_Tool_V1.2\Quectel_Radar_AM100AA-MT_Tool_V1.2\Quectel_Radar_AM100AA-MT_Tool_V1.2.exe"
EXE_DIR = os.path.dirname(EXE_PATH)
EXE_NAME = os.path.basename(EXE_PATH)
RECORD_SECONDS = 5


def _norm(text: str) -> str:
    return "".join((text or "").split()).strip().lower()


# ---------------------------------------------------------------------------
# Process / Window helpers (shared with radar_can_tool.py)
# ---------------------------------------------------------------------------

def get_exe_pid() -> int | None:
    """Return the PID of the running radar tool process, or None.

    Returns:
        int | None: PID if found, otherwise None.
    """
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] and proc.info["name"].lower() == EXE_NAME.lower():
            return proc.info["pid"]
    return None


def enum_windows_for_pid(pid: int) -> list:
    """Enumerate all visible top-level windows belonging to *pid*.

    Args:
        pid: Target process ID.

    Returns:
        list: List of (hwnd, class_name, window_title) tuples.
    """
    result = []

    def callback(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid == pid and win32gui.IsWindowVisible(hwnd):
            result.append((hwnd, win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(callback, None)
    return result


def find_main_window(pid: int) -> int | None:
    """Locate the main application window.

    Args:
        pid: Target process ID.

    Returns:
        int | None: Window handle (HWND) or None if not found.
    """
    _SKIP_CLASSES = {"QComboBoxPrivateContainer", "QMenu", "tooltips_class32"}
    windows = enum_windows_for_pid(pid)
    for hwnd, cls, text in windows:
        if cls in _SKIP_CLASSES:
            continue
        if "AM100AA" in text:
            return hwnd
    for hwnd, cls, text in windows:
        if cls in _SKIP_CLASSES:
            continue
        if "CAN" not in text:
            return hwnd
    return None


def find_can_window(pid: int, main_hwnd: int) -> int | None:
    """Locate the CAN dialog window.

    Args:
        pid: Target process ID.
        main_hwnd: Handle of the main window to exclude.

    Returns:
        int | None: CAN dialog HWND or None if not found.
    """
    for hwnd, _cls, text in enum_windows_for_pid(pid):
        if hwnd != main_hwnd and "CAN" in text:
            return hwnd
    return None


def wait_for_pid(timeout: float = 15.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = get_exe_pid()
        if pid is not None:
            return pid
        time.sleep(0.5)
    return None


def wait_for_main_window(pid: int, timeout: float = 20.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_main_window(pid)
        if hwnd is not None:
            return hwnd
        time.sleep(0.5)
    return None


def wait_for_can_window(pid: int, main_hwnd: int, timeout: float = 10.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_can_window(pid, main_hwnd)
        if hwnd is not None:
            return hwnd
        time.sleep(0.5)
    return None


def close_main_tool(main_hwnd: int, pid: int) -> None:
    """Close the radar tool window and ensure process is terminated.

    Args:
        main_hwnd: Main window handle.
        pid: Process ID.

    Returns:
        None
    """
    try:
        win32gui.PostMessage(main_hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass

    # Wait briefly for graceful exit.
    deadline = time.time() + 5
    while time.time() < deadline:
        if get_exe_pid() is None:
            return
        time.sleep(0.2)

    # Force-kill as fallback.
    try:
        proc = psutil.Process(pid)
        proc.kill()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Click helpers
# ---------------------------------------------------------------------------

def click_at_rect(rect) -> None:
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, cx, cy, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, cx, cy, 0, 0)


def click_control(control) -> None:
    wrapper = control.wrapper_object()
    wrapper.set_focus()
    wrapper.click_input()


def click_specific_apply_buttons(main_win) -> tuple[bool, bool]:
    """Click the two required apply buttons before recording.

    Required buttons:
    - 视图展示对应应用: btn_ViewApply
    - 配置输出对应应用: btn_Apply

    Args:
        main_win: Main window wrapper.

    Returns:
        tuple[bool, bool]: (view_apply_ok, output_apply_ok)
    """
    view_apply_ok = False
    output_apply_ok = False

    try:
        click_control(
            main_win.child_window(
                auto_id="MainWindow.centralwidget.groupBox_ViewConfig.btn_ViewApply"
            )
        )
        view_apply_ok = True
    except Exception:
        view_apply_ok = False

    try:
        click_control(
            main_win.child_window(
                auto_id="MainWindow.centralwidget.groupBox_ViewConfig.btn_Apply"
            )
        )
        output_apply_ok = True
    except Exception:
        output_apply_ok = False

    return view_apply_ok, output_apply_ok


def select_combo_item_by_text(combo, target_text: str) -> None:
    """Select a combo box item whose text matches target_text.

    Expands the combo via ExpandCollapse UIA pattern, then clicks the
    matching ListItem. Falls back to keyboard navigation if needed.

    Args:
        combo: pywinauto control wrapper for the ComboBox.
        target_text: The exact display text of the item to select.

    Returns:
        None
    """
    wrapper = combo.wrapper_object()
    wrapper.set_focus()

    # Try ExpandCollapse UIA pattern to open the drop-down
    try:
        wrapper.expand()
        time.sleep(0.4)
    except Exception:
        wrapper.click_input()
        time.sleep(0.4)

    # Look for ListItem in the combo's own descendants
    try:
        for item in wrapper.descendants(control_type="ListItem"):
            if item.window_text().strip() == target_text:
                item.click_input()
                time.sleep(0.2)
                return
    except Exception:
        pass

    # Search entire Desktop (Qt pops list as a separate window)
    try:
        for win in Desktop(backend="uia").windows():
            try:
                for item in win.descendants(control_type="ListItem"):
                    if item.window_text().strip() == target_text:
                        item.click_input()
                        time.sleep(0.2)
                        return
            except Exception:
                continue
    except Exception:
        pass

    # Last resort: keyboard arrow navigation
    keyboard.send_keys("{ESC}")
    time.sleep(0.1)
    wrapper.click_input()
    time.sleep(0.3)
    keyboard.send_keys("{HOME}")
    time.sleep(0.1)
    # Try up to 10 items with DOWN arrow
    for _ in range(10):
        try:
            current = wrapper.selected_text() if hasattr(wrapper, "selected_text") else ""
        except Exception:
            current = ""
        try:
            current = wrapper.window_text()
        except Exception:
            pass
        if current.strip() == target_text:
            keyboard.send_keys("{ENTER}")
            return
        keyboard.send_keys("{DOWN}")
        time.sleep(0.1)
    keyboard.send_keys("{ENTER}")


# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------

def _window_texts(window) -> list[str]:
    texts = []
    try:
        title = window.window_text()
        if title:
            texts.append(title)
    except Exception:
        pass
    try:
        for item in window.descendants(control_type="Text"):
            t = item.window_text()
            if t:
                texts.append(t)
    except Exception:
        pass
    return texts


def _is_point_cloud_only_prompt(window) -> bool:
    text = "\n".join(_window_texts(window))
    lowered = text.lower()
    return ("摄像头未打开" in text or "camera" in lowered) and (
        "仅录制点云" in text or "点云数据" in text or "point" in lowered
    )


def _click_yes_button(window) -> bool:
    yes_titles = {"Yes", "&Yes", "是", "确定", "OK"}
    for button in window.descendants(control_type="Button"):
        try:
            title = button.window_text().strip()
            if title in yes_titles:
                button.click_input()
                return True
        except Exception:
            continue
    try:
        keyboard.send_keys("{ENTER}")
        return True
    except Exception:
        return False


def confirm_point_cloud_only_recording(timeout: float = 5.0) -> bool:
    """Dismiss the 'camera not open, record point cloud only?' prompt.

    Args:
        timeout: Maximum seconds to wait for the prompt.

    Returns:
        bool: True if the prompt was found and dismissed.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in Desktop(backend="uia").windows():
            try:
                if not _is_point_cloud_only_prompt(window):
                    continue
                print("[INFO] Camera-only prompt detected; clicking Yes...")
                if _click_yes_button(window):
                    return True
            except Exception:
                continue
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Record button
# ---------------------------------------------------------------------------

def click_record_button(main_win) -> None:
    """Click the 开始录制 (Start/Stop Recording) button.

    Args:
        main_win: pywinauto window wrapper for the main window.

    Returns:
        None
    """
    click_control(
        main_win.child_window(
            auto_id="MainWindow.centralwidget.groupBox_Record.toolButton_Record"
        )
    )


# ---------------------------------------------------------------------------
# frame.txt analysis
# ---------------------------------------------------------------------------

def find_frame_txt() -> str | None:
    """Search for the most recently modified frame.txt under the tool directory.

    Returns:
        str | None: Absolute path to the newest frame.txt if found, otherwise None.
    """
    candidates = []
    for search_root in [EXE_DIR, os.getcwd()]:
        for root, _dirs, files in os.walk(search_root):
            if "frame.txt" in files:
                p = os.path.join(root, "frame.txt")
                candidates.append((os.path.getmtime(p), p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def analyse_frame_txt(path: str) -> tuple[bool, str]:
    """Determine whether frame.txt contains valid point cloud data.

    A line is considered point cloud data when it contains numeric fields
    consistent with radar point cloud output (at least 4 numeric tokens per line,
    excluding header/comment lines).

    Args:
        path: Absolute path to frame.txt.

    Returns:
        tuple[bool, str]: (passed, reason) where passed is True when point
            cloud data is present.
    """
    if not os.path.isfile(path):
        return False, f"frame.txt not found at {path}"

    size = os.path.getsize(path)
    if size == 0:
        return False, "frame.txt is empty (0 bytes)"

    point_cloud_lines = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith("frame"):
                continue
            tokens = line.split()
            # Heuristic: a point cloud data line has >= 4 numeric tokens
            numeric = sum(1 for t in tokens if _is_numeric(t))
            if numeric >= 4:
                point_cloud_lines += 1

    if point_cloud_lines > 0:
        return True, f"{point_cloud_lines} point cloud data line(s) found"
    return False, "No point cloud data lines detected in frame.txt"


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def main() -> None:
    """Execute the full AIMA workflow and report PASS / FAIL.

    Returns:
        None
    """
    # ---- 1. Launch / attach ------------------------------------------------
    pid = get_exe_pid()
    if pid is None:
        print(f"[INFO] Launching {EXE_NAME}...")
        subprocess.Popen([EXE_PATH])
        pid = wait_for_pid(timeout=15)

    if pid is None:
        print("[ERROR] Could not launch or find the target process")
        sys.exit(1)

    print(f"[INFO] PID: {pid}")

    main_hwnd = wait_for_main_window(pid, timeout=20)
    if main_hwnd is None:
        print("[ERROR] Could not find the main window")
        sys.exit(1)

    print(f"[INFO] Main window HWND: 0x{main_hwnd:08X}")
    app = Application(backend="uia").connect(handle=main_hwnd)
    main_win = app.window(handle=main_hwnd)

    # ---- 2. Open CAN dialog (always close any existing dialog first) --------
    existing_can = find_can_window(pid, main_hwnd)
    if existing_can is not None:
        print(f"[INFO] Closing stale CAN dialog: 0x{existing_can:08X}")
        win32gui.PostMessage(existing_can, win32con.WM_CLOSE, 0, 0)
        time.sleep(2)  # wait for floating Qt windows to dismiss
        # Re-acquire main window and app handle after cleanup
        main_hwnd = wait_for_main_window(pid, timeout=10)
        if main_hwnd is None:
            print("[ERROR] Lost main window after closing CAN dialog")
            sys.exit(1)
        app = Application(backend="uia").connect(handle=main_hwnd)
        main_win = app.window(handle=main_hwnd)
        print(f"[INFO] Re-acquired main window: 0x{main_hwnd:08X}")

    print("[INFO] Opening Communication > CAN menu...")
    menu_bar = main_win.child_window(auto_id="MainWindow.menuBar")
    communication_item = menu_bar.children(control_type="MenuItem")[0]
    click_at_rect(communication_item.element_info.rectangle)
    time.sleep(0.8)

    try:
        can_menu = main_win.child_window(auto_id="MainWindow.actionCAN")
        rect = can_menu.element_info.rectangle
    except Exception:
        can_items = Desktop(backend="uia").windows(control_type="MenuItem", title="CAN")
        if can_items:
            rect = can_items[0].element_info.rectangle
        else:
            raise RuntimeError("CAN menu item was not found")

    click_at_rect(rect)
    can_hwnd = wait_for_can_window(pid, main_hwnd, timeout=10)
    if can_hwnd is None:
        raise RuntimeError("CAN dialog did not appear")

    # ---- 3. Configure CAN device -------------------------------------------
    can_app = Application(backend="uia").connect(handle=can_hwnd)
    can_dialog = can_app.window(handle=can_hwnd)

    print("[INFO] Selecting current radar device: AM102AA...")
    select_combo_item_by_text(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_10.comboBox_CurrentRadarDev"
        ),
        "AM102AA",
    )
    time.sleep(0.5)

    print("[INFO] Selecting CAN baud rate: 500Kbps...")
    select_combo_item_by_text(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_2.groupBox_InitCAN.comboBox_Baud"
        ),
        "500Kbps",
    )
    time.sleep(0.5)

    print("[INFO] Clicking Open Device...")
    click_control(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_2.groupBox_5.btn_OpenDevice"
        )
    )
    time.sleep(2)

    print("[INFO] Clicking Open CAN...")
    click_control(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_2.groupBox_5.btn_OpenCAN"
        )
    )
    time.sleep(2)

    # ---- 4. Close CAN dialog -----------------------------------------------
    print("[INFO] Closing CAN dialog...")
    win32gui.PostMessage(can_hwnd, win32con.WM_CLOSE, 0, 0)
    time.sleep(1)

    # ---- 5. Apply output/view settings before recording --------------------
    print("[INFO] Applying settings: 视图展示/配置输出 -> 应用...")
    view_ok, output_ok = click_specific_apply_buttons(main_win)
    if not view_ok:
        print("[WARN] Could not click 视图展示 对应应用按钮 (btn_ViewApply)")
    if not output_ok:
        print("[WARN] Could not click 配置输出 对应应用按钮 (btn_Apply)")
    time.sleep(0.5)

    # ---- 6. Start recording ------------------------------------------------
    print("[INFO] Clicking 开始录制 (Start Recording)...")
    click_record_button(main_win)
    confirm_point_cloud_only_recording(timeout=5)

    print(f"[INFO] Recording for {RECORD_SECONDS} seconds...")
    time.sleep(RECORD_SECONDS)

    # ---- 7. Stop recording -------------------------------------------------
    print("[INFO] Clicking 开始录制 again to stop recording...")
    click_record_button(main_win)
    time.sleep(1)

    print("[INFO] Recording stopped.")

    print("[INFO] Closing radar tool after recording...")
    close_main_tool(main_hwnd, pid)

    # ---- 8. Locate and analyse frame.txt -----------------------------------
    print("[INFO] Searching for frame.txt...")
    # Give the tool a brief moment to flush the file
    time.sleep(1)

    frame_path = find_frame_txt()
    if frame_path is None:
        print("[RESULT] FAIL – frame.txt was not found")
        sys.exit(1)

    print(f"[INFO] Found frame.txt: {frame_path}")
    passed, reason = analyse_frame_txt(frame_path)

    if passed:
        print(f"[RESULT] PASS – {reason}")
    else:
        print(f"[RESULT] FAIL – {reason}")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
