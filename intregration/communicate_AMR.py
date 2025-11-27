#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, telnetlib, signal, threading, queue, traceback, re
from collections import deque
import paho.mqtt.client as mqtt

VERSION = "seq-2.2-return-match-at-destination"

# ================= CONFIG =================
STATE_PATH      = os.path.expanduser("~/cart_ws/intregration/data/state.json")
GOALS_MAP_PATH  = os.path.expanduser("~/cart_ws/intregration/data/goals_map.json")

MQTT_HOST  = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
BASE       = os.getenv("MQTT_BASE", "smartcart")

SUB_TOPIC       = f"{BASE}/toggle_omron"        # trigger จาก FSM/run_all
MATCH_TOPIC     = f"{BASE}/match"               # ผลจาก match_id (complete true/false)
STATUS_TOPIC    = f"{BASE}/amr/status"          # publish สถานะบรรทัดดิบจาก ARCL
CONNECTED_TOPIC = f"{BASE}/amr/connected"       # publish true/false

AMR_HOST   = os.getenv("AMR_HOST", "192.168.0.3")
AMR_PORT   = int(os.getenv("AMR_PORT", "7171"))
AMR_PASS   = os.getenv("AMR_PASS", "adept")
TELNET_TIMEOUT = 5.0

# Goals ( related with ARCL)
PICKUP_GOAL   = os.getenv("PICKUP_GOAL",  "ROEQ_SAF_cart500_entry")  # Pickup (Home)
DROPOFF_GOAL  = os.getenv("DROPOFF_GOAL", "ROEQ_SAF_cart500")        # Dropoff (Home)

# Request: time to delay
WAIT_DURATION = int(os.getenv("WAIT_DURATION", "15"))  # sec
# word to say something
COUNTDOWN_MSG = os.getenv("COUNTDOWN_MSG", "5 4 3 2 1 0 Good luck")


SAY_DONE_PATTERNS = (
    "Finished saying",
    "Done speaking",
    "Speech completed",
    "TTS finished",
)

# ================ helpers ================
def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[LOAD] {path} failed: {e}")
        return {}

def _resolve_goal(goal_id: str, goals_map: dict):
    
    if not goal_id or not isinstance(goals_map, dict):
        return None
    entry = goals_map.get(goal_id)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("goal") or entry.get("destination_goal") or entry.get("arcl_goal") or entry.get("name")
    return None

