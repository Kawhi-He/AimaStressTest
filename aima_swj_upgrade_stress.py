#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Author: Kawhi.He

脚本说明（AIMA SWJ 升级压测）：
1. 调用升级自动化脚本执行雷达升级。
2. 升级完成后，关闭升级工具，控制程控电源对雷达执行下电/上电。
3. 调用抓 log 工具录制 frame.txt，并分析是否包含点云数据。
4. 每一轮输出详细日志（时间戳/日志等级/代码行号），并将升级工具输出合并到统一日志。
5. 生成统计表：升级是否成功、上下电是否成功、雷达是否正常（按点云判断）。
6. 默认循环 100 次，次数可配置。

使用方式：
1. 修改本文件开头“可配置参数”区域。
2. 运行：python aima_swj_upgrade_stress.py
3. 临时改轮次：python aima_swj_upgrade_stress.py --rounds 2
"""

from __future__ import annotations

import argparse
import ctypes
import csv
import datetime as dt
import locale
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from programmable_power import ItechIt6121B


# =============================================================================
# 可配置参数（请按需修改）
# =============================================================================
# 压测次数（默认 100）
STRESS_TOTAL_ROUNDS = 100

# 升级脚本与升级资源
UPGRADE_SCRIPT_PATH = Path(__file__).with_name("radar_upgrade_automation.py")
UPGRADE_TOOL_EXE_PATH = Path(
    r"F:\Firmware\Quectel_Radar_Update_Tool_V1.9.0.2\Quectel_Radar_Update_Tool_V1.9.0.2\Quectel_Radar_Update_Tool_V1.9.0.2.exe"
)
UPGRADE_FIRMWARE_APP_BIN_PATH = Path(r"F:\Firmware\AM102AA_P_1.01_1.001.001_CR_V01\APP.bin")
UPGRADE_CAN_BAUDRATE = "500Kbps"
UPGRADE_MONITOR_TIMEOUT_S = 1200

# 抓 log（点云）脚本
CAPTURE_SCRIPT_PATH = Path(__file__).with_name("aima_record_test.py")
CAPTURE_TIMEOUT_S = 300

# frame.txt 搜索范围（可按需追加路径）
FRAME_SEARCH_ROOTS = [Path(__file__).parent]

# 程控电源参数（当前口：COM25）
POWER_PORT = "COM25"
POWER_BAUDRATE = 115200
POWER_TIMEOUT_S = 1.0
POWER_VOLTAGE_V = 12.0
POWER_CURRENT_A = 3.0
POWER_OFF_WAIT_S = 3.0
POWER_ON_WAIT_S = 3.0
POWER_OFF_MAX_VOLTAGE_V = 1.0
POWER_ON_MIN_VOLTAGE_V = 10.0
POST_CAPTURE_WAIT_BEFORE_OPEN_TOOL_S = 3.0

# 轮次间隔
ROUND_INTERVAL_S = 2.0


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


@dataclass
class RoundResult:
    """Store one round execution result.

    Args:
        round_idx: Round index starting from 1.
        upgrade_ok: Whether upgrade step succeeded.
        power_cycle_ok: Whether power cycle verification succeeded.
        radar_ok: Whether point cloud data was detected.
        final_result: PASS or FAIL.
        frame_path: frame.txt path if found.
        reason: Detailed result reason.

    Returns:
        None.
    """

    round_idx: int
    upgrade_ok: bool
    power_cycle_ok: bool
    radar_ok: bool
    final_result: str
    frame_path: str
    reason: str


def setup_logger(log_dir: Path) -> tuple[logging.Logger, Path]:
    """Create unified logger with file and console handlers.

    Args:
        log_dir: Directory where log files are written.

    Returns:
        tuple[logging.Logger, Path]: Logger object and main log file path.
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"aima_swj_upgrade_stress_{dt.datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("aima_swj_upgrade_stress")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | L%(lineno)04d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger, log_file


