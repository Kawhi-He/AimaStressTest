"""Low-level UI automation for the Quectel radar recording tool."""

import os
import subprocess
import sys
import time

import psutil
import win32api
import win32con
import win32gui
import win32process
from pywinauto import Application, Desktop, keyboard


EXE_PATH = r"D:\Kawhi\Tools\Quectel_Radar_AM100AA-MT_Tool_V1.2\Quectel_Radar_AM100AA-MT_Tool_V1.2.exe"
EXE_NAME = os.path.basename(EXE_PATH)


def get_exe_pid():
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["name"] and proc.info["name"].lower() == EXE_NAME.lower():
            return proc.info["pid"]
    return None


def enum_windows_for_pid(pid):
    result = []

    def callback(hwnd, _):
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid == pid and win32gui.IsWindowVisible(hwnd):
            result.append(
                (
                    hwnd,
                    win32gui.GetClassName(hwnd),
                    win32gui.GetWindowText(hwnd),
                )
            )
        return True

    win32gui.EnumWindows(callback, None)
    return result


def click_at_rect(rect):
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    win32api.SetCursorPos((cx, cy))
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, cx, cy, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, cx, cy, 0, 0)


def click_control(control):
    wrapper = control.wrapper_object()
    wrapper.set_focus()
    wrapper.click_input()


def select_first_combo_item(combo):
    wrapper = combo.wrapper_object()
    wrapper.set_focus()
    wrapper.click_input()
    time.sleep(0.3)
    keyboard.send_keys("{HOME}{ENTER}")


def find_main_window(pid):
    windows = enum_windows_for_pid(pid)
    for hwnd, _cls, text in windows:
        if "AM100AA" in text:
            return hwnd
    for hwnd, _cls, text in windows:
        if "CAN" not in text:
            return hwnd
    return None


def find_can_window(pid, main_hwnd):
    for hwnd, _cls, text in enum_windows_for_pid(pid):
        if hwnd != main_hwnd and "CAN" in text:
            return hwnd
    return None


def wait_for_pid(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = get_exe_pid()
        if pid is not None:
            return pid
        time.sleep(0.5)
    return None


def wait_for_main_window(pid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_main_window(pid)
        if hwnd is not None:
            return hwnd
        time.sleep(0.5)
    return None


def wait_for_can_window(pid, main_hwnd, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = find_can_window(pid, main_hwnd)
        if hwnd is not None:
            return hwnd
        time.sleep(0.5)
    return None


def _window_texts(window):
    texts = []
    try:
        title = window.window_text()
        if title:
            texts.append(title)
    except Exception:
        pass
    try:
        for item in window.descendants(control_type="Text"):
            text = item.window_text()
            if text:
                texts.append(text)
    except Exception:
        pass
    return texts


def _is_point_cloud_only_prompt(window):
    text = "\n".join(_window_texts(window))
    lowered = text.lower()
    return (
        ("摄像头未打开" in text or "camera" in lowered)
        and ("仅录制点云" in text or "点云数据" in text or "point" in lowered)
    )


def _click_yes_button(window):
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


def confirm_point_cloud_only_recording(timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in Desktop(backend="uia").windows():
            try:
                if not _is_point_cloud_only_prompt(window):
                    continue

                print("[INFO] Camera prompt detected; clicking Yes...")
                if _click_yes_button(window):
                    return True
            except Exception:
                continue
        time.sleep(0.2)
    return False


def click_record_button(main_win):
    click_control(
        main_win.child_window(
            auto_id="MainWindow.centralwidget.groupBox_Record.toolButton_Record"
        )
    )


def get_record_button(main_win):
    return main_win.child_window(
        auto_id="MainWindow.centralwidget.groupBox_Record.toolButton_Record"
    )


def is_record_button_ready(main_win):
    try:
        button = get_record_button(main_win).wrapper_object()
        return button.is_visible() and button.is_enabled()
    except Exception:
        return False


def close_can_dialog_if_open(pid, main_hwnd):
    can_hwnd = find_can_window(pid, main_hwnd)
    if can_hwnd is None:
        return False
    win32gui.PostMessage(can_hwnd, win32con.WM_CLOSE, 0, 0)
    time.sleep(0.5)
    return True


def open_can_dialog(main_win, pid, main_hwnd):
    existing = find_can_window(pid, main_hwnd)
    if existing is not None:
        print(f"[INFO] CAN dialog already open: hwnd=0x{existing:08X}")
        return existing

    print("[INFO] Step 2: click the Communication menu...")
    menu_bar = main_win.child_window(auto_id="MainWindow.menuBar")
    communication_item = menu_bar.children(control_type="MenuItem")[0]
    click_at_rect(communication_item.element_info.rectangle)
    time.sleep(0.8)

    print("[INFO] Step 3: click CAN...")
    can_menu = None
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

    can_hwnd = wait_for_can_window(pid, main_hwnd)
    if can_hwnd is None:
        raise RuntimeError("CAN dialog did not appear")
    return can_hwnd


def main():
    pid = get_exe_pid()
    if pid is None:
        print("[INFO] App is not running; launching it...")
        subprocess.Popen([EXE_PATH])
        pid = wait_for_pid()

    if pid is None:
        print("[ERROR] Could not launch or find the target process")
        sys.exit(1)

    print(f"[INFO] Target PID: {pid}")

    main_hwnd = wait_for_main_window(pid)
    if main_hwnd is None:
        print("[ERROR] Could not find the main window")
        sys.exit(1)

    print(f"[INFO] Main window: hwnd=0x{main_hwnd:08X}")
    app = Application(backend="uia").connect(handle=main_hwnd)
    main_win = app.window(handle=main_hwnd)

    # Step 1 is complete once the app is running and the main window is connected.
    can_hwnd = open_can_dialog(main_win, pid, main_hwnd)

    print("[INFO] Step 4: select the first device type...")
    can_app = Application(backend="uia").connect(handle=can_hwnd)
    can_dialog = can_app.window(handle=can_hwnd)
    select_first_combo_item(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_2.groupBox_3.comboBox_DeviceType"
        )
    )
    time.sleep(0.5)

    print("[INFO] Step 5: click Open Device...")
    click_control(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_2.groupBox_5.btn_OpenDevice"
        )
    )
    time.sleep(2)

    print("[INFO] Step 6: click Open CAN...")
    click_control(
        can_dialog.child_window(
            auto_id="MainWindow.CANDialog.groupBox_2.groupBox_5.btn_OpenCAN"
        )
    )
    time.sleep(2)

    print("[INFO] Step 7: close CAN dialog...")
    win32gui.PostMessage(can_hwnd, win32con.WM_CLOSE, 0, 0)
    time.sleep(1)

    print("[INFO] Step 8: click Start Recording...")
    click_record_button(main_win)
    confirm_point_cloud_only_recording()

    print("[INFO] Recording for 10 seconds...")
    time.sleep(10)

    print("[INFO] Click Start Recording again to stop...")
    click_record_button(main_win)
    time.sleep(0.5)

    print("[INFO] Done.")


if __name__ == "__main__":
    main()
