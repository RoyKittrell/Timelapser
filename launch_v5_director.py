#!/usr/bin/env python3
"""
Timelapser V5 Director launcher.

Starts the Streamlit dashboard on localhost:8501, then exposes it privately
through Tailscale Serve.

Run with:
    source /home/roy/timelapser-venv/bin/activate
    python3 launch_v5_director.py
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

STREAMLIT = "/home/roy/timelapser-venv/bin/streamlit"
APP = Path("/home/roy/Timelapser Sept2026/timelapser_v5/timelapser_director_v5.py")
PORT = 8501
HOST = "127.0.0.1"
LOG = Path("/home/roy/Timelapser Sept2026/timelapser_v5/director_streamlit.log")


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def run(cmd, **kwargs):
    print("$", " ".join(map(str, cmd)))
    return subprocess.run(cmd, **kwargs)


def main():
    print("\n=== Timelapser V5 Director launcher ===\n")

    if not APP.exists():
        print(f"ERROR: Director app not found:\n  {APP}")
        sys.exit(1)

    if not Path(STREAMLIT).exists():
        print(f"ERROR: Streamlit executable not found:\n  {STREAMLIT}")
        sys.exit(1)

    # 1. Start Streamlit if it is not already listening.
    if port_open(HOST, PORT):
        print(f"Port {PORT} is already active; leaving the existing dashboard alone.")
    else:
        print(f"Starting V5 Director on http://{HOST}:{PORT} ...")
        LOG.parent.mkdir(parents=True, exist_ok=True)

        log_handle = open(LOG, "a", buffering=1)
        subprocess.Popen(
            [
                STREAMLIT,
                "run",
                str(APP),
                "--server.address", HOST,
                "--server.port", str(PORT),
                "--server.headless", "true",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(APP.parent),
        )

        # Wait up to 15 seconds for Streamlit to come up.
        for _ in range(30):
            if port_open(HOST, PORT):
                break
            time.sleep(0.5)
        else:
            print("\nERROR: Streamlit did not start.")
            print(f"Check the log with:\n  tail -n 50 '{LOG}'")
            sys.exit(1)

        print("Streamlit is up.")

    # 2. Enable/update Tailscale Serve.
    print("\nStarting Tailscale Serve...")
    result = run(
        ["sudo", "tailscale", "serve", "--bg", f"http://{HOST}:{PORT}"],
        text=True,
    )
    if result.returncode != 0:
        print("\nERROR: Tailscale Serve failed.")
        sys.exit(result.returncode)

    # 3. Show status / URL.
    print("\n=== Tailscale Serve status ===")
    run(["tailscale", "serve", "status"])

    print("\n=== Tailscale peers ===")
    run(["tailscale", "status"])

    print("\nDirector is running.")
    print(f"Local:     http://{HOST}:{PORT}")
    print(f"Streamlit log: {LOG}")
    print("\nUse the HTTPS URL shown by 'tailscale serve status' from any device on your tailnet.")


if __name__ == "__main__":
    main()
