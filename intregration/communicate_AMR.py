#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, telnetlib, signal, threading, queue, traceback
import paho.mqtt.client as mqtt

# ---- CONFIG ----
STATE_PATH = "/home/fibo/cart_ws/intregration/data/state.json"
GOALS_MAP_PATH = "/home/fibo/cart_ws/intregration/data/goals_map.json"

MQTT_HOST  = "127.0.0.1"
MQTT_PORT  = 1883
BASE       = "smartcart"
SUB_TOPIC  = f"{BASE}/toggle_omron"      # << subscribe trigger จาก match_id
STATUS_TOPIC = f"{BASE}/amr/status"      # << publish สถานะจาก ARCL (บรรทัดดิบ)
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
    # ถ้ามี rate: "setStateUpdateRate 1"  # ตัวอย่าง: 1 Hz (รองรับในบางเวอร์ชัน)
]

# Heartbeat ป้องกันหลุด/กระตุ้นสถานะ (ถ้าระบบไม่ push เอง)
HEARTBEAT_CMD = "whereAmI"
HEARTBEAT_SEC = 0.0

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
    คืน (cmd, goal_name)
    รองรับ goals_map:
      - {"DOT400002": "Goal2"}
      - {"DOT400002": {"goal":"Goal2", "cmd":"Goto"}}
    ถ้าไม่พบ คืน (None, None)
    """
    if not goal_id or not isinstance(goals_map, dict):
        return (None, None)
    entry = goals_map.get(goal_id)
    if entry is None:
        return (None, None)
    if isinstance(entry, str):
        return ("Goto", entry)  # default cmd (ARCL มักใช้ 'goto'/'Goto')
    if isinstance(entry, dict):
        goal_name = entry.get("goal") or entry.get("arcl_goal") or entry.get("name")
        cmd = entry.get("cmd") or entry.get("command") or "Goto"
        if goal_name:
            return (cmd, goal_name)
    return (None, None)

# ---- Telnet Manager (persistent connection + reader thread) ----
class TelnetAMR:
    def __init__(self, host, port, password, mqtt_client):
        self.host = host
        self.port = port
        self.password = password
        self.mqtt = mqtt_client

        self._tn = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._writer_q = queue.Queue()
        self._connected = False

        self._reader_th = threading.Thread(target=self._reader_loop, name="AMRReader", daemon=True)
        self._writer_th = threading.Thread(target=self._writer_loop, name="AMRWriter", daemon=True)
        self._hb_th     = threading.Thread(target=self._heartbeat_loop, name="AMRHeartbeat", daemon=True)

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

    def publish_connected(self, val: bool):
        self._connected = val
        try:
            self.mqtt.publish(CONNECTED_TOPIC, json.dumps({"connected": val}), qos=1, retain=True)
        except Exception as e:
            print(f"[MQTT] publish connected failed: {e}")

    def _connect(self):
        print(f"[TELNET] connect {self.host}:{self.port}")
        tn = telnetlib.Telnet(self.host, self.port, TELNET_TIMEOUT)
        # ส่งรหัสผ่านทันที (ARCL รับเป็นบรรทัดแรก)
        tn.write((self.password + "\n").encode("ascii"))
        time.sleep(0.2)
        # ส่ง init monitor commands
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
                backoff = 1.0  # reset backoff เมื่อเชื่อมต่อสำเร็จ
                # อ่านบรรทัดสถานะอย่างต่อเนื่อง
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
                        # ตัดตามบรรทัด
                        *lines, buf = buf.split(b"\n")
                        for raw in lines:
                            line = raw.decode("utf-8", "ignore").strip()
                            if not line:
                                continue
                            # publish ออกไปที่ STATUS_TOPIC
                            self.mqtt.publish(STATUS_TOPIC, json.dumps({"ts": time.time(), "line": line}), qos=0)
                    else:
                        time.sleep(0.05)
            except Exception as e:
                print(f"[TELNET] reader error: {e}")
                traceback.print_exc(limit=1)
            finally:
                self._disconnect()
                # ถ้ายังไม่ถูกสั่งหยุด ให้ backoff แล้วค่อยลองใหม่
                if not self._stop.is_set():
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, 15.0)

    def _writer_loop(self):
        while not self._stop.is_set():
            try:
                item = self._writer_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                continue
            lines = item
            sent = False
            with self._lock:
                tn = self._tn
                if tn is not None:
                    try:
                        for line in lines:
                            print(f"[TELNET:send] {line}")
                            tn.write((line + "\n").encode("ascii"))
                            time.sleep(0.03)
                        sent = True
                    except Exception as e:
                        print(f"[TELNET] send error: {e}")
            if not sent:
                print("[TELNET] not connected. command queued but failed to send.")

    def _heartbeat_loop(self):
        if not HEARTBEAT_SEC:  # 0.0 / None => ปิด
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

    def enqueue_cmd(self, cmd_lines):
        """ใส่คำสั่งชุด (list[str]) ลงคิวให้ writer ส่งบน socket เดิม"""
        if not isinstance(cmd_lines, (list, tuple)) or not cmd_lines:
            return False
        self._writer_q.put(list(cmd_lines))
        return True

# ---- MQTT handlers ----
def on_connect(client, userdata, flags, rc):
    print("communicate_AMR running. Ctrl+C to quit.")
    print(f"[MQTT] sub {SUB_TOPIC}")
    client.subscribe(SUB_TOPIC, qos=1)

def on_message(client, userdata, msg):
    # รับ trigger จาก match_id
    amr: TelnetAMR = userdata["amr"]
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[MQTT] bad payload: {e}")
        return

    # goal_id อาจอยู่ที่ payload["goal_id"] หรือ payload["latest_job_ids"]["goal_id"]
    goal_id = payload.get("goal_id")
    if goal_id is None:
        lj = payload.get("latest_job_ids") or {}
        goal_id = lj.get("goal_id")

    print(f"[TOGGLE] received. goal_id={goal_id}")

    # โหลด goals_map + state ปัจจุบัน (เผื่ออยาก log/ตรวจ)
    goals_map = _load_json(GOALS_MAP_PATH)
    _ = _load_json(STATE_PATH)  # ไม่ได้ใช้ต่อ แต่อาจมี log ในอนาคต

    cmd, goal_name = _resolve_goal(goal_id, goals_map)
    if not cmd or not goal_name:
        print(f"[MAP] goal_id '{goal_id}' not found in {GOALS_MAP_PATH}")
        return

    # เตรียมคำสั่ง ARCL (เช่น "Goto Goal2")
    cmd_lines = [f"{cmd} {goal_name}"]
    # ส่งไปที่ AMR บน persistent socket
    ok = amr.enqueue_cmd(cmd_lines)
    if ok:
        print(f"[AMR] queued command: {cmd_lines}")
    else:
        print("[AMR] failed to queue command.")

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
