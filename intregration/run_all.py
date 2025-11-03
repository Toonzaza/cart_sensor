#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, time, signal, pathlib, subprocess, re
from collections import deque
from typing import Optional, Dict, Any, Set
import paho.mqtt.client as mqtt

# ---------- PATH/CONFIG ----------
BASE_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

DATA_DIR        = pathlib.Path(os.path.expanduser("~/cart_ws/intregration/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH      = DATA_DIR / "state.json"       # main_server เขียนไฟล์นี้ (และฝัง op)
FSM_STATE_PATH  = DATA_DIR / "fsm_state.json"   # orchestrator เขียนไฟล์นี้

MQTT_HOST  = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
MQTT_BASE  = os.getenv("MQTT_BASE", "smartcart")

TOPIC_JOB_LATEST = f"{MQTT_BASE}/job/latest"
TOPIC_TOGGLE     = f"{MQTT_BASE}/toggle_omron"
TOPIC_MATCH      = f"{MQTT_BASE}/match"
TOPIC_AMR_STATUS = f"{MQTT_BASE}/amr/status"
TOPIC_AMR_CONN   = f"{MQTT_BASE}/amr/connected"
TOPIC_SENSOR     = f"{MQTT_BASE}/sensor"
TOPIC_LED_CMD    = f"{MQTT_BASE}/led/cmd"

# รายการโหนดที่จะสตาร์ท
ORDER = [
    ("led_actuator",     "led_actuator.py"),
    ("main_server",      "main_server.py"),
    ("match_id",         "match_id.py"),
    ("communicate_AMR",  "communicate_AMR.py"),
    ("main_sensor",      "main_sensor.py"),
]

# ---------- Process runner ----------
PROCS = []
def _log(name):
    return open(LOG_DIR / f"{name}.log", "ab", buffering=0)

def start_node(name, script, extra_env=None):
    env = os.environ.copy()
    if extra_env: env.update(extra_env)
    p = subprocess.Popen([sys.executable, str(BASE_DIR / script)],
                         stdout=_log(name), stderr=subprocess.STDOUT, env=env)
    print(f"[RUNNER] started {name} pid={p.pid}")
    return p

# ---------- Utils ----------
def _now_fields():
    ts = time.time()
    lt = time.localtime(ts)
    return ts, time.strftime("%Y-%m-%d", lt), time.strftime("%H:%M:%S", lt), time.strftime("%Y-%m-%dT%H:%M:%S%z", lt)

def _safe_read_json(path: pathlib.Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _safe_write_json(path: pathlib.Path, obj: Dict[str, Any]):
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _clear_file(path: pathlib.Path):
    try:
        _safe_write_json(path, {})
    except Exception as e:
        print(f"[INIT] cannot clear {path}: {e}")

def _at_least_one_present(cuh_ids, kit_ids) -> bool:
    return any(bool(x) for x in (cuh_ids or [])) or any(bool(x) for x in (kit_ids or []))

def _parse_arrived_line(line: str) -> Optional[str]:
    m = re.match(r"^Arrived at\s+(.+)$", line.strip(), re.I)
    return m.group(1).strip() if m else None

def _fill_two(vals):
    v = list(vals or [])
    v = v[:2] + [None, None]
    return [v[0], v[1]]

# ---------- MQTT helper for retained clearing ----------
def mqtt_connect():
    cli = mqtt.Client(client_id="run_all_bootstrap")
    cli.connect(MQTT_HOST, MQTT_PORT, 30)
    cli.loop_start()
    return cli

def mqtt_clear_retained(cli: mqtt.Client, topic: str):
    # publish payload ว่าง retain=True เพื่อล้าง retained ตามสเปค
    cli.publish(topic, b"", qos=1, retain=True)

def mqtt_led_clear(cli: mqtt.Client):
    # เคลียร์ LED ทุกจุด (ถ้า led_actuator ต้องการ payload อื่น แจ้งได้)
    for target in ("cuh1","cuh2","kit1","kit2"):
        cli.publish(
            TOPIC_LED_CMD,
            json.dumps({"target": target, "result": "skip", "ts": time.time()}),
            qos=1, retain=False
        )

def initial_cleanup():
    cli = mqtt_connect()
    try:
        print("[INIT] clearing state files and retained messages...")
        _clear_file(STATE_PATH)
        _clear_file(FSM_STATE_PATH)
        mqtt_clear_retained(cli, TOPIC_JOB_LATEST)
        mqtt_led_clear(cli)
        time.sleep(0.3)
    finally:
        cli.loop_stop(); cli.disconnect()

# ---------- FSM ----------
class OrchestratorFSM:
    """
    Auto mode:
      - Request: WAIT_MATCH → dispatch เมื่อ smartcart/match complete
      - Return : WAIT_PHOTO_CLEAR (photo 4 ตัวโล่งต่อเนื่อง ≥ 5s) → dispatch
    States:
      IDLE, WAIT_MATCH, WAIT_PHOTO_CLEAR, EN_ROUTE, AT_DEST, RETURNING, DONE
    """
    PHOTO_NAMES_TARGET = ("barcode1", "barcode2", "rfidA", "rfidB")

    def __init__(self, mqtt_cli: mqtt.Client):
        self.cli = mqtt_cli
        self.queue: deque = deque()     # jobs: {"op","goal_id","cuh_ids","kit_ids"}
        self.current: Optional[Dict[str, Any]] = None
        self.state   = "IDLE"
        self.last_update_ts = time.time()

        self.match_info = {
            "required": None, "matched": None, "complete": False,
            "seen": {}, "op": None, "goal_id": None,
        }

        self.photo_state: Dict[str, int] = {}
        self.photo_clear_since: Optional[float] = None
        self.photo_clear_secs_required = 5.0

        # กันซ้ำ
        self._seen_jobs: Set[str] = set()
        self._seen_capacity = 100
        self._last_done_fingerprint: Optional[str] = None
        self._last_done_ts: float = 0.0

    def _fingerprint(self, job: Dict[str, Any]) -> str:
        return json.dumps(
            {"op": job.get("op"),
             "goal_id": job.get("goal_id"),
             "cuh_ids": job.get("cuh_ids"),
             "kit_ids": job.get("kit_ids")},
            sort_keys=True
        )

    def _persist(self):
        ts, d, t, iso = _now_fields()
        out = {
            "ts": ts, "date": d, "time": t, "iso": iso,
            "state": self.state,
            "current": self.current,
            "queue_len": len(self.queue),
            "match": self.match_info,
            "photo": {
                "state": self.photo_state,
                "clear_since": self.photo_clear_since,
                "required_secs": self.photo_clear_secs_required
            },
            "mode": "auto",
        }
        _safe_write_json(FSM_STATE_PATH, out)

    # ---- MQTT events
    def on_job_latest(self, payload: Dict[str, Any]):
        """
        NOTE: main_server ของคุณ publish job/latest 'ไม่มี op'
        และฝัง op ลงใน state.json → เราอ่าน 'op' จาก STATE_PATH
        """
        st = _safe_read_json(STATE_PATH)
        latest = st.get("latest_job_ids") or {}

        op   = latest.get("op")
        goal = latest.get("goal_id") or payload.get("goal_id")
        cuh2 = _fill_two(latest.get("cuh_ids") or ([latest.get("cuh_id")] if latest.get("cuh_id") else []))
        kit2 = _fill_two(latest.get("kit_ids") or ([latest.get("kit_id")] if latest.get("kit_id") else []))

        if op not in ("Request", "Return"):
            print(f"[FSM] ignore job: invalid op in state.json (op={op})")
            return
        if not goal:
            print("[FSM] ignore job: missing goal_id")
            return
        if not _at_least_one_present(cuh2, kit2):
            print("[FSM] ignore job: both CUH and KIT empty")
            return

        # ใช้ ts จาก payload ถ้ามี (main_server ใส่ให้ใน publish_job_topics) — ถ้าไม่มี จะ fallback เป็น now
        ts_in = payload.get("ts") if isinstance(payload, dict) else None
        if not isinstance(ts_in, (int, float)):
            ts_in = time.time()

        job = {"op": op, "goal_id": goal, "cuh_ids": cuh2, "kit_ids": kit2}
        fp  = self._fingerprint(job)
        key = f"{fp}|{int(ts_in)}"

        if key in self._seen_jobs:
            print("[FSM] ignore job: duplicate key (already seen)")
            return

        if self._last_done_fingerprint and fp == self._last_done_fingerprint and ts_in <= self._last_done_ts + 1e-6:
            print("[FSM] ignore job: same as last done (retained duplicate)")
            return

        self._seen_jobs.add(key)
        if len(self._seen_jobs) > self._seen_capacity:
            self._seen_jobs = set(list(self._seen_jobs)[-self._seen_capacity:])

        self.queue.append(job)
        print(f"[FSM] queued job: {job} (qlen={len(self.queue)})")

        if self.state == "IDLE":
            self._prepare()

        self._persist()

    def on_match(self, payload: Dict[str, Any]):
        latest = payload.get("latest_job_ids") or {}
        op = payload.get("op") or latest.get("op")
        goal = latest.get("goal_id") or payload.get("goal_id")
        complete = bool(payload.get("complete"))

        self.match_info = {
            "required": payload.get("required"),
            "matched": payload.get("matched"),
            "complete": complete,
            "seen": payload.get("seen"),
            "op": op,
            "goal_id": goal,
        }
        self.last_update_ts = time.time()
        self._persist()

        if self.current and self.state == "WAIT_MATCH":
            if op == "Request" and complete and goal == self.current.get("goal_id"):
                print(f"[FSM] Request match complete for goal={goal} → dispatch now")
                self._dispatch_current()

    def on_sensor(self, payload: Dict[str, Any]):
        if not isinstance(payload, dict): return
        if payload.get("sensor") != "photo" or "value" not in payload: return
        v = payload["value"]; name = v.get("name")
        if not name: return
        try:
            state = 1 if int(v.get("state",1)) else 0
        except Exception:
            state = 1
        self.photo_state[name] = state
        self.last_update_ts = time.time()

        if self.current and self.current.get("op") == "Return" and self.state == "WAIT_PHOTO_CLEAR":
            self._check_photo_clear_and_maybe_start_timer()
        self._persist()

    def on_amr_status(self, payload: Dict[str, Any]):
        line = (payload or {}).get("line", "")
        if not line or not self.current:
            return
        arr = _parse_arrived_line(line)
        if arr:
            if self.state in ("DISPATCHED", "EN_ROUTE"):
                self.state = "AT_DEST"
                print(f"[FSM] arrived destination: '{arr}'")
            elif self.state in ("AT_DEST", "RETURNING"):
                self.state = "DONE"
                print(f"[FSM] arrived dropoff/home: '{arr}' → DONE")
                self._finish_current()
            self._persist()
        self.last_update_ts = time.time()

    # ---- core
    def _prepare(self):
        if self.current or not self.queue: return
        self.current = self.queue.popleft()
        op = self.current["op"]

        if op == "Request":
            self.state = "WAIT_MATCH"
            print(f"[FSM] WAIT_MATCH (Request) goal={self.current['goal_id']}")
        else:
            self.state = "WAIT_PHOTO_CLEAR"
            self.photo_clear_since = None
            print(f"[FSM] WAIT_PHOTO_CLEAR (Return) goal={self.current['goal_id']}")
            self._check_photo_clear_and_maybe_start_timer()
        self._persist()

    def _check_photo_clear_and_maybe_start_timer(self):
        all_present = all((n in self.photo_state) for n in self.PHOTO_NAMES_TARGET)
        all_clear   = all_present and all(self.photo_state.get(n,0)==1 for n in self.PHOTO_NAMES_TARGET)
        if all_clear:
            if self.photo_clear_since is None:
                self.photo_clear_since = time.time()
                print("[FSM] photo all-clear → start 5s timer")
        else:
            if self.photo_clear_since is not None:
                print("[FSM] photo became blocked again → reset timer")
            self.photo_clear_since = None

    def _dispatch_current(self):
        if not self.current: return
        payload = {
            "reason": "fsm_dispatch",
            "goal_id": self.current["goal_id"],
            "op": self.current["op"],
            "ts": time.time(),
        }
        try:
            self.cli.publish(TOPIC_TOGGLE, json.dumps(payload, ensure_ascii=False), qos=1, retain=False)
            print(f"[FSM] dispatched -> {TOPIC_TOGGLE}: {payload}")
            self.state = "EN_ROUTE"
        except Exception as e:
            print("[FSM] dispatch error:", e)
            self.queue.appendleft(self.current)
            self.current = None
            self.state = "IDLE"
        finally:
            self._persist()

    def _finish_current(self):
        done = self.current
        # เก็บ fingerprint + ts ของงานล่าสุดที่เสร็จ เพื่อกัน retained/ซ้ำ
        try:
            self._last_done_fingerprint = self._fingerprint(done) if done else None
        except Exception:
            self._last_done_fingerprint = None
        self._last_done_ts = time.time()

        self.current = None
        self.state = "IDLE"
        self.photo_clear_since = None
        print(f"[FSM] job done: {done}")
        self._persist()

        # ---- RESET หลังงานจบ ----
        try:
            _safe_write_json(STATE_PATH, {})
            _safe_write_json(FSM_STATE_PATH, {})
            # เคลียร์ retained ของ job/latest
            self.cli.publish(TOPIC_JOB_LATEST, b"", qos=1, retain=True)
            # เคลียร์ LED
            for target in ("cuh1","cuh2","kit1","kit2"):
                self.cli.publish(
                    TOPIC_LED_CMD,
                    json.dumps({"target":target,"result":"skip","ts":time.time()}),
                    qos=1, retain=False
                )
        except Exception as e:
            print(f"[FSM] reset-after-done error: {e}")

        time.sleep(0.3)
        self._prepare()

    # ---- watchdog
    def watchdog_tick(self):
        # Return: all-clear ≥ N วินาที → dispatch
        if self.current and self.current.get("op") == "Return" and self.state == "WAIT_PHOTO_CLEAR":
            if self.photo_clear_since is not None:
                elapsed = time.time() - self.photo_clear_since
                if elapsed >= self.photo_clear_secs_required:
                    print(f"[FSM] photo clear for {elapsed:.1f}s ≥ {self.photo_clear_secs_required}s → dispatch Return")
                    self._dispatch_current()

        # reset ถ้านิ่งเกินไป
        idle_secs = time.time() - self.last_update_ts
        if self.current and idle_secs > 30*60:
            print(f"[FSM] watchdog: no updates for {int(idle_secs)}s → reset to IDLE")
            self.queue.appendleft(self.current)
            self.current = None
            self.state = "IDLE"
            self.photo_clear_since = None
            self._persist()

# ---------- MQTT glue ----------
def start_fsm_mqtt():
    cli = mqtt.Client(client_id="run_all_fsm")
    fsm = OrchestratorFSM(cli)

    def _on_connect(c, u, f, rc):
        subs = [
            (TOPIC_JOB_LATEST, 1),
            (TOPIC_MATCH, 1),
            (TOPIC_SENSOR, 1),
            (TOPIC_AMR_STATUS, 0),
            (TOPIC_AMR_CONN, 1),
        ]
        c.subscribe(subs)
        print(f"[FSM] MQTT connected; sub: job_latest / match / sensor / amr_status / amr_connected")

    def _on_message(c, u, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            data = {}
        if msg.topic == TOPIC_JOB_LATEST:
            fsm.on_job_latest(data)
        elif msg.topic == TOPIC_MATCH:
            fsm.on_match(data)
        elif msg.topic == TOPIC_SENSOR:
            fsm.on_sensor(data)
        elif msg.topic == TOPIC_AMR_STATUS:
            fsm.on_amr_status(data)
        elif msg.topic == TOPIC_AMR_CONN:
            print(f"[FSM] AMR connected={data.get('connected')}")

    cli.on_connect = _on_connect
    cli.on_message = _on_message
    cli.connect(MQTT_HOST, MQTT_PORT, 30)
    cli.loop_start()
    return cli, fsm

# ---------- main ----------
def main():
    # 0) เคลียร์สิ่งค้างก่อนเริ่ม
    initial_cleanup()

    # 1) start nodes
    for name, script in ORDER:
        PROCS.append(start_node(name, script))
        time.sleep(0.4)

    # 2) start FSM/MQTT
    cli, fsm = start_fsm_mqtt()

    # 3) loop
    try:
        while True:
            time.sleep(0.5)
            fsm.watchdog_tick()
            for (name, _), proc in zip(ORDER, PROCS):
                if proc.poll() is not None:
                    print(f"[RUNNER] WARN: process '{name}' exited with code {proc.returncode}")
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[RUNNER] stopping...")
        cli.loop_stop(); cli.disconnect()
        for p in PROCS:
            try: p.send_signal(signal.SIGINT)
            except: pass
        time.sleep(1.5)
        for p in PROCS:
            try:
                if p.poll() is None: p.terminate()
            except: pass

if __name__ == "__main__":
    main()
