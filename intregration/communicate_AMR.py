#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, telnetlib, signal, threading, queue, traceback, re
from collections import deque
import paho.mqtt.client as mqtt

VERSION = "seq-1.4-ignore-interrupted-and-wait-or"

# ---- CONFIG ----
STATE_PATH = "/home/fibo/cart_ws/intregration/data/state.json"
GOALS_MAP_PATH = "/home/fibo/cart_ws/intregration/data/goals_map.json"

MQTT_HOST  = "127.0.0.1"
MQTT_PORT  = 1883
BASE       = "smartcart"
SUB_TOPIC  = f"{BASE}/toggle_omron"       # << subscribe trigger จาก match_id
STATUS_TOPIC = f"{BASE}/amr/status"       # << publish สถานะจาก ARCL (บรรทัดดิบ)
CONNECTED_TOPIC = f"{BASE}/amr/connected" # << publish true/false

AMR_HOST   = "192.168.0.3"
AMR_PORT   = 7171
AMR_PASS   = "adept"
TELNET_TIMEOUT = 5.0

# Init คำสั่งหลัง login (ARCL ส่วนใหญ่รองรับ monitor*)
INIT_MONITOR_CMDS = [
    "monitorState on",
    "monitorTaskState on",
    "monitorBattery on",
    "monitorLocalization on",
]

# Heartbeat ป้องกันหลุด/กระตุ้นสถานะ (0.0 = ปิด)
HEARTBEAT_CMD = "whereAmI"
HEARTBEAT_SEC = 0.0

# Fixed Goals และคำสั่ง
PICKUP_GOAL   = "ROEQ_SAF_cart500_entry"
DROPOFF_GOAL  = "ROEQ_SAF_cart500"
WAIT_DURATION = 30  # วินาที
COUNTDOWN_MSG = "5 4 3 2 1 0 Good luck"

# ถ้า ARCL ของคุณมีสตริงยืนยัน “จบการพูด” เพิ่มที่นี่
SAY_DONE_PATTERNS = (
    "Finished saying",
    "Done speaking",
    "Speech completed",
    "TTS finished",
)

# ---- helpers ----
def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[LOAD] {path} failed: {e}")
        return {}

def _resolve_goal(goal_id: str, goals_map: dict):
    """
    คืน goal_name จาก goal_id
    รองรับ goals_map:
      - {"DOT400002": "Goal2"}
      - {"DOT400002": {"goal":"Goal2", ...}}
    ถ้าไม่พบ คืน None
    """
    if not goal_id or not isinstance(goals_map, dict):
        return None
    entry = goals_map.get(goal_id)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        goal_name = entry.get("goal") or entry.get("destination_goal") or entry.get("arcl_goal") or entry.get("name")
        return goal_name
    return None

