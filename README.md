# AimaStressTest

Windows automation scripts for Quectel radar upgrade/capture stress testing.

## Overview

This repository contains end-to-end automation for:

- Radar firmware upgrade via Quectel update tool UI automation.
- Power-cycle verification through a programmable power supply.
- Radar point-cloud recording and health check after each round.
- Stress loop execution with unified logs and CSV summary output.

Primary target environment is Windows with GUI tools available.

## Repository Structure

- `radar_upgrade_automation.py`
	- Automates Quectel Radar Update Tool workflow:
		- Open tool
		- Configure CAN baudrate
		- Open device/CAN
		- Select `APP.bin`
		- Start upgrade and monitor result

- `aima_record_test.py`
	- Automates radar recording tool workflow and validates point-cloud output.

- `aima_swj_upgrade_stress.py`
	- Main stress entrypoint.
	- Runs upgrade -> capture check -> power cycle -> statistics loop.
	- Writes detailed logs and CSV summary into `stress_logs/`.

- `programmable_power.py`
	- SCPI serial wrapper for ITECH IT6121B programmable power supply.

- `demo_power_toggle.py`
	- Demo utility to toggle PSU voltage between 0V and 12V.

- `radar_can_tool.py`
	- Low-level UI automation helper for radar CAN/recording tool interaction.

## Requirements

## Software

- Windows 10/11
- Python 3.10+
- Quectel Radar Update Tool (installed locally)
- Quectel Radar recording tool (installed locally)

## Python Packages

- `pywinauto`
- `psutil`
- `pywin32`
- `pyserial`

Install dependencies:

```bash
pip install pywinauto psutil pywin32 pyserial
```

## Hardware

- Radar device under test
- CAN environment required by update/record tools
- ITECH IT6121B programmable power supply (or compatible serial SCPI device)

## Quick Start

Before running, edit the path/config constants in scripts if your local setup differs.

## 1) Run single upgrade automation

```bash
python radar_upgrade_automation.py \
	--exe "F:\\Firmware\\Quectel_Radar_Update_Tool_V1.9.0.2\\Quectel_Radar_Update_Tool_V1.9.0.2\\Quectel_Radar_Update_Tool_V1.9.0.2.exe" \
	--app-bin "F:\\Firmware\\AM102AA_P_1.01_1.001.001_CR_V01\\APP.bin" \
	--baudrate 500Kbps \
	--timeout 1200 \
	--poll 1.0
```

Optional:

- `--log-file <path>`: write captured upgrade logs to a file.

## 2) Run single capture validation

```bash
python aima_record_test.py
```

Expected pass condition: generated `frame.txt` contains at least one point-cloud line.

## 3) Run stress test loop

```bash
python aima_swj_upgrade_stress.py --rounds 100 --upgrade-timeout 1200 --capture-timeout 300
```

Common quick check:

```bash
python aima_swj_upgrade_stress.py --rounds 2
```

## Output

- Stress logs directory: `stress_logs/`
- Per-round upgrade output: `upgrade_tool_round_XXX_*.txt`
- Summary CSV: `stress_summary_YYYYMMDD_HHMMSS.csv`

Final round result is evaluated by:

- Upgrade success
- Power-cycle verification success
- Point-cloud detection success

Round is `PASS` only when all three are true; otherwise `FAIL`.

## Notes

- These scripts rely on UI automation. Keep target windows visible and avoid manual interference during execution.
- Administrator privileges may be required depending on tool installation and serial/CAN access.
- If UI labels differ across tool versions, update selectors in automation scripts accordingly.

## Git Ignore Policy

Local environment and generated runtime artifacts are ignored via `.gitignore`, including:

- `.venv/`
- `__pycache__/`
- `stress_logs/`