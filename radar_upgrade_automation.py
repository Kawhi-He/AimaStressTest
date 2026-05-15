#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Author: Kawhi.He

Automate Quectel Radar Update Tool with pywinauto.

Workflow:
1. Launch the update tool executable.
2. Go to Communication page.
3. Set CAN baud rate to 500Kbps.
4. Click "Open Device" and "Open CAN".
5. Return to Upgrade page.
6. Click "Get Radar Version Info".
7. Open APP file and choose APP.bin.
8. Click "Start".
9. Monitor progress and upgrade logs until success/failure/timeout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pywinauto import Application, Desktop, timings
from pywinauto.base_wrapper import BaseWrapper
from pywinauto.controls.uiawrapper import UIAWrapper
from pywinauto.findbestmatch import MatchError
from pywinauto.keyboard import send_keys


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").strip().lower()


def _last_marker_index(text: str, markers: tuple[str, ...]) -> int:
    """Get the last index of any marker in text.

    Args:
        text: Input text.
        markers: Candidate marker strings.

    Returns:
        int: Last index if found, otherwise -1.
    """

    idx = -1
    for marker in markers:
        pos = text.rfind(marker)
        if pos > idx:
            idx = pos
    return idx


def classify_upgrade_log_text(log_text: str) -> Optional[tuple[bool, str]]:
    """Classify upgrade result from log text.

    Args:
        log_text: Full log text from upgrade tool.

    Returns:
        Optional[tuple[bool, str]]: (is_success, reason) if classified; otherwise None.
    """

    norm = _norm(log_text)
    success_markers = (
        "升级成功!",
        "升级成功！",
        "升级成功",
        "success",
        "升级完成",
        "completed",
    )
    failure_markers = (
        "升级失败...",
        "升级失败。。。",
        "升级失败",
        "fail",
        "错误",
        "error",
    )

    success_idx = _last_marker_index(norm, success_markers)
    fail_idx = _last_marker_index(norm, failure_markers)

    if success_idx < 0 and fail_idx < 0:
        return None
    if success_idx > fail_idx:
        return True, "success marker in logs"
    return False, "failure marker in logs"


@dataclass
class Config:
    """Runtime configuration for GUI automation.

    Args:
        exe_path: Full path to Quectel Radar Update Tool executable.
        app_bin_path: Full path to APP.bin firmware file.
        can_baudrate: Target baud rate text in the combo box.
        timeout: Global timeout for long running waits in seconds.
        poll_interval: Polling interval for progress/log monitoring.
        log_output: Optional output file path for captured upgrade logs.

    Returns:
        None. This dataclass stores configuration values.
    """

    exe_path: Path
    app_bin_path: Path
    can_baudrate: str = "500Kbps"
    timeout: int = 1200
    poll_interval: float = 1.0
    log_output: Optional[Path] = None


