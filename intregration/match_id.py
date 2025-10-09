#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, signal
import paho.mqtt.client as mqtt

# ---- FIXED PATHS / TOPICS ----
STATE_PATH = "/home/fibo/cart_ws/intregration/data/state.json"
MQTT_HOST  = "127.0.0.1"
MQTT_PORT  = 1883
BASE       = "smartcart"

SUB_TOPIC        = f"{BASE}/sensor"           # รับจาก detect_sensor (payload ย่อ)
PUB_MATCH_TOPIC  = f"{BASE}/match"            # รายงานผล match
AMR_TOGGLE_TOPIC = f"{BASE}/toggle_omron"     # <<< trigger ไป communicate_AMR

# ---- helpers ----
_last_job = {}

def _norm(s): return None if s is None else str(s).strip()

def _load_state():
    global _last_job
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        job = st.get("latest_job_ids") or {}
        _last_job = job
    except Exception as e:
        print(f"[STATE] load failed ({STATE_PATH}): {e}")
        job = _last_job or {}
    cuh = _norm(job.get("cuh_id"))
    kit = _norm(job.get("kit_id"))
    goal = job.get("goal_id")
    return cuh, kit, goal, job

class MatchState:
    def __init__(self): self.reset()
    def reset(self):
        self.cuh_ok = False
        self.kit_ok = False
        self.seen = {}
    def as_dict(self): return {"cuh_id": self.cuh_ok, "kit_id": self.kit_ok}

ms = MatchState()

def on_connect(client, userdata, flags, rc):
    print("match_id running. Ctrl+C to quit.")
    print(f"[MQTT] sub {SUB_TOPIC}")
    client.subscribe(SUB_TOPIC)

def on_message(client, userdata, msg):
    # 1) payload in
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[MQTT] bad payload: {e}")
        return
    sensor = (payload.get("sensor") or "").strip()
    gpio   = payload.get("gpio")
    value  = payload.get("value") or {}

    # 2) state
    cuh_need, kit_need, goal_id, latest_job = _load_state()
    cuh_required = (cuh_need is not None)
    kit_required = (kit_need is not None)

    # 3) update match flags
    if sensor.startswith("barcode"):
        scanned = _norm(value.get("code"))
        ms.seen["barcode"] = scanned
        ms.cuh_ok = (scanned == cuh_need) if cuh_required else True
        print(f"[MATCH] BARCODE gpio={gpio} code='{scanned}' vs cuh_id='{cuh_need}' -> {ms.cuh_ok}")
    elif sensor.startswith("rfid"):
        kit_scan = _norm(value.get("ascii")) or _norm(value.get("epc"))
        ms.seen["rfid"] = kit_scan
        ms.kit_ok = (kit_scan == kit_need) if kit_required else True
        print(f"[MATCH] RFID gpio={gpio} read='{kit_scan}' vs kit_id='{kit_need}' -> {ms.kit_ok}")

    # 4) ensure non-required fields pass
    if not cuh_required: ms.cuh_ok = True
    if not kit_required: ms.kit_ok = True

    # 5) complete when all required pass
    complete = ((not cuh_required) or ms.cuh_ok) and ((not kit_required) or ms.kit_ok)

    out = {
        "latest_job_ids": latest_job,
        "matched": ms.as_dict(),
        "complete": complete,
        "seen": ms.seen,
        "ts": time.time()
    }
    client.publish(PUB_MATCH_TOPIC, json.dumps(out, ensure_ascii=False), qos=0, retain=False)
    print(f"[MQTT] pub {PUB_MATCH_TOPIC}: {out}")

    # 6) trigger OMROM AMR when complete
    if complete:
        toggle = {
            "reason": "match_complete",
            "latest_job_ids": latest_job,     # มี cuh_id / kit_id / goal_id ตามไฟล์
            "goal_id": latest_job.get("goal_id"),
            "ts": time.time()
        }
        client.publish(AMR_TOGGLE_TOPIC, json.dumps(toggle, ensure_ascii=False), qos=1, retain=False)
        print(f"[AMR] 🔔 trigger -> {AMR_TOGGLE_TOPIC}: {toggle}")
        ms.reset()

def main():
    cli = mqtt.Client(client_id="match_id")
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, 30)

    def _exit(*_):
        try: cli.loop_stop(); cli.disconnect()
        finally: os._exit(0)

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)
    cli.loop_forever()

if __name__ == "__main__":
    main()
