#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
match_id.py
- รับค่าจาก detect_sensor (MQTT: <base>/sensor)
- เช็คกับ state.json (เฉพาะ CUH_ID, KIT_ID)
- ถ้าทั้งสองค่าตรงภายในหน้าต่างเวลา → ส่งสัญญาณไป communicate_AMR (MQTT: <base>/trigger)
"""
import json, time, argparse, threading
from pathlib import Path
from typing import Optional, Dict, Tuple
import paho.mqtt.client as mqtt
import hashlib

REQUIRED_FIELDS = ("CUH_ID", "KIT_ID")

def _hash_job(expected: Dict[str, Optional[str]]) -> str:
    """ใช้ทำ version กันยิงซ้ำเมื่อ state.json เปลี่ยน"""
    payload = json.dumps({"exp": expected}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

class MatchID:
    def __init__(self, state_file: str, window_sec: float = 12.0):
        self.state_path = Path(state_file)
        self.window_sec = window_sec
        self.expected: Dict[str, Optional[str]] = {"CUH_ID": None, "KIT_ID": None}
        self.version: str = _hash_job(self.expected)
        self.cache: Dict[str, Tuple[str, float]] = {}  # {"CUH_ID": (value, t), "KIT_ID": (value, t)}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_mtime = 0.0
        threading.Thread(target=self._watch_state_loop, daemon=True).start()

    def _load_state(self) -> Optional[dict]:
        try:
            if not self.state_path.exists():
                return None
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _refresh_from_state(self):
        data = self._load_state()
        if data is None:
            return
        # ดึงเฉพาะ CUH_ID, KIT_ID (จะอยู่ใน expected{} หรือ root ก็รองรับ)
        if isinstance(data.get("expected"), dict):
            cuh = data["expected"].get("CUH_ID")
            kit = data["expected"].get("KIT_ID")
        else:
            cuh = data.get("CUH_ID")
            kit = data.get("KIT_ID")

        with self._lock:
            self.expected = {"CUH_ID": cuh, "KIT_ID": kit}
            self.version = _hash_job(self.expected)
            self.cache.clear()  # เริ่มรอบใหม่
        print(f"[STATE] expected={self.expected} v={self.version[:8]}")

    def _watch_state_loop(self):
        while not self._stop.is_set():
            try:
                mtime = self.state_path.stat().st_mtime
                if mtime != self._last_mtime:
                    self._last_mtime = mtime
                    self._refresh_from_state()
                time.sleep(0.5)
            except FileNotFoundError:
                time.sleep(0.5)

    def stop(self):
        self._stop.set()

    @staticmethod
    def _field_from_sensor(ev_type: str) -> Optional[str]:
        if ev_type == "barcode": return "CUH_ID"
        if ev_type == "rfid":    return "KIT_ID"
        return None

    def on_sensor(self, ev: dict) -> dict:
        """
        ev = {
          "type": "barcode"/"rfid",
          "text": "...",
          "pos_gpio": 23,
          "ts": "2025-10-08T13:00:00"
        }
        """
        now = time.time()
        field = self._field_from_sensor(ev.get("type"))
        text  = (ev.get("text") or "").strip()

        with self._lock:
            if field and text:
                self.cache[field] = (text, now)

            # ประเมินว่าครบและตรงหรือยัง
            window_min = now - self.window_sec
            matched = {}
            complete = True
            for f in REQUIRED_FIELDS:
                got = self.cache.get(f)
                exp = self.expected.get(f)
                ok = False
                if got and got[1] >= window_min and exp:
                    ok = (got[0] == exp)
                matched[f] = ok
                complete &= ok

            result = {
                "version": self.version,
                "expected": dict(self.expected),
                "incoming": {"field": field, "value": text, "from_gpio": ev.get("pos_gpio"), "ts": ev.get("ts")},
                "matched": matched,
                "complete": complete
            }
            return result

def main():
    ap = argparse.ArgumentParser(description="match_id: verify CUH_ID & KIT_ID, then notify AMR module")
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-base", default="smartcart")
    ap.add_argument("--state-file", default="state.json", help="ไฟล์ที่ server_pi เขียน CUH_ID/KIT_ID ล่าสุด")
    ap.add_argument("--window-sec", type=float, default=12.0, help="เวลาที่ถือว่าเป็นรอบเดียวกัน")
    args = ap.parse_args()

    base = args.mqtt_base
    topic_sensor  = f"{base}/sensor"    # รับจาก detect_sensor
    topic_match   = f"{base}/match"     # รายงานระหว่างทาง/ดีบัก
    topic_trigger = f"{base}/trigger"   # ส่งให้ communicate_AMR.py (ไม่ map goal ที่นี่)

    matcher = MatchID(args.state_file, args.window_sec)

    cli = mqtt.Client(client_id="match_id")
    cli.connect(args.mqtt_host, args.mqtt_port, keepalive=30)

    last_triggered_version = None  # กันยิงซ้ำเมื่อ state ไม่เปลี่ยน

    def on_connect(c, u, f, rc):
        c.subscribe(topic_sensor, qos=1)
        print(f"[MQTT] sub {topic_sensor}")

    def on_message(c, u, msg):
        nonlocal last_triggered_version
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            print(f"[WARN] bad JSON on {msg.topic}")
            return

        if msg.topic == topic_sensor:
            res = matcher.on_sensor(payload)
            c.publish(topic_match, json.dumps(res), qos=1)  # สำหรับดูสถานะ matched/complete
            print(f"[MATCH] matched={res['matched']} complete={res['complete']} v={res['version'][:8]}")

            if res["complete"] and res["version"] != last_triggered_version:
                # ส่งสัญญาณไป communicate_AMR — ไม่ใส่ goal ที่นี่
                trigger_msg = {
                    "reason": "BOTH_MATCHED",
                    "version": res["version"],
                    "ts": payload.get("ts"),
                    "matched": res["matched"],
                    "expected": res["expected"]
                }
                c.publish(topic_trigger, json.dumps(trigger_msg), qos=1)
                last_triggered_version = res["version"]
                print(f"[TRIGGER] -> {topic_trigger}: {trigger_msg}")

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.loop_start()

    print("match_id running. Ctrl+C to quit.")
    try:
        while True: time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        matcher.stop()
        cli.loop_stop(); cli.disconnect()
        print("Stopped.")

if __name__ == "__main__":
    main()