# ========== Telnet Manager ==========
class TelnetAMR:
    def __init__(self, host, port, password, mqtt_client):
        self.host = host
        self.port = port
        self.password = password
        self.mqtt = mqtt_client

        self._tn = None
        self._lock = threading.Lock()
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()

        self._writer_q = queue.Queue()
        self._connected = False

        self._cv = threading.Condition()     
        self._evt_buf = deque(maxlen=1200)   # ring buffer: (ts, line)

        self._reader_th = threading.Thread(target=self._reader_loop, name="AMRReader", daemon=True)
        self._writer_th = threading.Thread(target=self._writer_loop, name="AMRWriter", daemon=True)
        self._hb_th     = threading.Thread(target=self._heartbeat_loop, name="AMRHeartbeat", daemon=True)

        # regex จาก ARCL logs
        self._re_arrived           = re.compile(r"^Arrived at\s+(.+)$", re.I)
        self._re_wait_done         = re.compile(r"^Completed doing task wait\s+(\d+)\s*$", re.I)
        self._re_waitstate_done    = re.compile(r"^WaitState:\s+Waiting completed\s*$", re.I)
        self._re_saying            = re.compile(r'^Saying\s+"(.+)"\s*$', re.I)

        # gate สำหรับรอ match_id
        self._last_match_complete = False
        self._last_match_ts = 0.0
        self._current_goal_id = None   # goal_id

    # ---------- lifecycle ----------
    def start(self):
        self._reader_th.start()
        self._writer_th.start()
        self._hb_th.start()

    def stop(self):
        self._stop.set()
        try:
            if self._tn:
                self._tn.close()
        except Exception:
            pass

    def is_connected(self):
        return self._connected

    # ---------- mqtt ----------
    def publish_connected(self, val: bool):
        self._connected = val
        try:
            self.mqtt.publish(CONNECTED_TOPIC, json.dumps({"connected": val}), qos=1, retain=True)
        except Exception as e:
            print(f"[MQTT] publish connected failed: {e}")

    # ---------- telnet IO ----------
    def _connect(self):
        print(f"[TELNET] connect {self.host}:{self.port}")
        tn = telnetlib.Telnet(self.host, self.port, TELNET_TIMEOUT)
        # login
        tn.write((self.password + "\n").encode("ascii"))
        time.sleep(0.2)
        # init monitors
        for cmd in ("monitorState on","monitorTaskState on","monitorBattery on","monitorLocalization on"):
            tn.write((cmd + "\n").encode("ascii")); time.sleep(0.05)
        with self._lock:
            self._tn = tn
        self.publish_connected(True)
        print("[TELNET] connected + monitors set.")
        return tn

    def _disconnect(self):
        with self._lock:
            try:
                if self._tn:
                    self._tn.close()
            except Exception:
                pass
            self._tn = None
        if self._connected:
            print("[TELNET] disconnected.")
        self.publish_connected(False)

    def _reader_loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                tn = self._connect()
                backoff = 1.0
                buf = b""
                while not self._stop.is_set():
                    try:
                        chunk = tn.read_eager()
                    except EOFError:
                        raise
                    except Exception:
                        chunk = b""
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        for raw in lines:
                            line = raw.decode("utf-8", "ignore").strip()
                            if not line: continue
                            # publish ARCL raw line
                            try:
                                self.mqtt.publish(STATUS_TOPIC, json.dumps({"ts": time.time(), "line": line}), qos=0)
                            except Exception:
                                pass
                            with self._cv:
                                self._evt_buf.append((time.time(), line))
                                self._cv.notify_all()
                    else:
                        time.sleep(0.05)
            except Exception as e:
                print(f"[TELNET] reader error: {e}")
                traceback.print_exc(limit=1)
            finally:
                self._disconnect()
                if not self._stop.is_set():
                    time.sleep(backoff)
                    backoff = min(backoff*2.0, 15.0)

    def _writer_loop(self):
        while not self._stop.is_set():
            try:
                item = self._writer_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not item:
                continue
            lines = item
            sent = False
            with self._lock:
                tn = self._tn
                if tn is not None:
                    try:
                        for line in lines:
                            tn.write((line + "\n").encode("ascii"))
                            print(f"[TELNET:send] {line}")
                            time.sleep(0.03)
                        sent = True
                    except Exception as e:
                        print(f"[TELNET] send error: {e}")
            if not sent:
                print("[TELNET] not connected. command queued but failed to send.")

    def _heartbeat_loop(self):
        if not HEARTBEAT_SEC:
            while not self._stop.is_set():
                time.sleep(1.0)
            return
        last = 0.0
        while not self._stop.is_set():
            now = time.time()
            if self._connected and (now - last) >= HEARTBEAT_SEC:
                self.enqueue_cmd([HEARTBEAT_CMD]); last = now
            time.sleep(0.5)

    # ---------- sync helpers ----------
    def enqueue_cmd(self, cmd_lines):
        if not isinstance(cmd_lines, (list, tuple)) or not cmd_lines:
            return False
        self._writer_q.put(list(cmd_lines))
        return True

    def send_line(self, line: str):
        with self._lock:
            if self._tn is None:
                raise RuntimeError("AMR not connected")
            self._tn.write((line + "\n").encode("ascii"))
            print(f"[TELNET:send] {line}")

    def _wait_stream(self, timeout: float, desc: str, predicate):
        deadline = time.time() + timeout
        last_log = 0.0
        with self._cv:
            start_idx = len(self._evt_buf)

        while True:
            now = time.time()
            if now >= deadline:
                raise TimeoutError(f"wait timeout: {desc}")

            with self._cv:
                if len(self._evt_buf) <= start_idx:
                    self._cv.wait(timeout=min(0.5, deadline - now))
                new_items = list(self._evt_buf)[start_idx:]
                start_idx = len(self._evt_buf)

            for _, line in new_items:
                s = line.strip()
                if s.startswith("Error:"):
                    raise RuntimeError(f"ARCL error while waiting ({desc}): {s}")
                if predicate(s):
                    return s

            if now - last_log > 5:
                print(f"[WAIT] {desc} ...")
                last_log = now

    # ---------- predicates ----------
    def _pred_arrived_goal(self, goal: str):
        goal_low = goal.strip().lower()
        def _ok(s: str):
            m = self._re_arrived.match(s)
            return bool(m and m.group(1).strip().lower() == goal_low)
        return _ok

    def _pred_wait_done_any(self, sec: int):
        sec_str = str(sec)
        def _ok(s: str):
            if self._re_waitstate_done.match(s):
                return True
            m = self._re_wait_done.match(s)
            return bool(m and m.group(1) == sec_str)
        return _ok

    def _pred_saying_started(self):
        def _ok(s: str):
            return bool(self._re_saying.match(s))
        return _ok

    # ---------- say completion ----------
    def _estimate_say_seconds(self, text: str) -> float:
        nchar = max(1, len(text))
        sec = nchar / 10.0 + 1.0
        return min(max(sec, 2.0), 60.0)

    def wait_say_done(self, text: str, hard_timeout: float = 90.0):
        try:
            self._wait_stream(timeout=10.0, desc="say started", predicate=self._pred_saying_started())
        except Exception:
            print("[WAIT] no 'Saying ...' seen; continue with time-based wait")

        try:
            pats = tuple(x.lower() for x in SAY_DONE_PATTERNS)
            def _pred(s: str):
                sl = s.lower()
                return any(p in sl for p in pats)
            self._wait_stream(timeout=5.0, desc="say finished (message)", predicate=_pred)
            return
        except Exception:
            pass

        est = self._estimate_say_seconds(text)
        print(f"[WAIT] speaking ~{est:.1f}s (estimated)")
        t0 = time.time()
        while time.time() - t0 < min(est + 1.0, hard_timeout):
            try:
                pats = tuple(x.lower() for x in SAY_DONE_PATTERNS)
                def _pred2(s: str):
                    sl = s.lower()
                    return any(p in sl for p in pats)
                self._wait_stream(timeout=0.6, desc="say finished (poll)", predicate=_pred2)
                return
            except Exception:
                pass
        print("[WAIT] say fallback done (no explicit finish message)")

    # ---------- match wait (Return waits at destination) ----------
    def wait_for_match_complete(self, timeout: float = 15*60):
        """
        รอจนมีข้อความจาก topic smartcart/match ที่มี complete == True
        """
        deadline = time.time() + timeout
        last_log = 0.0
        while time.time() < deadline:
            if self._last_match_complete:
                print("[MATCH] complete signal received.")
                return
            if time.time() - last_log > 5.0:
                print("[MATCH] waiting for complete ...")
                last_log = time.time()
            time.sleep(0.2)
        raise TimeoutError("wait_for_match_complete timeout")

    # ---------- main sequence ----------
    def run_sequence(self, destination_goal: str, op: str = "Request"):
        """
        Request:
          1) Goto PICKUP_GOAL
          2) Goto destination_goal
          3) doTask wait WAIT_DURATION
          4) say COUNTDOWN_MSG
          5) Goto DROPOFF_GOAL

        Return :
          1) Goto PICKUP_GOAL
          2) Goto destination_goal
          3) **Reset match gate & wait for match at destination**
          4) say COUNTDOWN_MSG
          5) Goto DROPOFF_GOAL
        """
        if not self.is_connected():
            raise RuntimeError("AMR not connected")

        if not self._seq_lock.acquire(blocking=False):
            raise RuntimeError("AMR is busy running another sequence")

        try:
            # Step 1: Home entry (pickup)
            self.send_line(f"Goto {PICKUP_GOAL}")
            self._wait_stream(timeout=30*60, desc=f"Arrived at {PICKUP_GOAL}",
                              predicate=self._pred_arrived_goal(PICKUP_GOAL))

            # Step 2: Destination
            self.send_line(f"Goto {destination_goal}")
            self._wait_stream(timeout=30*60, desc=f"Arrived at {destination_goal}",
                              predicate=self._pred_arrived_goal(destination_goal))

            if op.lower() == "return":
                # ---- Reset match gate "หลังถึงปลายทาง" เพื่อบังคับให้รอการสแกนจริง ณ ปลายทาง ----
                self._last_match_complete = False
                print("[SEQ] Return: waiting for match at DESTINATION ...")
                self.wait_for_match_complete(timeout=15*60)
            else:
                # Request: unload wait
                self.send_line(f"doTask wait {WAIT_DURATION}")
                self._wait_stream(timeout=WAIT_DURATION + 120,
                                  desc=f"wait {WAIT_DURATION}s completed",
                                  predicate=self._pred_wait_done_any(WAIT_DURATION))

            # Step 4: say countdown
            say_text = COUNTDOWN_MSG
            self.send_line(f'say "{say_text}"')
            self.wait_say_done(say_text, hard_timeout=120.0)

            # Step 5: Home dock
            self.send_line(f"Goto {DROPOFF_GOAL}")
            self._wait_stream(timeout=30*60, desc=f"Arrived at {DROPOFF_GOAL}",
                              predicate=self._pred_arrived_goal(DROPOFF_GOAL))

            print("[SEQ] Completed all steps.")
        finally:
            self._seq_lock.release()