def enable_windows_no_sleep(logger: logging.Logger) -> bool:
    """Prevent Windows from sleeping while this script is running.

    Args:
        logger: Logger instance.

    Returns:
        bool: True if no-sleep state was applied; otherwise False.
    """

    if sys.platform != "win32":
        logger.info("No-sleep guard skipped: current platform is not Windows")
        return False

    state = ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    )
    if state == 0:
        logger.warning("Failed to enable no-sleep guard on Windows")
        return False

    logger.info("No-sleep guard enabled (system + display stay awake)")
    return True


def disable_windows_no_sleep(logger: logging.Logger) -> None:
    """Restore default Windows sleep policy for current thread.

    Args:
        logger: Logger instance.

    Returns:
        None.
    """

    if sys.platform != "win32":
        return

    state = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    if state == 0:
        logger.warning("Failed to restore Windows sleep policy")
        return

    logger.info("No-sleep guard disabled; Windows sleep policy restored")


def run_subprocess_with_logs(
    cmd: list[str],
    logger: logging.Logger,
    tag: str,
    timeout_s: int,
) -> tuple[int, str, bool]:
    """Run subprocess and forward all outputs to unified logger.

    Args:
        cmd: Subprocess command.
        logger: Logger instance.
        tag: Log prefix tag.
        timeout_s: Process timeout in seconds.

    Returns:
        tuple[int, str, bool]: (return code, merged output, timed_out).
    """

    logger.info("[%s] CMD: %s", tag, " ".join(cmd))

    def decode_output(raw: bytes | str | None) -> str:
        """Decode subprocess output with Windows-friendly fallback encodings.

        Args:
            raw: Raw subprocess output bytes or text.

        Returns:
            str: Decoded text.
        """

        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw

        encodings = ["utf-8", "gb18030", locale.getpreferredencoding(False)]
        seen: set[str] = set()
        for enc in encodings:
            enc_norm = (enc or "").strip().lower()
            if not enc_norm or enc_norm in seen:
                continue
            seen.add(enc_norm)
            try:
                return raw.decode(enc_norm)
            except UnicodeDecodeError:
                continue

        return raw.decode("utf-8", errors="replace")

    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            timeout=timeout_s,
            check=False,
        )
        stdout_text = decode_output(cp.stdout)
        stderr_text = decode_output(cp.stderr)
        merged = (stdout_text + "\n" + stderr_text).strip()
        if merged:
            for line in merged.splitlines():
                logger.info("[%s] %s", tag, line)
        return cp.returncode, merged, False
    except subprocess.TimeoutExpired as exc:
        out = decode_output(exc.stdout)
        err = decode_output(exc.stderr)
        merged = (out + "\n" + err).strip()
        if merged:
            for line in merged.splitlines():
                logger.warning("[%s] %s", tag, line)
        logger.error("[%s] Process timeout after %ss", tag, timeout_s)
        return 124, merged, True


def _norm_text(text: str) -> str:
    """Normalize text for robust keyword matching.

    Args:
        text: Input text.

    Returns:
        str: Normalized lower-case text without whitespaces.
    """

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


def classify_upgrade_log_result(log_text: str) -> tuple[bool, str] | None:
    """Classify upgrade result from tool log text.

    Args:
        log_text: Upgrade tool log text.

    Returns:
        tuple[bool, str] | None: (upgrade_ok, reason) if classified; otherwise None.
    """

    norm = _norm_text(log_text)
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
        return True, "Upgrade tool log contains success marker"
    return False, "Upgrade tool log contains failure marker"


def kill_process_by_image(image_name: str, logger: logging.Logger) -> None:
    """Force-close all processes by image name.

    Args:
        image_name: Executable image name.
        logger: Logger instance.

    Returns:
        None.
    """

    cmd = ["taskkill", "/F", "/T", "/IM", image_name]
    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if cp.returncode == 0:
        logger.info("Closed process image: %s", image_name)
    else:
        logger.info("No running process to close: %s", image_name)