# ---- Telnet Manager (persistent connection + reader thread + synchronous sequence) ----
class TelnetAMR:
    def __init__(self, host, port, password, mqtt_client):
        self.host = host
        self.port = port
        self.password = password
        self.mqtt = mqtt_client

        self._tn = None
        self._lock = threading.Lock()         # ป้องกันการเขียนทับ socket
        self._seq_lock = threading.Lock()     # serialize งาน run_sequence (1 งานต่อครั้ง)
        self._stop = threading.Event()

        self._writer_q = queue.Queue()
        self._connected = False

        self._cv = threading.Condition()      # ปลุกเมื่อมีบรรทัดใหม่
        self._evt_buf = deque(maxlen=600)     # ring buffer เก็บ (ts, line)

        self._reader_th = threading.Thread(target=self._reader_loop, name="AMRReader", daemon=True)
        self._writer_th = threading.Thread(target=self._writer_loop, name="AMRWriter", daemon=True)
        self._hb_th     = threading.Thread(target=self._heartbeat_loop, name="AMRHeartbeat", daemon=True)

        # regex ตรงกับ log ของคุณ
        self._re_arrived           = re.compile(r"^Arrived at\s+(.+)$", re.I)
        self._re_going             = re.compile(r"^Going to\s+(.+)$", re.I)
        self._re_wait_done         = re.compile(r"^Completed doing task wait\s+(\d+)\s*$", re.I)
        self._re_waitstate_done    = re.compile(r"^WaitState:\s+Waiting completed\s*$", re.I)
        self._re_saying            = re.compile(r'^Saying\s+"(.+)"\s*$', re.I)
        self._re_interrupted       = re.compile(r"^Interrupted:\s+(.+)$", re.I)

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

    # ---------- mqtt helpers ----------
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
        # ส่งรหัสผ่านทันที
        tn.write((self.password + "\n").encode("ascii"))
        time.sleep(0.2)
        # ส่ง init monitor
        for cmd in INIT_MONITOR_CMDS:
            tn.write((cmd + "\n").encode("ascii"))
            time.sleep(0.05)
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
            tn = None
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
                            if not line:
                                continue
                            # publish MQTT สถานะดิบ
                            try:
                                self.mqtt.publish(STATUS_TOPIC, json.dumps({"ts": time.time(), "line": line}), qos=0)
                            except Exception:
                                pass
                            # เก็บลง event buffer และปลุก waiters
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
                    backoff = min(backoff * 2.0, 15.0)

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
                self.enqueue_cmd([HEARTBEAT_CMD])
                last = now
            time.sleep(0.5)

    # ---------- sync helpers ----------
    def enqueue_cmd(self, cmd_lines):
        """ใช้ทั่วไป; sequence จริงจะไม่ใช้"""
        if not isinstance(cmd_lines, (list, tuple)) or not cmd_lines:
            return False
        self._writer_q.put(list(cmd_lines))
        return True

    def send_line(self, line: str):
        """ส่งคำสั่งเดี่ยวแบบ synchronous (ไม่เข้าคิว)"""
        with self._lock:
            tn = self._tn
            if tn is None:
                raise RuntimeError("AMR not connected")
            tn.write((line + "\n").encode("ascii"))
            print(f"[TELNET:send] {line}")

    def _wait_stream(self, timeout: float, desc: str, predicate):
        """
        รอจน predicate(line) == True
        - 'Error:' => fatal (raise)
        - 'Interrupted:*' => ไม่ถือว่า error (รวมถึง 'Parking') — log แล้วรอต่อ
        """
        deadline = time.time() + timeout
        last_print = 0.0
        with self._cv:
            start_idx = len(self._evt_buf)

        while True:
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError(f"wait timeout: {desc}")

            with self._cv:
                if len(self._evt_buf) <= start_idx:
                    self._cv.wait(timeout=min(0.5, remaining))
                new_items = list(self._evt_buf)[start_idx:]
                start_idx = len(self._evt_buf)

            for ts, line in new_items:
                s = line.strip()
                # Fatal เฉพาะ Error:
                if s.startswith("Error:"):
                    raise RuntimeError(f"ARCL error while waiting ({desc}): {s}")
                # Interrupted:* -> ignore
                if s.startswith("Interrupted:"):
                    print(f"[WAIT] note: {s} (ignored)")
                # สำเร็จตามเงื่อนไข
                if predicate(s):
                    return s

            if now - last_print > 5:
                print(f"[WAIT] {desc} ...")
                last_print = now

    # ---------- predicate helpers ----------
    def _pred_arrived_goal(self, goal: str):
        goal_low = goal.strip().lower()
        def _ok(s: str):
            m = self._re_arrived.match(s)
            if not m: 
                return False
            g = m.group(1).strip().lower()
            return goal_low == g
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
        # ไม่ต้องเทียบข้อความพูดเป๊ะ ๆ ก็ได้ ตาม log จะเป็น Saying "...."
        def _ok(s: str):
            return bool(self._re_saying.match(s))
        return _ok

    def _pred_say_finished(self):
        pats = tuple(x.lower() for x in SAY_DONE_PATTERNS)
        def _ok(s: str):
            sl = s.lower()
            return any(p in sl for p in pats)
        return _ok

    # ---------- say completion wait ----------
    def _estimate_say_seconds(self, text: str) -> float:
        # ประมาณ: 10 ตัว/วินาที + 1s buffer (min 2s, max 60s)
        nchar = max(1, len(text))
        sec = nchar / 10.0 + 1.0
        return min(max(sec, 2.0), 60.0)

    def wait_say_done(self, text: str, hard_timeout: float = 90.0):
        # 1) รอเริ่ม Saying
        try:
            self._wait_stream(timeout=10.0, desc="say started", predicate=self._pred_saying_started())
        except Exception:
            print("[WAIT] no 'Saying ...' seen; continue with time-based wait")

        # 2) พยายามหาข้อความจบ
        try:
            self._wait_stream(timeout=5.0, desc="say finished (message)", predicate=self._pred_say_finished())
            return
        except Exception:
            pass

        # 3) ไม่มีข้อความจบ → ใช้เวลาประมาณการ
        est = self._estimate_say_seconds(text)
        print(f"[WAIT] speaking ~{est:.1f}s (estimated)")
        t0 = time.time()
        while time.time() - t0 < min(est + 1.0, hard_timeout):
            try:
                self._wait_stream(timeout=0.6, desc="say finished (poll)", predicate=self._pred_say_finished())
                return
            except Exception:
                pass
        print("[WAIT] say fallback done (no explicit finish message)")

    # ---------- main sequence ----------
    def run_sequence(self, destination_goal: str):
        """
        1) Goto PICKUP_GOAL     -> Arrived at PICKUP_GOAL
        2) Goto destination     -> Arrived at destination
        3) doTask wait N        -> Completed doing task wait N  OR  WaitState: Waiting completed
        4) say "<COUNTDOWN>"    -> พูดให้เสร็จจริง (ข้อความจบหรือเวลา)
        5) Goto DROPOFF_GOAL    -> Arrived at DROPOFF_GOAL
        """
        if not self.is_connected():
            raise RuntimeError("AMR not connected")

        if not self._seq_lock.acquire(blocking=False):
            raise RuntimeError("AMR is busy running another sequence")

        try:
            # Step 1
            self.send_line(f"Goto {PICKUP_GOAL}")
            self._wait_stream(timeout=30*60, desc=f"Arrived at {PICKUP_GOAL}",
                              predicate=self._pred_arrived_goal(PICKUP_GOAL))

            # Step 2
            self.send_line(f"Goto {destination_goal}")
            self._wait_stream(timeout=30*60, desc=f"Arrived at {destination_goal}",
                              predicate=self._pred_arrived_goal(destination_goal))

            # Step 3
            self.send_line(f"doTask wait {WAIT_DURATION}")
            self._wait_stream(timeout=WAIT_DURATION + 120,
                              desc=f"wait {WAIT_DURATION}s completed",
                              predicate=self._pred_wait_done_any(WAIT_DURATION))

            # Step 4 — รอให้ “พูดเสร็จ” ก่อนค่อยไปต่อ
            say_text = COUNTDOWN_MSG
            self.send_line(f'say "{say_text}"')
            self.wait_say_done(say_text, hard_timeout=120.0)

            # Step 5
            self.send_line(f"Goto {DROPOFF_GOAL}")
            self._wait_stream(timeout=30*60, desc=f"Arrived at {DROPOFF_GOAL}",
                              predicate=self._pred_arrived_goal(DROPOFF_GOAL))

            print("[SEQ] Completed all 5 steps.")
        finally:
            self._seq_lock.release()

