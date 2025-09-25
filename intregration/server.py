import asyncio
import websockets
import json
import os
import time
import tempfile
import unicodedata
import re

# === MQTT ===
import paho.mqtt.client as mqtt

# ---- ที่เก็บข้อมูลฝั่ง Pi ----
DATA_DIR   = os.path.expanduser("~/cart_ws/intregration/data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH   = os.path.join(DATA_DIR, "job_ids.jsonl")
os.makedirs(DATA_DIR, exist_ok=True)

# ---- MQTT Config (ตั้งผ่าน ENV ได้) ----
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_BASE = os.getenv("MQTT_BASE", "smartcart")
STATION_ID = os.getenv("STATION_ID", "slot1")

TOPIC_JOB_LATEST = f"{MQTT_BASE}/job/latest"                   # retained snapshot
TOPIC_JOB_EVENT  = f"{MQTT_BASE}/job/event"                    # append-only
TOPIC_DETECT_DESIRED = f"{MQTT_BASE}/detect/{STATION_ID}/desired"  # retained config
TOPIC_DETECT_MODE    = f"{MQTT_BASE}/detect/{STATION_ID}/mode"     # brief mode

_mqtt = None  # global client

# -------------------- Utilities --------------------

def atomic_write(path: str, text: str):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def to_none(x):
    """แปลง 'None' (รองรับ combining marks ไทย) -> None; อื่น ๆ คืนค่าเดิม"""
    if x is None:
        return None
    s = str(x).strip()
    s_norm = ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch))
    return None if s_norm.lower() == "none" else x

def now_fields():
    """คืน ts(d), date, time, iso (+0700)"""
    ts = time.time()
    lt = time.localtime(ts)
    return (
        ts,
        time.strftime("%Y-%m-%d", lt),
        time.strftime("%H:%M:%S", lt),
        time.strftime("%Y-%m-%dT%H:%M:%S%z", lt),
    )

def detect_mode(cuh, kit, goal):

    if goal is None:
        return None
    if cuh and kit:
        return "BOTH"
    if cuh and not kit:
        return "CUH_ONLY"
    if kit and not cuh:
        return "KIT_ONLY"
    return None

# -------------------- MQTT Wrappers --------------------

def mqtt_init():
    global _mqtt
    cli = mqtt.Client(client_id="ws-bridge-server", clean_session=True)
    if MQTT_USER:
        cli.username_pw_set(MQTT_USER, MQTT_PASS or "")
    try:
        cli.connect(MQTT_HOST, MQTT_PORT, 60)
        cli.loop_start()
        _mqtt = cli
        print(f"[MQTT] connected to {MQTT_HOST}:{MQTT_PORT}")
    except Exception as e:
        _mqtt = None
        print(f"[MQTT] connect failed: {e}")

def mqtt_pub(topic, obj, qos=0, retain=False):
    if not _mqtt:
        return
    try:
        _mqtt.publish(topic, json.dumps(obj, ensure_ascii=False), qos=qos, retain=retain)
    except Exception as e:
        print(f"[MQTT] publish error => {topic}: {e}")

def publish_job_topics(cuh, kit, goal, ts, d, t, iso):
    payload = {"ts": ts, "date": d, "time": t, "iso": iso,
               "cuh_id": cuh, "kit_id": kit, "goal_id": goal}
    # snapshot (retained) + event (non-retained)
    mqtt_pub(TOPIC_JOB_LATEST, payload, qos=1, retain=True)
    mqtt_pub(TOPIC_JOB_EVENT,  payload, qos=0, retain=False)

def publish_detect_config(cuh, kit, goal, ts, d, t, iso):
    mode = detect_mode(cuh, kit, goal)
    if not mode:
        return
    desired = {
        "req_id": f"{int(ts*1000)}",
        "mode": mode,
        "goal_id": goal,
        "cuh_id": cuh,
        "kit_id": kit,
        "edge": "rising",
        "window_ms": 1500,
        "ts": ts, "date": d, "time": t, "iso": iso
    }
    # ให้ Detect node อ่าน config ล่าสุดได้ทันทีเมื่อรีสตาร์ต → retain=True, QoS 1
    mqtt_pub(TOPIC_DETECT_DESIRED, desired, qos=1, retain=True)
    # แจ้งโหมดสั้น ๆ (optional)
    mqtt_pub(TOPIC_DETECT_MODE, {"mode": mode, "ts": ts}, qos=0, retain=False)