def run_upgrade_once(
    python_exe: str,
    logger: logging.Logger,
    log_dir: Path,
    round_idx: int,
    upgrade_timeout_s: int,
) -> tuple[bool, str]:
    """Run upgrade automation once and evaluate result.

    Args:
        python_exe: Python executable path.
        logger: Logger instance.
        log_dir: Log directory.
        round_idx: Current round index.

    Returns:
        tuple[bool, str]: (upgrade_ok, reason).
    """

    if not UPGRADE_SCRIPT_PATH.exists():
        return False, f"Upgrade script not found: {UPGRADE_SCRIPT_PATH}"
    if not UPGRADE_TOOL_EXE_PATH.exists():
        return False, f"Upgrade tool not found: {UPGRADE_TOOL_EXE_PATH}"
    if not UPGRADE_FIRMWARE_APP_BIN_PATH.exists():
        return False, f"APP.bin not found: {UPGRADE_FIRMWARE_APP_BIN_PATH}"

    upgrade_tool_log = log_dir / f"upgrade_tool_round_{round_idx:03d}_{dt.datetime.now():%H%M%S}.txt"
    cmd = [
        python_exe,
        str(UPGRADE_SCRIPT_PATH),
        "--exe",
        str(UPGRADE_TOOL_EXE_PATH),
        "--app-bin",
        str(UPGRADE_FIRMWARE_APP_BIN_PATH),
        "--baudrate",
        UPGRADE_CAN_BAUDRATE,
        "--timeout",
        str(upgrade_timeout_s),
        "--poll",
        "1.0",
        "--log-file",
        str(upgrade_tool_log),
    ]

    code, output, timed_out = run_subprocess_with_logs(
        cmd,
        logger,
        "UPGRADE",
        upgrade_timeout_s + 120,
    )
    kill_process_by_image(UPGRADE_TOOL_EXE_PATH.name, logger)

    if upgrade_tool_log.exists():
        try:
            log_text = upgrade_tool_log.read_text(encoding="utf-8", errors="replace")
            classified = classify_upgrade_log_result(log_text)
            if classified is not None:
                return classified
        except OSError as exc:
            logger.warning("Cannot read upgrade tool log file: %s", exc)

    if timed_out:
        return False, "Upgrade process timeout"
    if code == 0:
        return True, "Upgrade script exit code is 0"

    if "success=True" in output:
        return True, "Upgrade output contains success=True"
    return False, f"Upgrade failed, return code={code}"


def power_cycle_once(psu: ItechIt6121B, logger: logging.Logger) -> tuple[bool, str]:
    """Power off/on radar and verify electrical state.

    Args:
        psu: Connected programmable power controller.
        logger: Logger instance.

    Returns:
        tuple[bool, str]: (power_cycle_ok, reason).
    """

    try:
        # Power-off phase: explicitly force output voltage to 0V.
        psu.configure_output(voltage=0.0, current=POWER_CURRENT_A)
        psu.output_on()
        time.sleep(POWER_OFF_WAIT_S)
        off_v = psu.read_actual_voltage()

        # Power-on phase: restore nominal supply voltage.
        psu.configure_output(voltage=POWER_VOLTAGE_V, current=POWER_CURRENT_A)
        psu.output_on()
        time.sleep(POWER_ON_WAIT_S)
        on_v = psu.read_actual_voltage()

        logger.info("Power verify voltage: off=%sV, on=%sV", off_v, on_v)

        if off_v is None or on_v is None:
            return False, "Cannot read voltage from power supply"

        off_ok = off_v <= POWER_OFF_MAX_VOLTAGE_V
        on_ok = on_v >= POWER_ON_MIN_VOLTAGE_V
        if off_ok and on_ok:
            return True, f"Power cycle verified (off={off_v:.3f}V, on={on_v:.3f}V)"

        return False, (
            f"Power verify failed: off={off_v:.3f}V (<= {POWER_OFF_MAX_VOLTAGE_V}), "
            f"on={on_v:.3f}V (>= {POWER_ON_MIN_VOLTAGE_V})"
        )
    except Exception as exc:
        return False, f"Power cycle exception: {exc!r}"


