"""Start the local phone-control server if its port is not listening."""
from pathlib import Path
import socket
import subprocess
import sys


APP = Path(__file__).resolve().parent


def is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1):
            return True
    except OSError:
        return False


if not is_running():
    subprocess.Popen(
        [sys.executable, str(APP / "run_background.pyw")],
        cwd=str(APP),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