class RadarUpgradeAutomation:
    """UI automation wrapper for Quectel Radar Update Tool.

    Args:
        config: Runtime configuration parameters.

    Returns:
        None. Use run() to execute the full upgrade workflow.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.app: Optional[Application] = None
        self.main: Optional[UIAWrapper] = None

    def run(self) -> int:
        """Execute the end-to-end upgrade automation.

        Args:
            None.

        Returns:
            Exit code: 0 on success, non-zero on failure.
        """

        self._validate_inputs()
        self._launch_app()

        self._switch_page("通信")
        self._set_can_baudrate(self.config.can_baudrate)
        self._click_in_group("CAN通信", "打开设备")
        self._click_in_group("CAN通信", "打开CAN")

        self._switch_page("升级")
        self._click_button("获取雷达版本信息")

        self._select_app_file_in_group("APP升级", "打开文件", self.config.app_bin_path)
        self._click_button("开始")

        ok, reason = self._monitor_upgrade()
        print(f"[RESULT] success={ok}, reason={reason}")
        return 0 if ok else 2

    def _validate_inputs(self) -> None:
        """Validate executable and firmware file existence.

        Args:
            None.

        Returns:
            None.
        """

        if not self.config.exe_path.exists():
            raise FileNotFoundError(f"Tool executable not found: {self.config.exe_path}")
        if not self.config.app_bin_path.exists():
            raise FileNotFoundError(f"APP firmware file not found: {self.config.app_bin_path}")

    def _launch_app(self) -> None:
        """Launch the target application and bind to its main window.

        Args:
            None.

        Returns:
            None.
        """

        self._close_existing_instances()
        print(f"[INFO] Launching: {self.config.exe_path}")
        self.app = Application(backend="uia").start(str(self.config.exe_path))
        self.main = self.app.top_window()
        self.main.wait("visible", timeout=30)
        self.main.set_focus()
        time.sleep(1)

    def _close_existing_instances(self) -> None:
        """Close existing tool processes to guarantee single running instance.

        Args:
            None.

        Returns:
            None.
        """

        exe_name = self.config.exe_path.name
        # Keep only one process instance by force-closing older ones first.
        cmd = ["taskkill", "/F", "/T", "/IM", exe_name]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[INFO] Closed existing instances: {exe_name}")
            time.sleep(1.0)

    def _switch_page(self, page_name: str) -> None:
        """Switch to a page/tab by name.

        Args:
            page_name: UI text for target page, e.g. "通信" or "升级".

        Returns:
            None.
        """

        print(f"[STEP] Switch page -> {page_name}")
        if not self.main:
            raise RuntimeError("Main window is not ready")

        # Try tab first.
        tab_items = self.main.descendants(control_type="TabItem")
        for item in tab_items:
            if _norm(item.window_text()) == _norm(page_name):
                item.click_input()
                time.sleep(0.6)
                return

        # Fallback to button/text style navigation.
        self._click_button(page_name)

    def _set_can_baudrate(self, baudrate: str) -> None:
        """Set baud rate in CAN parameter area.

        Args:
            baudrate: Target baud rate text.

        Returns:
            None.
        """

        print(f"[STEP] Set CAN baudrate -> {baudrate}")
        if self.main is None:
            raise RuntimeError("Main window is not ready")

        combos = self.main.descendants(control_type="ComboBox")
        matched_combo = None

        # In this tool, the baudrate combo displays name text as "CAN参数".
        for combo in combos:
            if _norm(combo.window_text()) == _norm("CAN参数"):
                wrapper = UIAWrapper(combo.element_info)
                try:
                    wrapper.select(baudrate)
                    matched_combo = wrapper
                    break
                except Exception:
                    continue

        if matched_combo is None:
            raise RuntimeError("Cannot select baudrate from CAN parameter ComboBox")

        time.sleep(0.4)

    def _click_in_group(self, group_name: str, button_name: str) -> None:
        """Click a button inside a named group.

        Args:
            group_name: Group box title.
            button_name: Button text to click.

        Returns:
            None.
        """

        print(f"[STEP] Click in group [{group_name}] -> {button_name}")
        try:
            group = self._find_group(group_name)
            self._click_button(button_name, root=group)
        except Exception:
            # Some builds expose non-user-friendly group titles. Fallback globally.
            self._click_button(button_name)

    def _select_app_file_in_group(self, group_name: str, button_name: str, file_path: Path) -> None:
        """Open file dialog from group and select firmware file.

        Args:
            group_name: Group box title.
            button_name: Open file button text.
            file_path: Target firmware file path.

        Returns:
            None.
        """

        print(f"[STEP] Select APP file -> {file_path}")
        selected_group = None
        try:
            selected_group = self._find_group(group_name)
        except Exception:
            selected_group = None

        existing_handles = self._snapshot_top_window_handles()

        clicked = False
        try:
            if selected_group is not None:
                print("[INFO] Click APP open-file button in APP升级 group")
                self._click_button(button_name, root=selected_group)
                clicked = True
        except Exception:
            pass

        if not clicked:
            print("[INFO] Click APP open-file button globally")
            self._click_button(button_name)

        print("[INFO] Open-file button clicked, waiting for file dialog...")
        self._handle_file_dialog(file_path, existing_handles)

    def _snapshot_top_window_handles(self) -> set[int]:
        """Get current top-level window handles for new-dialog detection.

        Args:
            None.

        Returns:
            Set of current visible top-level window handles.
        """

        handles: set[int] = set()
        for win in Desktop(backend="uia").windows():
            try:
                if win.is_visible():
                    handles.add(win.handle)
            except Exception:
                continue
        return handles

    def _handle_file_dialog(self, file_path: Path, existing_handles: set[int]) -> None:
        """Handle standard Windows Open File dialog.

        Args:
            file_path: File path to input in the dialog.

        Returns:
            None.
        """

        if self.main is None:
            raise RuntimeError("Main window is not ready")

        # Give the modal dialog a moment to appear and become active.
        time.sleep(0.8)

        try:
            dialog = Desktop(backend="uia").top_window()
            if dialog.handle != self.main.handle:
                dialog.set_focus()
                print(f"[INFO] Active file dialog: title={dialog.window_text()!r}, class={dialog.element_info.class_name!r}")
                self._set_file_path_into_filename_box(dialog, file_path)

                try:
                    open_btn = dialog.child_window(auto_id="1", control_type="Button")
                    if open_btn.exists(timeout=1):
                        open_btn.click_input()
                        time.sleep(1.0)
                        return
                except Exception:
                    pass

                dialog.type_keys("%o")
                time.sleep(1.0)
                return
        except Exception:
            pass

        # Last fallback: send global hotkeys.
        self._global_type_file_path_and_open(file_path)

    def _set_file_path_into_filename_box(self, dialog: BaseWrapper, file_path: Path) -> None:
        """Type file path into the file dialog's filename input box.

        Args:
            dialog: File dialog window wrapper.
            file_path: Target file path.

        Returns:
            None.
        """

        typed = False

        # Typical Windows file dialog file-name edit box id.
        try:
            filename_edit = dialog.child_window(auto_id="1148", control_type="Edit")
            if filename_edit.exists(timeout=1):
                filename_edit.set_focus()
                # Use set_text to avoid IME/input-method side effects in the file-name box.
                filename_edit.set_text(str(file_path))
                typed = True
        except Exception:
            typed = False

        if typed:
            return

        # Fallback: hotkey to focus "文件名" box, then input path.
        dialog.type_keys("%n")
        time.sleep(0.2)
        dialog.type_keys("^a{BACKSPACE}")
        time.sleep(0.1)
        dialog.type_keys(str(file_path), with_spaces=True, set_foreground=True)
        time.sleep(0.2)

    def _global_type_file_path_and_open(self, file_path: Path) -> None:
        """Fallback: type full file path globally and confirm open.

        Args:
            file_path: Target firmware path.

        Returns:
            None.
        """

        time.sleep(0.8)
        # Force focus to the "文件名" box, then input full path and click Open.
        send_keys("%n")
        time.sleep(0.2)
        send_keys("^a{BACKSPACE}")
        time.sleep(0.1)
        send_keys(str(file_path), with_spaces=True)
        time.sleep(0.2)
        send_keys("%o")
        time.sleep(1.0)

    def _wait_file_dialog(self, existing_handles: set[int], timeout: int = 12) -> BaseWrapper:
        """Wait for the file selection dialog after clicking Open File.

        Args:
            timeout: Maximum wait time in seconds.

        Returns:
            Top-level window wrapper of the detected file dialog.
        """

        if self.main is None:
            raise RuntimeError("Main window is not ready")

        main_handle = self.main.handle
        start = time.time()

        while time.time() - start < timeout:
            windows = Desktop(backend="uia").windows()
            fallback_candidates: list[BaseWrapper] = []
            for win in windows:
                try:
                    if win.handle == main_handle:
                        continue
                    if not win.is_visible():
                        continue
                    cls = (win.element_info.class_name or "").strip()
                    title = (win.window_text() or "").strip()
                    is_dialog_like = cls in ("#32770", "CabinetWClass") or any(
                        key in title for key in ("打开", "Open", "文件资源管理器")
                    )
                    if not is_dialog_like:
                        continue

                    # Preferred: newly appeared dialog window.
                    if win.handle not in existing_handles:
                        return win

                    # Fallback: reused existing explorer/file-dialog window.
                    fallback_candidates.append(win)
                except Exception:
                    continue

            if fallback_candidates:
                for win in fallback_candidates:
                    try:
                        if win.is_active():
                            return win
                    except Exception:
                        continue
                return fallback_candidates[0]

            # Fallback by current top window focus.
            try:
                top = Desktop(backend="uia").top_window()
                if top.handle != main_handle and top.is_visible():
                    cls = (top.element_info.class_name or "").strip()
                    title = (top.window_text() or "").strip()
                    if cls in ("#32770", "CabinetWClass") or any(
                        key in title for key in ("打开", "Open", "文件资源管理器")
                    ):
                        return top
            except Exception:
                pass

            time.sleep(0.2)

        raise TimeoutError("Open file dialog was not detected")

    def _click_button(self, name: str, root: Optional[BaseWrapper] = None) -> None:
        """Click a button/text/tab element by visible caption.

        Args:
            name: Display text.
            root: Optional root scope for searching controls.

        Returns:
            None.
        """

        container = root or self.main
        if container is None:
            raise RuntimeError("Main window is not ready")

        candidates = []
        for control_type in ("Button", "Hyperlink", "Text", "TabItem"):
            candidates.extend(container.descendants(control_type=control_type))

        target = self._best_text_match(candidates, name)
        if target is None:
            raise MatchError(f"Cannot find control by text: {name}")

        target.click_input()
        time.sleep(0.5)

    def _click_by_text(self, name: str) -> None:
        """Click a text item globally in the main window.

        Args:
            name: Display text.

        Returns:
            None.
        """

        self._click_button(name, root=self.main)

    def _find_group(self, group_name: str) -> BaseWrapper:
        """Find group/pane region by caption.

        Args:
            group_name: Display name for target group region.

        Returns:
            BaseWrapper of the matched group region.
        """

        if self.main is None:
            raise RuntimeError("Main window is not ready")

        group_controls = []
        group_controls.extend(self.main.descendants(control_type="Group"))
        group_controls.extend(self.main.descendants(control_type="Pane"))

        target = self._best_text_match(group_controls, group_name)
        if target is None:
            raise MatchError(f"Cannot find group/pane by text: {group_name}")
        return target

    def _best_text_match(self, controls: list[BaseWrapper], target_text: str) -> Optional[BaseWrapper]:
        """Find the best matching control by normalized text.

        Args:
            controls: Candidate controls.
            target_text: Desired text.

        Returns:
            Matched control wrapper, or None if not found.
        """

        target_norm = _norm(target_text)

        # Exact normalized match first.
        for ctrl in controls:
            txt = _norm(ctrl.window_text())
            if txt and txt == target_norm:
                return ctrl

        # Partial match fallback.
        for ctrl in controls:
            txt = _norm(ctrl.window_text())
            if txt and (target_norm in txt or txt in target_norm):
                return ctrl

        return None

    def _get_log_text(self) -> str:
        """Read upgrade log text area from current UI.

        Args:
            None.

        Returns:
            Captured log text, empty string if no suitable control is found.
        """

        if self.main is None:
            return ""

        candidates = []
        candidates.extend(self.main.descendants(control_type="Document"))
        candidates.extend(self.main.descendants(control_type="Edit"))
        candidates.extend(self.main.descendants(control_type="Text"))

        # Prefer bigger text containers.
        best_text = ""
        for ctrl in candidates:
            try:
                txt = ctrl.window_text() or ""
            except Exception:
                txt = ""
            if len(txt) > len(best_text):
                best_text = txt
        return best_text

    def _get_progress_value(self) -> Optional[float]:
        """Read progress value from progress bar if available.

        Args:
            None.

        Returns:
            Progress percentage in range [0, 100], or None when unavailable.
        """

        if self.main is None:
            return None

        bars = self.main.descendants(control_type="ProgressBar")
        for bar in bars:
            try:
                iface = bar.iface_value
                raw = float(iface.CurrentValue)
                if raw <= 1.0:
                    return round(raw * 100, 2)
                return round(raw, 2)
            except Exception:
                pass

            try:
                txt = bar.window_text()
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", txt)
                if m:
                    return float(m.group(1))
            except Exception:
                pass

        return None

    def _monitor_upgrade(self) -> tuple[bool, str]:
        """Monitor upgrade progress and log messages.

        Args:
            None.

        Returns:
            Tuple of (is_success, reason).
        """

        print("[STEP] Monitoring upgrade progress and logs...")
        start = time.time()
        last_log = ""
        last_progress = -1.0
        log_file = self.config.log_output or Path.cwd() / f"upgrade_log_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"

        while time.time() - start < self.config.timeout:
            progress = self._get_progress_value()
            logs = self._get_log_text().strip()

            if progress is not None and progress != last_progress:
                print(f"[PROGRESS] {progress}%")
                last_progress = progress

            if logs and logs != last_log:
                delta = logs[len(last_log):] if logs.startswith(last_log) else logs
                print(f"[LOG] {delta[-300:]}")
                last_log = logs
                log_file.write_text(logs, encoding="utf-8", errors="ignore")

            classified = classify_upgrade_log_text(logs)
            if classified is not None:
                return classified

            if progress is not None and progress >= 100:
                return True, "progress reached 100%"

            time.sleep(self.config.poll_interval)

        return False, f"timeout after {self.config.timeout}s"


def build_arg_parser() -> argparse.ArgumentParser:
    """Create argument parser for CLI options.

    Args:
        None.

    Returns:
        Configured ArgumentParser instance.
    """

    parser = argparse.ArgumentParser(description="Automate Quectel Radar Update Tool")
    parser.add_argument(
        "--exe",
        default=r"F:\Firmware\Quectel_Radar_Update_Tool_V1.9.0.2\Quectel_Radar_Update_Tool_V1.9.0.2\Quectel_Radar_Update_Tool_V1.9.0.2.exe",
        help="Path to Quectel Radar Update Tool executable",
    )
    parser.add_argument(
        "--app-bin",
        default=r"F:\Firmware\AM102AA_P_1.01_1.001.001_CR_V01\APP.bin",
        help="Path to APP.bin firmware file",
    )
    parser.add_argument("--baudrate", default="500Kbps", help="CAN baud rate text")
    parser.add_argument("--timeout", type=int, default=1200, help="Monitor timeout in seconds")
    parser.add_argument("--poll", type=float, default=1.0, help="Monitor polling interval in seconds")
    parser.add_argument("--log-file", default="", help="Optional path for upgrade log output file")
    return parser


def main() -> int:
    """Parse CLI arguments and run automation.

    Args:
        None.

    Returns:
        Process exit code.
    """

    args = build_arg_parser().parse_args()
    config = Config(
        exe_path=Path(args.exe),
        app_bin_path=Path(args.app_bin),
        can_baudrate=args.baudrate,
        timeout=args.timeout,
        poll_interval=args.poll,
        log_output=Path(args.log_file) if args.log_file else None,
    )

    automation = RadarUpgradeAutomation(config)
    try:
        return automation.run()
    except KeyboardInterrupt:
        print("[WARN] Interrupted by user")
        return 130
    except Exception as exc:
        print(f"[ERROR] {repr(exc)}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
