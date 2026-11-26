"""Quiet scheduled-task entry point with a local crash log."""
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import traceback

app_path = Path(__file__).resolve().parent
log_path = app_path / "control_server.log"
with log_path.open("a", encoding="utf-8", buffering=1) as log:
    sys.stdout = log
    sys.stderr = log
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 手机控制台后台启动", flush=True)
    if not (app_path / 'pairing_verifier.json').is_file():
        print('配对尚未初始化；请在电脑上手动启动控制台。后台不会生成配对码。', flush=True)
        raise SystemExit(1)
    stop_relay = threading.Event()
    relay_process = None

    def supervise_relay():
        global relay_process
        node = shutil.which("node")
        while not stop_relay.is_set():
            enabled = (app_path / "remote_relay.json").is_file()
            if enabled and node and (relay_process is None or relay_process.poll() is not None):
                relay_process = subprocess.Popen(
                    [node, str(app_path / "remote_bridge.mjs")],
                    cwd=str(app_path), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            elif not enabled and relay_process is not None and relay_process.poll() is None:
                relay_process.terminate()
                relay_process = None
            stop_relay.wait(10)

    relay_thread = threading.Thread(target=supervise_relay, daemon=True)
    relay_thread.start()
    try:
        from server import main
        # A missing verifier requires an explicit foreground initialization.
        # Background stdout is a log and must never receive a new pairing secret.
        result = main(['--background'])
    except Exception:
        traceback.print_exc(file=log)
        raise
    finally:
        stop_relay.set()
        if relay_process is not None and relay_process.poll() is None:
            relay_process.terminate()
    raise SystemExit(result)
