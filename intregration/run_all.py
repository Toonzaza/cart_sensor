#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartCart node runner (ordered, resilient).

Order:
  1) led_actuator.py
  2) server.py or main_server.py (first found)
  3) match_id.py
  4) communicate_AMR.py
  5) main_sensor.py

- Skips missing scripts with a warning (keeps others running)
- Logs per node to ./logs/<name>.log
- Graceful shutdown on Ctrl+C / SIGTERM
"""

import os, sys, time, signal, subprocess, pathlib, socket

# ===== CONFIG =====
PROJECT_DIR = pathlib.Path(os.path.expanduser("~/cart_ws/intregration"))
PYTHON_BIN  = os.environ.get("PYTHON_BIN", "/home/fibo/cart_env/bin/python")
MQTT_HOST   = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT   = int(os.environ.get("MQTT_PORT", "1883"))

# auto-pick server entry (server.py preferred, fallback main_server.py)
SERVER_SCRIPT = "server.py" if (PROJECT_DIR / "server.py").exists() else (
                "main_server.py" if (PROJECT_DIR / "main_server.py").exists() else None)

# ordered nodes (name, script)
ORDER = [
    ("led_actuator",     "led_actuator.py"),
    ("server",           SERVER_SCRIPT),     # may be None -> skipped
    ("match_id",         "match_id.py"),
    ("communicate_AMR",  "communicate_AMR.py"),
    ("main_sensor",      "main_sensor.py"),
]

LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
PROCS = []  # [(name, Popen)]

def _log_path(name: str) -> pathlib.Path:
    return LOG_DIR / f"{name}.log"

def check_mqtt(host: str, port: int, timeout=1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def start_node(name: str, script: str):
    if not script:
        print(f"[RUNNER] - skip {name}: script not specified")
        return None
    script_path = PROJECT_DIR / script
    if not script_path.exists():
        print(f"[RUNNER] - skip {name}: {script} not found")
        return None

    # open log in append binary mode (unbuffered)
    logf = open(_log_path(name), "ab", buffering=0)

    env = os.environ.copy()
    env.setdefault("MQTT_HOST", MQTT_HOST)
    env.setdefault("MQTT_PORT", str(MQTT_PORT))
    # led_actuator uses gpiozero; ensure lgpio backend on Pi 5
    env.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")

    cmd = [PYTHON_BIN, str(script_path)]
    print(f"[RUNNER] ▶ {name:16s} -> {' '.join(cmd)}  (log: {_log_path(name)})")
    p = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=logf,
        stderr=subprocess.STDOUT,
        env=env,
        preexec_fn=os.setsid  # own process group
    )
    return p

def stop_all():
    for name, p in PROCS:
        try:
            print(f"[RUNNER] ◀ stopping {name} (pid={p.pid})")
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass

    # wait a bit
    t0 = time.time()
    while time.time() - t0 < 5:
        if not any(p.poll() is None for _, p in PROCS):
            break
        time.sleep(0.1)

    # force kill leftovers
    for name, p in PROCS:
        if p.poll() is None:
            try:
                print(f"[RUNNER] !! kill -9 {name} (pid={p.pid})")
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass

def sig_handler(signum, frame):
    print(f"\n[RUNNER] caught signal {signum}, stopping all...")
    stop_all()
    sys.exit(0)

def main():
    # signals
    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # sanity checks
    if not pathlib.Path(PYTHON_BIN).exists():
        print(f"[RUNNER] ! PYTHON_BIN not found: {PYTHON_BIN}")
        sys.exit(1)

    if check_mqtt(MQTT_HOST, MQTT_PORT):
        print(f"[RUNNER] MQTT broker OK at {MQTT_HOST}:{MQTT_PORT}")
    else:
        print(f"[RUNNER] WARNING: MQTT {MQTT_HOST}:{MQTT_PORT} not reachable. Start mosquitto first.")

    # start nodes in order
    global PROCS
    PROCS = []
    for name, script in ORDER:
        p = start_node(name, script)
        if p:
            PROCS.append((name, p))
            # small gap per node so subscribers are ready
            time.sleep(0.8)

    if not PROCS:
        print("[RUNNER] nothing started. Check ORDER and scripts.")
        sys.exit(1)

    print("[RUNNER] all requested nodes started. Press Ctrl+C to stop.")
    print("         logs in:", LOG_DIR)

    # monitor: if any process exits -> stop all (simple & safe for dev)
    while True:
        for name, p in PROCS:
            code = p.poll()
            if code is not None:
                print(f"[RUNNER] !! process '{name}' exited with code {code}. Stopping all.")
                stop_all()
                sys.exit(code if code is not None else 1)
        time.sleep(0.5)

if __name__ == "__main__":
    main()
