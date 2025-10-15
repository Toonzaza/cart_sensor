#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, signal, unicodedata
import paho.mqtt.client as mqtt

STATE_PATH = "/home/fibo/cart_ws/intregration/data/state.json"
MQTT_HOST  = "127.0.0.1"
MQTT_PORT  = 1883
BASE       = "smartcart"

SUB_TOPIC        = f"{BASE}/sensor"
PUB_MATCH_TOPIC  = f"{BASE}/match"
AMR_TOGGLE_TOPIC = f"{BASE}/toggle_omron"
LED_CMD_TOPIC    = f"{BASE}/led/cmd"

# trigger GPIO -> index
CUH_TRIGGER_INDEX = {23: 0, 24: 1}
KIT_TRIGGER_INDEX = {25: 0, 16: 1}

LED_GPIO = {
    "cuh1": {"green": 20, "red": 21},
    "cuh2": {"green": 17, "red": 27},
    "kit1": {"green": 5,  "red": 6 },
    "kit2": {"green": 13, "red": 19},
}

_last_job = {}

def _strip_combining(s: str) -> str:
    return ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch))

def _norm_token(x):
    if x is None: return None
    s = str(x).strip()
    s_norm = _strip_combining(s).lower()
    return None if s_norm == "none" or s == "" else s

def _load_state():
    """คืน (cuh_ids2, kit_ids2, goal, job_raw) โดยอาเรย์ยาว 2 ช่อง + None ได้"""
    global _last_job
    job = {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        job = st.get("latest_job_ids") or {}
        _last_job = job
    except Exception as e:
        print(f"[STATE] load failed ({STATE_PATH}): {e}")
        job = _last_job or {}

    cuh2 = job.get("cuh_ids") or [job.get("cuh_id"), None]
    kit2 = job.get("kit_ids") or [job.get("kit_id"), None]

    # normalize ทั้งสองช่อง (คง None ถ้า null)
    cuh2 = [ _norm_token(x) for x in (cuh2[:2] + [None, None])[:2] ]
    kit2 = [ _norm_token(x) for x in (kit2[:2] + [None, None])[:2] ]
    goal = _norm_token(job.get("goal_id"))
    return cuh2, kit2, goal, job

class MatchState:
    def __init__(self): self.reset()
    def reset(self):
        self.cuh_ok = False
        self.kit_ok = False
        self.seen = {}
        self.matched_values = {"cuh": None, "kit": None}
    def as_dict(self):
        return {"cuh_id": self.cuh_ok, "kit_id": self.kit_ok}

ms = MatchState()

def _publish_led(client, target: str, result: str):
    pins = LED_GPIO.get(target)
    if not pins:
        print(f"[LED] unknown target '{target}', skip")
        return
    payload = {
        "target": target,
        "result": result,  # "ok" | "nok" | "skip"
        "green_gpio": pins["green"],
        "red_gpio": pins["red"],
        "ts": time.time()
    }
    client.publish(LED_CMD_TOPIC, json.dumps(payload, ensure_ascii=False), qos=1, retain=False)
    print(f"[LED] cmd -> {LED_CMD_TOPIC}: {payload}")

def on_connect(client, userdata, flags, rc):
    print("match_id running. Ctrl+C to quit.")
    print(f"[MQTT] sub {SUB_TOPIC}")
    client.subscribe(SUB_TOPIC)

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[MQTT] bad payload: {e}")
        return

    sensor = (payload.get("sensor") or "").strip()
    gpio   = payload.get("gpio")
    value  = payload.get("value") or {}

    cuh2, kit2, goal_id, latest_job = _load_state()
    cuh_required = any(x is not None for x in cuh2)
    kit_required = any(x is not None for x in kit2)

    if sensor.startswith("barcode"):
        scanned = _norm_token(value.get("code"))
        ms.seen["barcode"] = scanned

        if cuh_required:
            ms.cuh_ok = (scanned is not None) and (scanned in [x for x in cuh2 if x is not None])
            if ms.cuh_ok: ms.matched_values["cuh"] = scanned
        else:
            ms.cuh_ok = True

        idx = CUH_TRIGGER_INDEX.get(gpio, None)
        if idx is None:
            print(f"[MATCH] BARCODE gpio={gpio} (unknown trigger index)")
        else:
            expect = cuh2[idx] if idx < 2 else None
            target = "cuh1" if idx == 0 else "cuh2"
            if expect is None:
                _publish_led(client, target, "skip")
                print(f"[MATCH] BARCODE gpio={gpio} code='{scanned}' vs (none) -> SKIP")
            else:
                ok = (scanned == expect)
                _publish_led(client, target, "ok" if ok else "nok")
                print(f"[MATCH] BARCODE gpio={gpio} code='{scanned}' vs expect='{expect}' -> {ok}")

    elif sensor.startswith("rfid"):
        kit_scan = _norm_token(value.get("ascii")) or _norm_token(value.get("epc"))
        ms.seen["rfid"] = kit_scan

        if kit_required:
            ms.kit_ok = (kit_scan is not None) and (kit_scan in [x for x in kit2 if x is not None])
            if ms.kit_ok: ms.matched_values["kit"] = kit_scan
        else:
            ms.kit_ok = True

        idx = KIT_TRIGGER_INDEX.get(gpio, None)
        if idx is None:
            print(f"[MATCH] RFID gpio={gpio} (unknown trigger index)")
        else:
            expect = kit2[idx] if idx < 2 else None
            target = "kit1" if idx == 0 else "kit2"
            if expect is None:
                _publish_led(client, target, "skip")
                print(f"[MATCH] RFID gpio={gpio} read='{kit_scan}' vs (none) -> SKIP")
            else:
                ok = (kit_scan == expect)
                _publish_led(client, target, "ok" if ok else "nok")
                print(f"[MATCH] RFID gpio={gpio} read='{kit_scan}' vs expect='{expect}' -> {ok}")

    complete = ((not cuh_required) or ms.cuh_ok) and ((not kit_required) or ms.kit_ok)

    out = {
        "latest_job_ids": latest_job,
        "required": {"cuh": cuh_required, "kit": kit_required},
        "matched": ms.as_dict(),
        "matched_values": ms.matched_values,
        "seen": ms.seen,
        "complete": complete,
        "ts": time.time()
    }
    client.publish(PUB_MATCH_TOPIC, json.dumps(out, ensure_ascii=False), qos=0, retain=False)
    print(f"[MQTT] pub {PUB_MATCH_TOPIC}: {out}")

    if complete:
        toggle = {
            "reason": "match_complete",
            "latest_job_ids": latest_job,
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
        try:
            cli.loop_stop(); cli.disconnect()
        finally:
            os._exit(0)

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)
    cli.loop_forever()

if __name__ == "__main__":
    main()