# ============== MQTT glue ==============
def on_connect(client, userdata, flags, rc):
    print(f"[MAIN] starting communicate_AMR ({VERSION})")
    print(f"[MQTT] sub {SUB_TOPIC} , {MATCH_TOPIC}")
    client.subscribe(SUB_TOPIC, qos=1)
    client.subscribe(MATCH_TOPIC, qos=1)

def on_message(client, userdata, msg):
    amr: TelnetAMR = userdata["amr"]

    # 1) รับผล match จาก match_id
    if msg.topic == MATCH_TOPIC:
        try:
            m = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        complete = bool(m.get("complete"))
        amr._last_match_complete = complete
        amr._last_match_ts = time.time()
        print(f"[MATCH] update complete={complete} ts={amr._last_match_ts}")
        return

    # 2) รับ trigger เริ่มงานจาก FSM
    if msg.topic == SUB_TOPIC:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[MQTT] bad payload: {e}")
            return

        goal_id = payload.get("goal_id") or (payload.get("latest_job_ids") or {}).get("goal_id")
        op      = payload.get("op") or (payload.get("latest_job_ids") or {}).get("op") or "Request"

        print(f"[TRIGGER] received. op={op} goal_id={goal_id}")

        goals_map = _load_json(GOALS_MAP_PATH)
        destination_goal = _resolve_goal(goal_id, goals_map)
        if not destination_goal:
            print(f"[MAP] goal_id '{goal_id}' not found in {GOALS_MAP_PATH}")
            return

        print(f"[MAP] goal_id '{goal_id}' mapped to goal '{destination_goal}'")

        # reset match gate for new job
        amr._last_match_complete = False
        amr._current_goal_id = goal_id

        def _run():
            try:
                amr.run_sequence(destination_goal=destination_goal, op=(op or "Request"))
            except Exception as e:
                print(f"[SEQ] aborted: {e}")

        threading.Thread(target=_run, name="AMRSequence", daemon=True).start()

# ================ main ================
def main():
    cli = mqtt.Client(client_id="communicate_AMR", userdata={})
    amr = TelnetAMR(AMR_HOST, AMR_PORT, AMR_PASS, cli)
    cli.user_data_set({"amr": amr})

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, 30)

    amr.start()

    def _exit(*_):
        try:
            amr.stop()
            cli.loop_stop()
            cli.disconnect()
        finally:
            os._exit(0)

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)

    cli.loop_start()
    print("[MAIN] running. Press Ctrl+C to quit.")
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()

