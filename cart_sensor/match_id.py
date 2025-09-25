#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, os, time, threading
import paho.mqtt.client as mqtt

GOALS_FILE = "/opt/smartcart/goals.json"   # ไฟล์ .json เดิมที่คุณเก็บ
TOPIC_SUBS = [
    ("smartcart/read/barcode/+", 0),
    ("smartcart/read/rfid", 0),
]
TOPIC_PUB_MATCH = "smartcart/event/match"

class GoalsStore:
    def __init__(self, path):
        self.path = path
        self.mtime = 0
        self.data = {}
        self.lock = threading.Lock()

    def _load(self):
        try:
            st = os.stat(self.path)
            if st.st_mtime <= self.mtime:
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self.lock:
                self.data = data
                self.mtime = st.st_mtime
            print(f"[GOALS] loaded {self.path} (mtime={self.mtime})")
        except FileNotFoundError:
            print(f"[GOALS] not found: {self.path}")
        except Exception as e:
            print(f"[GOALS] load error: {e}")

    def get_snapshot(self):
        self._load()
        with self.lock:
            return self.data

gs = GoalsStore(GOALS_FILE)

def try_match(payload: dict) -> dict|None:
    """
    payload: จาก MQTT ฝั่ง reader (barcode หรือ rfid)
    โครง goals.json: ยืดหยุ่นได้ เช่น list ของ objects
    [
      {"carrier_id":"A-1111","lot_id":"etghfhj5b1","device":"sdf...", "die_type":"fd45a-4s", "goal":"DOT400002"},
      ...
    ]
    """
    goals = gs.get_snapshot()
    if not isinstance(goals, list):  # ปรับตามสกีมาที่คุณใช้จริง
        return None

    # ดึงคีย์จาก payload ที่จะใช้ match
    barcode = payload.get("code", "").strip() if payload.get("type") == "barcode" else ""
    rfid_ascii = payload.get("ascii", "").strip() if payload.get("type") == "rfid" else ""
    epc = payload.get("epc", "").strip() if payload.get("type") == "rfid" else ""

    # กติกา match (ตัวอย่าง): ถ้าข้อมูลใน goals มี field "carrier_id" เท่ากับ barcode หรือ rfid_ascii
    for g in goals:
        try:
            if barcode and g.get("carrier_id","").strip() == barcode:
                return {"matched_by":"barcode","goal": g}
            if rfid_ascii and g.get("carrier_id","").strip() == rfid_ascii:
                return {"matched_by":"rfid_ascii","goal": g}
            if epc and g.get("epc","").strip().lower() == epc.lower():
                return {"matched_by":"epc","goal": g}
        except Exception:
            continue
    return None

def on_message(cli, u, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        return

    res = try_match(payload)
    if res:
        out = {
            "type": "match.ok",
            "by": res["matched_by"],
            "goal": res["goal"],
            "source": payload.get("source"),
            "device": payload.get("device"),
            "ts": int(time.time()*1000)
        }
        print("[MATCH] OK:", out)
        cli.publish(TOPIC_PUB_MATCH, json.dumps(out, ensure_ascii=False), qos=1, retain=False)
    else:
        # ถ้าต้องแจ้งกรณีไม่ตรง
        out = {
            "type": "match.none",
            "source": payload.get("source"),
            "code": payload.get("code") or payload.get("ascii") or payload.get("epc"),
            "device": payload.get("device"),
            "ts": int(time.time()*1000)
        }
        print("[MATCH] NONE:", out)
        # จะไม่ publish ก็ได้ หรือใช้หัวข้อ smartcart/event/match_none
        # cli.publish("smartcart/event/match_none", json.dumps(out, ensure_ascii=False), qos=0)

def main():
    cli = mqtt.Client(client_id="match-id-node", clean_session=True)
    cli.connect("127.0.0.1", 1883, 60)
    cli.on_message = on_message
    for t, q in TOPIC_SUBS:
        cli.subscribe(t, q)
    print("[MQTT] subscribed:", TOPIC_SUBS)
    cli.loop_forever()

if __name__ == "__main__":
    main()
