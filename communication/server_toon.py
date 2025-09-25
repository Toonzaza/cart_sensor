# /home/fibo/cart_ws/communication/server.py
import asyncio
import websockets
import json
import os
import time
import tempfile
import re

# ---- ที่เก็บข้อมูลฝั่ง Pi (อยู่ในโฟลเดอร์ home, ไม่ต้องใช้ sudo) ----
DATA_DIR   = os.path.expanduser("~/cart_ws/data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH   = os.path.join(DATA_DIR, "job_ids.jsonl")
os.makedirs(DATA_DIR, exist_ok=True)

CUH_PAT  = re.compile(r"^CUH[\w-]+$", re.IGNORECASE)
KIT_PAT  = re.compile(r"^MXK[\w-]+$", re.IGNORECASE)
GOAL_PAT = re.compile(r"^DOT[\w-]+$", re.IGNORECASE)

def atomic_write(path: str, text: str):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def classify_ids_from_list(tokens):
    """
    tokens: list[str] เช่น ["CUH22-1043","MXK20-1003","DOT400002"]
    return: {"cuh_id":..., "kit_id":..., "goal_id":...}
    """
    cuh = kit = goal = None
    for t in tokens:
        s = str(t).strip().upper()
        if CUH_PAT.match(s):
            cuh = s
        elif KIT_PAT.match(s):
            kit = s
        elif GOAL_PAT.match(s):
            goal = s
        else:
            # ถ้าเจอค่าไม่เข้ากฎ ปล่อยผ่าน (หรือจะ raise ก็ได้)
            pass
    if not (cuh and kit and goal):
        missing = []
        if not cuh:  missing.append("CUH*")
        if not kit:  missing.append("MXK*")
        if not goal: missing.append("DOT*")
        raise ValueError(f"missing or invalid: {', '.join(missing)}")
    return {"cuh_id": cuh, "kit_id": kit, "goal_id": goal}

async def handle_client(websocket):
    async for message in websocket:
        try:
            data = json.loads(message)

            # ------------------- NEW: รับเป็นลิสต์ -------------------
            if isinstance(data, list):
                # คาดหวังเป็น list[str] ความยาว 3
                if len(data) != 3 or not all(isinstance(x, str) for x in data):
                    await websocket.send(json.dumps({"status":"error","message":"expect list[str] of length 3"}))
                    continue

                try:
                    mapped = classify_ids_from_list(data)
                except ValueError as e:
                    await websocket.send(json.dumps({"status":"error","message":str(e)}))
                    continue

                ts = time.time()

                # เก็บสถานะล่าสุด
                state = {"latest_job_ids": {"ts": ts, **mapped}}
                atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))

                # บันทึกประวัติ
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": ts, **mapped}, ensure_ascii=False) + "\n")

                print(f"Job IDs (from list): CUH={mapped['cuh_id']} KIT={mapped['kit_id']} GOAL={mapped['goal_id']}")
                await websocket.send(json.dumps({"status":"ok","type":"job_ids","mapped":mapped}))
                continue
            # ---------------------------------------------------------

            # (ยังรองรับฟอร์แมตเดิมแบบ object ด้วย)
            if isinstance(data, dict) and data.get("type") == "new_order":
                print("New Order Received:", data)
                await websocket.send(json.dumps({"status": "received", "type": "new_order"}))

            elif isinstance(data, dict) and data.get("type") == "position":
                print("Position Received:", data.get("value"))
                await websocket.send(json.dumps({"status": "ok", "type": "position", "value": data.get("value")}))

            elif isinstance(data, dict) and data.get("type") == "job_ids":
                cuh = data.get("cuh_id")
                kit = data.get("kit_id")
                goal = data.get("goal_id")
                if not all(isinstance(x, str) and x for x in [cuh, kit, goal]):
                    await websocket.send(json.dumps({"status":"error","message":"cuh_id/kit_id/goal_id must be non-empty strings"}))
                    continue

                ts = data.get("ts", time.time())
                state = {"latest_job_ids": {"ts": ts, "cuh_id": cuh, "kit_id": kit, "goal_id": goal}}
                atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": ts, "cuh_id": cuh, "kit_id": kit, "goal_id": goal}, ensure_ascii=False) + "\n")

                print(f"Job IDs (from object): CUH={cuh} KIT={kit} GOAL={goal}")
                await websocket.send(json.dumps({"status":"ok","type":"job_ids"}))

            else:
                await websocket.send(json.dumps({"status":"error","message":"unsupported payload"}))

        except json.JSONDecodeError:
            print("Raw message:", message)
            await websocket.send(json.dumps({"status": "error", "message": "Invalid JSON"}))

async def main():
    async with websockets.serve(handle_client, "0.0.0.0", 8765):
        print("WebSocket server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

asyncio.run(main())
