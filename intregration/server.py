# app_main.py
import os, json, asyncio, websockets
from fn_server import (
    mqtt_init, mqtt_pub, now_fields, to_none,
    publish_job_topics, publish_detect_config, detect_mode,
    setup_amr_status_subscriptions, persist_state_and_log
)

# ========== WebSocket Handler ==========
async def handle_client(websocket, mqtt_cli):
    async for message in websocket:
        try:
            data = json.loads(message)

            # แบบ list [CUH, MXK, DOT]
            if isinstance(data, list):
                if len(data) != 3:
                    await websocket.send(json.dumps({"status":"error","message":"expect list length 3 [CUH, MXK, DOT]"}))
                    continue

                cuh_raw, mxk_raw, dot_raw = data
                cuh = to_none(cuh_raw)
                kit = to_none(mxk_raw)
                goal = to_none(dot_raw)

                if goal is None:
                    await websocket.send(json.dumps({"status":"error","message":"DOT must not be None"})); continue
                if cuh is None and kit is None:
                    await websocket.send(json.dumps({"status":"error","message":"either CUH or MXK must be present"})); continue

                ts, d, t, iso = now_fields()
                # <<< บันทึกลงไฟล์ (เฉพาะค่าจาก Web App) >>>
                persist_state_and_log(cuh, kit, goal, ts, d, t, iso)

                # MQTT publish ไปยังระบบตรวจจับ/แดชบอร์ด
                publish_job_topics(mqtt_cli, cuh, kit, goal, ts, d, t, iso)
                publish_detect_config(mqtt_cli, cuh, kit, goal, ts, d, t, iso)

                mode = detect_mode(cuh, kit, goal)
                print(f"[WS][{iso}] CUH={cuh} KIT={kit} GOAL={goal} MODE={mode}")
                await websocket.send(json.dumps({
                    "status":"ok","type":"job_ids",
                    "mapped":{"cuh_id":cuh,"kit_id":kit,"goal_id":goal,"mode":mode},
                    "ts": ts, "date": d, "time": t, "iso": iso
                }))
                continue

            # โหมด object (compat เดิม)
            if isinstance(data, dict) and data.get("type") == "new_order":
                print("[WS] New Order:", data)
                await websocket.send(json.dumps({"status": "received", "type": "new_order"}))
                continue

            if isinstance(data, dict) and data.get("type") == "position":
                print("[WS] Position:", data.get("value"))
                await websocket.send(json.dumps({"status": "ok", "type": "position", "value": data.get("value")}))
                continue

            if isinstance(data, dict) and data.get("type") == "job_ids":
                cuh = data.get("cuh_id"); kit = data.get("kit_id"); goal = data.get("goal_id")
                if not all(isinstance(x, str) and x for x in [cuh, kit, goal]):
                    await websocket.send(json.dumps({"status":"error","message":"cuh_id/kit_id/goal_id must be non-empty strings"})); continue

                ts, d, t, iso = now_fields()
                # <<< บันทึกลงไฟล์ (เฉพาะค่าจาก Web App) >>>
                persist_state_and_log(cuh, kit, goal, ts, d, t, iso)

                # MQTT publish
                publish_job_topics(mqtt_cli, cuh, kit, goal, ts, d, t, iso)
                publish_detect_config(mqtt_cli, cuh, kit, goal, ts, d, t, iso)

                mode = detect_mode(cuh, kit, goal)
                print(f"[WS][{iso}] OBJ CUH={cuh} KIT={kit} GOAL={goal} MODE={mode}")
                await websocket.send(json.dumps({"status":"ok","type":"job_ids","mode":mode,
                                                 "ts":ts,"date":d,"time":t,"iso":iso}))
                continue

            await websocket.send(json.dumps({"status":"error","message":"unsupported payload"}))

        except json.JSONDecodeError:
            print("[WS] Invalid JSON:", message)
            await websocket.send(json.dumps({"status": "error", "message": "Invalid JSON"}))

# ========== Main ==========
async def main():
    mqtt_cli = mqtt_init(client_id="ws-bridge-server")
    setup_amr_status_subscriptions(mqtt_cli)   # รับสถานะ AMR → พิมพ์หน้าจอ (ไม่เก็บไฟล์)

    host = os.getenv("WS_HOST", "0.0.0.0")
    port = int(os.getenv("WS_PORT", "8765"))
    async with websockets.serve(lambda ws: handle_client(ws, mqtt_cli), host, port):
        print(f"WebSocket server started on ws://{host}:{port}")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
