"""Install or remove persistent per-user macOS LaunchAgents for quote collection."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JOBS = {"research-cycle": "com.thetahawk.options-collector"}
LEGACY_LABELS = ("com.thetahawk.earnings-options-collector",)


def _agent_payload(job: str, label: str) -> dict[str, object]:
    data_dir = ROOT / "data"
    log_name = "options" if job == "research-cycle" else job
    return {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            "-u",
            "-m",
            "thetahawk.scheduled_collection",
            "--job",
            job,
            "--interval-seconds",
            "900",
            *(["--options-history-min-dte", "14", "--options-history-max-dte", "75"]
              if job in {"options", "options-and-earnings", "research-cycle"} else []),
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(data_dir / f"{log_name}.log"),
        "StandardErrorPath": str(data_dir / f"{log_name}.err.log"),
    }


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def install() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent collection is available only on macOS")
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    for label in LEGACY_LABELS:
        plist_path = launch_agents / f"{label}.plist"
        _launchctl("bootout", domain, str(plist_path), check=False)
        if plist_path.exists():
            plist_path.unlink()
        _launchctl("disable", f"{domain}/{label}", check=False)
    for job, label in JOBS.items():
        plist_path = launch_agents / f"{label}.plist"
        with plist_path.open("wb") as stream:
            plistlib.dump(_agent_payload(job, label), stream)
        _launchctl("bootout", domain, str(plist_path), check=False)
        _launchctl("enable", f"{domain}/{label}")
        _launchctl("bootstrap", domain, str(plist_path))
        print(f"Installed {label} ({job}, 15-minute interval).")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    for label in (*JOBS.values(), *LEGACY_LABELS):
        plist_path = launch_agents / f"{label}.plist"
        _launchctl("bootout", domain, str(plist_path), check=False)
        if plist_path.exists():
            plist_path.unlink()
        _launchctl("disable", f"{domain}/{label}", check=False)
        print(f"Removed {label}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--uninstall", action="store_true", help="Stop and remove collection LaunchAgents")
    action.add_argument("--status", action="store_true", help="Show launchd status for collection agents")
    args = parser.parse_args()
    if args.uninstall:
        uninstall()
    elif args.status:
        domain = f"gui/{os.getuid()}"
        for label in (*JOBS.values(), *LEGACY_LABELS):
            result = _launchctl("print", f"{domain}/{label}", check=False)
            if result.returncode == 0:
                print(result.stdout.strip())
            else:
                print(f"{label}: not loaded")
    else:
        install()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