def recovery_power_cycle_after_failure(
    psu: ItechIt6121B,
    logger: logging.Logger,
) -> tuple[bool, str]:
    """Run a recovery power cycle after a failed round.

    Args:
        psu: Connected programmable power controller.
        logger: Logger instance.

    Returns:
        tuple[bool, str]: (recovery_ok, reason).
    """

    logger.warning(
        "Round failed; running recovery power cycle with default %.1fV",
        POWER_VOLTAGE_V,
    )
    ok, reason = power_cycle_once(psu, logger)
    if ok:
        logger.info("Recovery power cycle finished: %s", reason)
    else:
        logger.error("Recovery power cycle failed: %s", reason)
    return ok, reason


def open_upgrade_tool_once(logger: logging.Logger) -> tuple[bool, str]:
    """Open upgrade tool executable once.

    Args:
        logger: Logger instance.

    Returns:
        tuple[bool, str]: (opened_ok, reason).
    """

    if not UPGRADE_TOOL_EXE_PATH.exists():
        return False, f"Upgrade tool not found: {UPGRADE_TOOL_EXE_PATH}"

    try:
        subprocess.Popen([str(UPGRADE_TOOL_EXE_PATH)])
        logger.info("Opened upgrade tool: %s", UPGRADE_TOOL_EXE_PATH)
        return True, "Upgrade tool opened"
    except OSError as exc:
        return False, f"Open upgrade tool failed: {exc!r}"


def find_latest_frame_txt() -> Path | None:
    """Find the newest frame.txt from configured search roots.

    Args:
        None.

    Returns:
        Path | None: Newest frame.txt path, or None if not found.
    """

    candidates: list[tuple[float, Path]] = []
    for root in FRAME_SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("frame.txt"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def is_numeric(text: str) -> bool:
    """Check whether input text can be parsed as float.

    Args:
        text: Input text token.

    Returns:
        bool: True if numeric; otherwise False.
    """

    try:
        float(text)
        return True
    except ValueError:
        return False


def analyze_frame_txt(frame_path: Path) -> tuple[bool, str]:
    """Analyze frame.txt and determine if point cloud exists.

    Args:
        frame_path: Path to frame.txt.

    Returns:
        tuple[bool, str]: (radar_ok, reason).
    """

    if not frame_path.exists():
        return False, f"frame.txt not found: {frame_path}"
    if frame_path.stat().st_size == 0:
        return False, "frame.txt is empty"

    pc_lines = 0
    in_point_section = False
    point_num_declared = 0
    with frame_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or s.lower().startswith("frame"):
                continue

            # Track sections in frame.txt to avoid counting non-point lines.
            if s == "[Point]":
                in_point_section = True
                continue
            if s in ("[HEAD]", "[Object]"):
                in_point_section = False

            # Accept declared point count as an additional signal.
            if s.startswith("PointNum="):
                _, _, val = s.partition("=")
                if val.isdigit():
                    point_num_declared += int(val)
                continue

            # Current frame format is key=value style, for example:
            # 0:Range=2.7 Velocity=-65.4 AngleAZ=-0.35 AngleEL=0 RCS=0
            if in_point_section and ("Range=" in s and "Velocity=" in s):
                pc_lines += 1
                continue

            tokens = s.split()
            if sum(1 for t in tokens if is_numeric(t)) >= 4:
                pc_lines += 1

    if pc_lines > 0:
        return True, f"Point cloud lines detected: {pc_lines}"
    if point_num_declared > 0:
        return True, f"Point cloud declared by PointNum: {point_num_declared}"
    return False, "No point cloud lines detected"


def run_capture_and_analyze_once(
    python_exe: str,
    logger: logging.Logger,
    capture_timeout_s: int,
) -> tuple[bool, str, str]:
    """Run capture tool once, then analyze newest frame.txt.

    Args:
        python_exe: Python executable path.
        logger: Logger instance.

    Returns:
        tuple[bool, str, str]: (radar_ok, reason, frame_path_text).
    """

    if not CAPTURE_SCRIPT_PATH.exists():
        return False, f"Capture script not found: {CAPTURE_SCRIPT_PATH}", ""

    cmd = [python_exe, str(CAPTURE_SCRIPT_PATH)]
    code, _output, timed_out = run_subprocess_with_logs(
        cmd,
        logger,
        "CAPTURE",
        capture_timeout_s,
    )
    if timed_out:
        return False, "Capture process timeout", ""
    if code not in (0, 1):
        return False, f"Capture tool exit code={code}", ""

    frame_path = find_latest_frame_txt()
    if frame_path is None:
        return False, "frame.txt not found", ""

    radar_ok, reason = analyze_frame_txt(frame_path)
    return radar_ok, reason, str(frame_path)


def write_round_csv(csv_path: Path, rows: list[RoundResult]) -> None:
    """Write all round results to CSV file.

    Args:
        csv_path: Target CSV path.
        rows: Round result rows.

    Returns:
        None.
    """

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "round",
                "upgrade_ok",
                "power_cycle_ok",
                "radar_ok",
                "final_result",
                "frame_path",
                "reason",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.round_idx,
                    row.upgrade_ok,
                    row.power_cycle_ok,
                    row.radar_ok,
                    row.final_result,
                    row.frame_path,
                    row.reason,
                ]
            )


