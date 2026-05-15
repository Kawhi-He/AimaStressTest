# -*- coding: utf-8 -*-
"""Demo script to toggle programmable power supply between 0V and 12V.

Author: Kawhi.He
"""

from __future__ import annotations

import argparse
import time

from programmable_power import ItechIt6121B


def run_demo(
    port: str,
    baudrate: int,
    current_limit: float,
    interval_s: float,
    duration_s: float,
) -> None:
    """Run voltage toggle demo with fixed interval and total duration.

    Author: Kawhi.He

    Args:
        port (str): Serial port of programmable power supply, such as "COM25".
        baudrate (int): Serial baudrate used by programmable power supply.
        current_limit (float): Output current limit in amperes.
        interval_s (float): Voltage switch interval in seconds.
        duration_s (float): Total demo run duration in seconds.

    Returns:
        None.
    """
    psu = ItechIt6121B(port=port, baudrate=baudrate)
    start = time.monotonic()
    next_switch = start
    low_state = True

    try:
        psu.connect()
        psu.output_on()

        # Start from 0V, then switch to 12V every interval.
        while True:
            now = time.monotonic()
            elapsed = now - start
            if elapsed >= duration_s:
                break

            if now >= next_switch:
                target_voltage = 0.0 if low_state else 12.0
                psu.configure_output(
                    voltage=target_voltage,
                    current=current_limit,
                )
                psu.log(
                    "[DEMO] Elapsed "
                    f"{elapsed:.1f}s/{duration_s:.1f}s, "
                    f"set voltage to {target_voltage:.1f}V"
                )
                low_state = not low_state
                next_switch += interval_s

            time.sleep(0.1)
    finally:
        try:
            psu.configure_output(voltage=0.0, current=current_limit)
            psu.output_off()
        finally:
            psu.close()


def build_parser() -> argparse.ArgumentParser:
    """Create command line argument parser for the demo.

    Author: Kawhi.He

    Args:
        None.

    Returns:
        argparse.ArgumentParser: Configured parser instance.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Toggle programmable power supply voltage "
            "between 0V and 12V."
        )
    )
    parser.add_argument(
        "--port",
        required=True,
        help="Serial port, for example COM25",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=115200,
        help="Serial baudrate of programmable power supply",
    )
    parser.add_argument(
        "--current",
        type=float,
        default=3.0,
        help="Current limit in A",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Switch interval in seconds",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Total run time in seconds",
    )
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    run_demo(
        port=args.port,
        baudrate=args.baudrate,
        current_limit=args.current,
        interval_s=args.interval,
        duration_s=args.duration,
    )
