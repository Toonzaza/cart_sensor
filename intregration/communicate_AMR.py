#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
communicate_AMR.py
- subscribe <base>/trigger  (จาก match_id)
- อ่าน goal_id จาก state.json แล้ว map เป็น GoalName ด้วย goals_map.json
- ส่งคำสั่ง ARCL "goto <GoalName>" ผ่าน Telnet ไปยัง AMR
- publish ผลลัพธ์ไป <base>/amr/ack และรายงานสถานะ/ที่อยู่เป็นช่วง ๆ (optional) ไป <base>/amr/state
"""

import argparse, json, time, telnetlib, threading, queue
from pathlib import Path
from typing import Optional, Dict
import paho.mqtt.client as mqtt

# ---------- Telnet ARCL client ----------
class ARCLClient:
    def __init__(self, host="192.168.0.3", port=7171, password="adept",
                 connect_timeout=5.0, rw_timeout=5.0):
        self.host, self.port = host, port
        self.password = password
        self.connect_timeout = connect_timeout
        self.rw_timeout = rw_timeout
        self.tn: Optional[telnetlib.Telnet] = None
        self.lock = threading.Lock()

    def _expect(self, what: bytes, timeout=None) -> bool:
        if not self.tn: return False
        idx, _, _ = self.tn.expect([what], timeout or self.rw_timeout)
        return idx == 0

    def connect(self) -> None:
        """เชื่อมต่อและ login ถ้ายังไม่พร้อม"""
        with self.lock:
            if self.tn is not None:
                return
            self.tn = telnetlib.Telnet(self.host, self.port, timeout=self.connect_timeout)
            # รอ prompt ขอสายรหัส
            self._expect(b"Enter password:", timeout=self.rw_timeout)
            self.tn.write((self.password + "\r\n").encode("ascii"))
            # รอการตอบรับสั้น ๆ (บางเวอร์ชันจะส่ง "Welcome" หรือ prompt เงียบ)
            time.sleep(0.2)

    def send_cmd(self, cmd: str) -> str:
        """
        ส่งคำสั่ง ARCL หนึ่งบรรทัดและอ่านคำตอบช่วงสั้น ๆ กลับมาเป็นสตริง
        (ARCL ไม่มีมาตรฐาน "END" ตายตัว—ใช้อ่านคายบัฟเฟอร์ช่วงสั้น ๆ แทน)
        """
        with self.lock:
            if self.tn is None:
                self.connect()
            # เขียนคำสั่ง
            self.tn.write((cmd + "\r\n").encode("ascii"))
            # เก็บคำตอบช่วงสั้น ๆ
            time.sleep(0.2)
            out = b""
            # ดูดบัฟเฟอร์ที่มีอยู่ (ไม่บล็อกนาน)
            t0 = time.time()
            while time.time() - t0 < self.rw_timeout:
                try:
                    chunk = self.tn.read_very_eager()
                except EOFError:
                    break
                if chunk:
                    out += chunk
                    # เว้นจังหวะสั้น ๆ รอให้ส่งหมด
                    time.sleep(0.05)
                else:
                    break
            return out.decode(errors="ignore")

    def goto(self, goal_name: str) -> str:
        """สั่งไปเป้าหมาย"""
        return self.send_cmd(f"goto {goal_name}")

    def where_am_i(self) -> str:
        """ขอสถานะ/ที่อยู่คร่าว ๆ (ขึ้นกับเวอร์ชัน ARCL)"""
        # บางระบบใช้ 'whereAmI' หรือ 'getState' / 'getGoal'
        # ลอง whereAmI ก่อน ถ้าไม่ได้คุณปรับเป็นคำสั่งที่ AMR ของคุณรองรับ
        return self.send_cmd("whereAmI")

    def close(self):
        with self.lock:
            try:
                if self.tn:
                    self.tn.write(b"logout\r\n")
                    self.tn.close()
            finally:
                self.tn = None

# ---------- Utilities ----------
def load_json(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def pick_goal_id(state_file: str) -> Optional[str]:
    d = load_json(state_file)
    if isinstance(d.get("expected"), dict) and d.get("goal_id") is None:
        # บางระบบอาจเก็บ goal_id ใน expected — รองรับกรณีนี้ได้ถ้าจำเป็น
        pass
    return d.get("goal_id")

# ---------- Communicator ----------
class CommunicateAMR:
    def __init__(self, mqtt_host: str, mqtt_port: int, base: str,
                 state_file: str, goals_map_file: str,
                 arcl_host: str, arcl_port: int, arcl_password: str,
                 poll_state_sec: Optional[float] = None):
        self.base = base
        self.topic_trigger = f"{base}/trigger"      # รับจาก match_id
        self.topic_ack     = f"{base}/amr/ack"      # รายงานผลส่งคำสั่ง
        self.topic_state   = f"{base}/amr/state"    # รายงานสถานะ (optional)

        self.state_file = state_file
        self.goals_map: Dict[str, str] = load_json(goals_map_file)

        self.cli = mqtt.Client(client_id="communicate_AMR")
        self.cli.on_connect = self._on_connect
        self.cli.on_message = self._on_message
        self.cli.connect(mqtt_host, mqtt_port, keepalive=30)

        self.arcl = ARCLClient(host=arcl_host, port=arcl_port, password=arcl_password)
        self.queue = queue.Queue()  # serialize trigger → ARCL

        # worker ส่งคำสั่ง
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

        # optional: โพลสถานะเป็นช่วง ๆ
        self.poll_state_sec = poll_state_sec
        if poll_state_sec and poll_state_sec > 0:
            self.poller = threading.Thread(target=self._poll_state_loop, daemon=True)
            self.poller.start()

    # ------ MQTT callbacks ------
    def _on_connect(self, c, u, f, rc):
        c.subscribe(self.topic_trigger, qos=1)
        print(f"[MQTT] subscribed {self.topic_trigger}")

    def _on_message(self, c, u, msg):
        if msg.topic != self.topic_trigger:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            print("[WARN] bad JSON trigger")
            return
        # ใส่คิวให้ worker จัดการ
        self.queue.put(payload)

    # ------ worker ------
    def _worker_loop(self):
        while True:
            trig = self.queue.get()
            try:
                self._handle_trigger(trig)
            except Exception as e:
                print(f"[ERR] handle trigger: {e}")

    def _handle_trigger(self, trig: dict):
        # อ่าน goal_id จาก state.json (ถ้าข้อความมี goal_id ก็ใช้จากข้อความได้)
        goal_id = trig.get("goal_id") or pick_goal_id(self.state_file)
        if not goal_id:
            self._publish_ack("error", "missing_goal_id", trig, None, None)
            return

        goal_name = self.goals_map.get(goal_id)
        if not goal_name:
            self._publish_ack("error", f"goal_id_not_mapped:{goal_id}", trig, goal_id, None)
            return

        # ส่ง ARCL
        try:
            self.arcl.connect()
            reply = self.arcl.goto(goal_name)
            # ประเมินคร่าว ๆ ว่าสำเร็จไหมจากข้อความตอบ
            ok = ("OK" in reply.upper()) or ("DONE" in reply.upper()) or ("ACCEPTED" in reply.upper())
            self._publish_ack("ok" if ok else "sent", None if ok else reply, trig, goal_id, goal_name)
        except Exception as e:
            self._publish_ack("error", f"{type(e).__name__}: {e}", trig, goal_id, goal_name)

    def _publish_ack(self, status: str, detail: Optional[str], trig: dict,
                     goal_id: Optional[str], goal_name: Optional[str]):
        msg = {
            "status": status,                # ok | sent | error
            "detail": detail,                # ข้อความตอบกลับ/สาเหตุ
            "goal_id": goal_id,
            "goal_name": goal_name,
            "trigger": trig,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.cli.publish(self.topic_ack, json.dumps(msg), qos=1)
        print(f"[ACK] {msg}")

    # ------ polling AMR state (optional) ------
    def _poll_state_loop(self):
        while True:
            try:
                self.arcl.connect()
                reply = self.arcl.where_am_i()
                self.cli.publish(self.topic_state, json.dumps({
                    "raw": reply,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }), qos=0)
            except Exception as e:
                print(f"[STATE] poll error: {e}")
            time.sleep(self.poll_state_sec or 5)

    def loop_forever(self):
        self.cli.loop_forever()

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="communicate_AMR: receive trigger → map goal → send ARCL via Telnet")
    # MQTT
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-base", default="smartcart")
    # Files
    ap.add_argument("--state-file", default="state.json",
                    help="ไฟล์ที่ server_pi เขียน goal_id ล่าสุด (และ expected)")
    ap.add_argument("--goals-map", default="goals_map.json",
                    help="ไฟล์ map goal_id → GoalName ของ AMR/ARCL")
    # ARCL/Telnet
    ap.add_argument("--arcl-host", default="192.168.0.3")
    ap.add_argument("--arcl-port", type=int, default=7171)
    ap.add_argument("--arcl-password", default="adept")
    # Optional polling
    ap.add_argument("--poll-state-sec", type=float, default=0.0,
                    help="ถ้า >0 จะโพลสถานะ AMR เป็นช่วง ๆ (วินาที)")
    args = ap.parse_args()

    svc = CommunicateAMR(
        mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port, base=args.mqtt_base,
        state_file=args.state_file, goals_map_file=args.goals_map,
        arcl_host=args.arcl_host, arcl_port=args.arcl_port, arcl_password=args.arcl_password,
        poll_state_sec=args.poll_state_sec if args.poll_state_sec > 0 else None
    )
    print("communicate_AMR running. Ctrl+C to stop.")
    try:
        svc.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        svc.arcl.close()

if __name__ == "__main__":
    main()