# ---- MQTT handlers ----
def on_connect(client, userdata, flags, rc):
    print(f"[MAIN] starting communicate_AMR ({VERSION})")
    print(f"[MQTT] sub {SUB_TOPIC}")
    client.subscribe(SUB_TOPIC, qos=1)

def on_message(client, userdata, msg):
    """
    รับ trigger จาก MQTT แล้วรันลำดับคำสั่งแบบ synchronous:
      1. Goto PICKUP_GOAL
      2. Goto goal (จาก goals_map.json)
      3. doTask wait N วินาที
      4. say ข้อความ COUNTDOWN_MSG (รอให้ “พูดเสร็จจริง”)
      5. Goto DROPOFF_GOAL
    """
    amr: TelnetAMR = userdata["amr"]
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[MQTT] bad payload: {e}")
        return

    # goal_id อาจอยู่ตรงๆ หรือ nested ใน latest_job_ids
    goal_id = payload.get("goal_id")
    if goal_id is None:
        lj = payload.get("latest_job_ids") or {}
        goal_id = lj.get("goal_id")

    print(f"[TRIGGER] received. goal_id={goal_id}")

    goals_map = _load_json(GOALS_MAP_PATH)
    destination_goal = _resolve_goal(goal_id, goals_map)

    if not destination_goal:
        print(f"[MAP] goal_id '{goal_id}' not found in {GOALS_MAP_PATH}")
        return

    print(f"[MAP] goal_id '{goal_id}' mapped to goal '{destination_goal}'")

    # รัน sequence ใน thread แยก เพื่อไม่บล็อก callback ของ MQTT
    def _run():
        try:
            amr.run_sequence(destination_goal)
        except Exception as e:
            print(f"[SEQ] aborted: {e}")

    threading.Thread(target=_run, name="AMRSequence", daemon=True).start()

# ---- main ----
def main():
    # สร้าง MQTT client + AMR manager
    cli = mqtt.Client(client_id="communicate_AMR", userdata={})
    amr = TelnetAMR(AMR_HOST, AMR_PORT, AMR_PASS, cli)
    cli.user_data_set({"amr": amr})

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, 30)

    # start AMR persistent connection threads
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

    cli.loop_start()   # ไม่บล็อกเธรดหลัก
    print("[MAIN] running. Press Ctrl+C to quit.")
    # หลับยาว ๆ ให้สัญญาณมาปลุก
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