def format_results_table(rows: list[RoundResult]) -> str:
    """Build human-readable plain text table for round results.

    Args:
        rows: Round results.

    Returns:
        str: Formatted table text.
    """

    headers = ["Round", "Upgrade", "PowerCycle", "Radar", "Result", "Reason"]
    matrix = []
    for r in rows:
        matrix.append(
            [
                str(r.round_idx),
                "OK" if r.upgrade_ok else "FAIL",
                "OK" if r.power_cycle_ok else "FAIL",
                "OK" if r.radar_ok else "FAIL",
                r.final_result,
                r.reason,
            ]
        )

    widths = [len(h) for h in headers]
    for row in matrix:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(cells[i].ljust(widths[i]) for i in range(len(cells)))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    lines.extend(fmt_row(r) for r in matrix)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Create command line parser.

    Args:
        None.

    Returns:
        argparse.ArgumentParser: Parser instance.
    """

    parser = argparse.ArgumentParser(description="AIMA SWJ upgrade stress test")
    parser.add_argument("--rounds", type=int, default=STRESS_TOTAL_ROUNDS, help="Stress rounds")
    parser.add_argument(
        "--upgrade-timeout",
        type=int,
        default=UPGRADE_MONITOR_TIMEOUT_S,
        help="Upgrade monitor timeout (seconds)",
    )
    parser.add_argument(
        "--capture-timeout",
        type=int,
        default=CAPTURE_TIMEOUT_S,
        help="Capture timeout (seconds)",
    )
    return parser


def main() -> int:
    """Run AIMA SWJ stress test loop.

    Args:
        None.

    Returns:
        int: Process exit code.
    """

    args = build_parser().parse_args()
    rounds = max(1, args.rounds)
    upgrade_timeout_s = max(30, args.upgrade_timeout)
    capture_timeout_s = max(30, args.capture_timeout)

    py_exe = sys.executable
    base_dir = Path(__file__).parent
    log_dir = base_dir / "stress_logs"
    logger, main_log_path = setup_logger(log_dir)
    csv_path = log_dir / f"stress_summary_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"

    logger.info("=" * 80)
    logger.info("AIMA SWJ upgrade stress started")
    logger.info("Python executable: %s", py_exe)
    logger.info("Total rounds: %s", rounds)
    logger.info("Upgrade timeout: %ss", upgrade_timeout_s)
    logger.info("Capture timeout: %ss", capture_timeout_s)
    logger.info("Main log file: %s", main_log_path)
    logger.info("Summary csv: %s", csv_path)
    logger.info("=" * 80)

    no_sleep_enabled = enable_windows_no_sleep(logger)

    try:
        psu = ItechIt6121B(
            port=POWER_PORT,
            baudrate=POWER_BAUDRATE,
            timeout=POWER_TIMEOUT_S,
            logger=logger,
        )

        try:
            psu.connect()
        except Exception as exc:
            logger.error("Power supply connect failed: %r", exc)
            return 2

        results: list[RoundResult] = []
        try:
            for i in range(1, rounds + 1):
                logger.info("\n")
                logger.info("========== ROUND %03d / %03d ==========", i, rounds)

                upgrade_ok, upgrade_reason = run_upgrade_once(
                    py_exe,
                    logger,
                    log_dir,
                    i,
                    upgrade_timeout_s,
                )
                logger.info("Round %03d upgrade: %s (%s)", i, upgrade_ok, upgrade_reason)

                frame_path = ""
                if not upgrade_ok:
                    # Requirement: if upgrade fails, skip point-cloud capture and
                    # mark this round as FAIL directly.
                    power_ok = False
                    power_reason = "Skipped post-capture power cycle because upgrade failed"
                    radar_ok = False
                    radar_reason = "Skipped capture because upgrade failed"
                    logger.warning("Round %03d: skip capture because upgrade failed", i)
                else:
                    radar_ok, radar_reason, frame_path = run_capture_and_analyze_once(
                        py_exe,
                        logger,
                        capture_timeout_s,
                    )
                    logger.info("Round %03d radar check: %s (%s)", i, radar_ok, radar_reason)

                    kill_process_by_image(UPGRADE_TOOL_EXE_PATH.name, logger)

                    power_ok, power_reason = power_cycle_once(psu, logger)
                    logger.info("Round %03d power cycle: %s (%s)", i, power_ok, power_reason)

                    logger.info(
                        "Round %03d wait %.1fs before reopening upgrade tool",
                        i,
                        POST_CAPTURE_WAIT_BEFORE_OPEN_TOOL_S,
                    )
                    time.sleep(POST_CAPTURE_WAIT_BEFORE_OPEN_TOOL_S)

                    reopen_ok, reopen_reason = open_upgrade_tool_once(logger)
                    if reopen_ok:
                        logger.info("Round %03d reopen upgrade tool: %s", i, reopen_reason)
                    else:
                        logger.warning("Round %03d reopen upgrade tool: %s", i, reopen_reason)

                final_ok = upgrade_ok and power_ok and radar_ok
                final_result = "PASS" if final_ok else "FAIL"
                reason = f"upgrade={upgrade_reason}; power={power_reason}; radar={radar_reason}"

                result = RoundResult(
                    round_idx=i,
                    upgrade_ok=upgrade_ok,
                    power_cycle_ok=power_ok,
                    radar_ok=radar_ok,
                    final_result=final_result,
                    frame_path=frame_path,
                    reason=reason,
                )
                results.append(result)
                write_round_csv(csv_path, results)

                logger.info("Round %03d FINAL: %s", i, final_result)
                logger.info("Latest frame: %s", frame_path if frame_path else "N/A")

                if final_result == "FAIL":
                    recovery_power_cycle_after_failure(psu, logger)

                if i < rounds:
                    time.sleep(ROUND_INTERVAL_S)
        finally:
            try:
                psu.close()
            except Exception:
                pass

        pass_count = sum(1 for r in results if r.final_result == "PASS")
        fail_count = len(results) - pass_count
        logger.info("=" * 80)
        logger.info("Stress finished: total=%d, pass=%d, fail=%d", len(results), pass_count, fail_count)
        logger.info("Summary table:\n%s", format_results_table(results))
        logger.info("=" * 80)

        return 0 if fail_count == 0 else 1
    finally:
        if no_sleep_enabled:
            disable_windows_no_sleep(logger)


if __name__ == "__main__":
    sys.exit(main())