# -------------------- Persistence --------------------

def persist_state_and_log(cuh, kit, goal, ts, d, t, iso):
    state = {"latest_job_ids": {
        "ts": ts, "date": d, "time": t, "iso": iso,
        "cuh_id": cuh, "kit_id": kit, "goal_id": goal
    }}
    atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))

    log_row = {"ts": ts, "date": d, "time": t, "iso": iso,
               "cuh_id": cuh, "kit_id": kit, "goal_id": goal}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_row, ensure_ascii=False) + "\n")

# -------------------- WebSocket Handler --------------------

async def handle_client(websocket):
    async for message in websocket:
        try:
            data = json.loads(message)

            # --- โหมดส่งเป็น list [CUH, MXK, DOT] ---
            if isinstance(data, list):
                if len(data) != 3:
                    await websocket.send(json.dumps({"status":"error","message":"expect list length 3 [CUH, MXK, DOT]"}))
                    continue

                cuh_raw, mxk_raw, dot_raw = data
                cuh = to_none(cuh_raw)
                kit = to_none(mxk_raw)
                goal = to_none(dot_raw)

                # validation ตามกติกา
                if goal is None:
                    await websocket.send(json.dumps({"status":"error","message":"DOT must not be None"}))
                    continue
                if cuh is None and kit is None:
                    await websocket.send(json.dumps({"status":"error","message":"either CUH or MXK must be present"}))
                    continue

                ts, d, t, iso = now_fields()
                persist_state_and_log(cuh, kit, goal, ts, d, t, iso)
                publish_job_topics(cuh, kit, goal, ts, d, t, iso)
                publish_detect_config(cuh, kit, goal, ts, d, t, iso)

                mode = detect_mode(cuh, kit, goal)
                print(f"[LIST] CUH={cuh} KIT={kit} GOAL={goal} MODE={mode} @ {iso}")
                await websocket.send(json.dumps({
                    "status":"ok","type":"job_ids",
                    "mapped":{"cuh_id":cuh,"kit_id":kit,"goal_id":goal,"mode":mode},
                    "ts": ts, "date": d, "time": t, "iso": iso
                }))
                continue

            # --- โหมดเดิม: object ---
            if isinstance(data, dict) and data.get("type") == "new_order":
                print("New Order Received:", data)
                await websocket.send(json.dumps({"status": "received", "type": "new_order"}))
                continue

            if isinstance(data, dict) and data.get("type") == "position":
                print("Position Received:", data.get("value"))
                await websocket.send(json.dumps({"status": "ok", "type": "position", "value": data.get("value")}))
                continue

            if isinstance(data, dict) and data.get("type") == "job_ids":
                cuh = data.get("cuh_id")
                kit = data.get("kit_id")
                goal = data.get("goal_id")

                # ต้องเป็นสตริงไม่ว่างทุกตัว (object โหมดนี้ไม่รับ None)
                if not all(isinstance(x, str) and x for x in [cuh, kit, goal]):
                    await websocket.send(json.dumps({"status":"error","message":"cuh_id/kit_id/goal_id must be non-empty strings"}))
                    continue

                ts, d, t, iso = now_fields()
                persist_state_and_log(cuh, kit, goal, ts, d, t, iso)
                publish_job_topics(cuh, kit, goal, ts, d, t, iso)
                publish_detect_config(cuh, kit, goal, ts, d, t, iso)

                mode = detect_mode(cuh, kit, goal)
                print(f"[OBJ ] CUH={cuh} KIT={kit} GOAL={goal} MODE={mode} @ {iso}")
                await websocket.send(json.dumps({"status":"ok","type":"job_ids","mode":mode,
                                                 "ts":ts,"date":d,"time":t,"iso":iso}))
                continue

            # ไม่เข้าเงื่อนไข
            await websocket.send(json.dumps({"status":"error","message":"unsupported payload"}))

        except json.JSONDecodeError:
            print("Raw message:", message)
            await websocket.send(json.dumps({"status": "error", "message": "Invalid JSON"}))

# -------------------- Main --------------------

async def main():
    mqtt_init()
    async with websockets.serve(handle_client, "0.0.0.0", 8765):
        print("WebSocket server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
