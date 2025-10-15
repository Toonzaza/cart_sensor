# main_server.py
import os, json, asyncio, websockets
from fn_server import (
    mqtt_init, now_fields, normalize_ids, _to_none_token,
    publish_job_topics, publish_detect_config, detect_mode_any,
    setup_amr_status_subscriptions, persist_state_and_log, _fill_two_slots
)

# ========== WebSocket Handler ==========
async def handle_client(websocket, mqtt_cli):
    async for message in websocket:
        try:
            data = json.loads(message)

            # ===== list mode =====
            if isinstance(data, list):
                if len(data) == 3:
                    cuh_raw, mxk_raw, dot_raw = data
                    cuh_ids = normalize_ids([cuh_raw])
                    kit_ids = normalize_ids([mxk_raw])
                    goal = _to_none_token(dot_raw)
                elif len(data) == 5:
                    cuh1, cuh2, mxk1, mxk2, dot_raw = data
                    cuh_ids = normalize_ids([cuh1, cuh2])
                    kit_ids = normalize_ids([mxk1, mxk2])
                    goal = _to_none_token(dot_raw)
                else:
                    await websocket.send(json.dumps({"status":"error","message":"expect list length 3 or 5"}))
                    continue

                cuh2 = _fill_two_slots(cuh_ids)
                kit2 = _fill_two_slots(kit_ids)

                if goal is None:
                    await websocket.send(json.dumps({"status":"error","message":"goal_id must not be None"})); continue
                if (cuh2[0] is None and cuh2[1] is None) and (kit2[0] is None and kit2[1] is None):
                    await websocket.send(json.dumps({"status":"error","message":"either CUH or KIT must be present"})); continue

                ts, d, t, iso = now_fields()
                persist_state_and_log([x for x in cuh2 if x], [x for x in kit2 if x], goal, ts, d, t, iso)
                publish_job_topics(mqtt_cli, [x for x in cuh2 if x], [x for x in kit2 if x], goal, ts, d, t, iso)
                publish_detect_config(mqtt_cli, [x for x in cuh2 if x], [x for x in kit2 if x], goal, ts, d, t, iso)

                mode = detect_mode_any([x for x in cuh2 if x], [x for x in kit2 if x], goal)
                mapped = {
                    "cuh_ids": cuh2, "kit_ids": kit2, "goal_id": goal, "mode": mode
                }
                if cuh2[0] is not None: mapped["cuh_id"] = cuh2[0]
                if kit2[0] is not None: mapped["kit_id"] = kit2[0]

                print(f"[WS][{iso}] LIST cuh_ids={cuh2} kit_ids={kit2} goal={goal} mode={mode}")
                await websocket.send(json.dumps({
                    "status":"ok","type":"job_ids","mapped":mapped,
                    "ts": ts, "date": d, "time": t, "iso": iso
                }))
                continue

            # ===== object mode =====
            if isinstance(data, dict) and data.get("type") == "new_order":
                print("[WS] New Order:", data)
                await websocket.send(json.dumps({"status": "received", "type": "new_order"}))
                continue

            if isinstance(data, dict) and data.get("type") == "position":
                print("[WS] Position:", data.get("value"))
                await websocket.send(json.dumps({"status": "ok", "type": "position", "value": data.get("value")}))
                continue

            if isinstance(data, dict) and data.get("type") == "job_ids":
                cuh_ids = normalize_ids(data.get("cuh_ids", []))
                kit_ids = normalize_ids(data.get("kit_ids", []))
                if not cuh_ids and "cuh_id" in data:
                    x = _to_none_token(data.get("cuh_id"))
                    if x: cuh_ids = [x]
                if not kit_ids and "kit_id" in data:
                    x = _to_none_token(data.get("kit_id"))
                    if x: kit_ids = [x]
                goal = _to_none_token(data.get("goal_id"))

                cuh2 = _fill_two_slots(cuh_ids)
                kit2 = _fill_two_slots(kit_ids)

                if goal is None:
                    await websocket.send(json.dumps({"status":"error","message":"goal_id must not be None"})); continue
                if (cuh2[0] is None and cuh2[1] is None) and (kit2[0] is None and kit2[1] is None):
                    await websocket.send(json.dumps({"status":"error","message":"either CUH or KIT must be present"})); continue

                ts, d, t, iso = now_fields()
                persist_state_and_log([x for x in cuh2 if x], [x for x in kit2 if x], goal, ts, d, t, iso)
                publish_job_topics(mqtt_cli, [x for x in cuh2 if x], [x for x in kit2 if x], goal, ts, d, t, iso)
                publish_detect_config(mqtt_cli, [x for x in cuh2 if x], [x for x in kit2 if x], goal, ts, d, t, iso)

                mode = detect_mode_any([x for x in cuh2 if x], [x for x in kit2 if x], goal)
                print(f"[WS][{iso}] OBJ cuh_ids={cuh2} kit_ids={kit2} goal={goal} mode={mode}")
                await websocket.send(json.dumps({
                    "status":"ok","type":"job_ids","mode":mode,
                    "ts":ts,"date":d,"time":t,"iso":iso
                }))
                continue

            await websocket.send(json.dumps({"status":"error","message":"unsupported payload"}))

        except json.JSONDecodeError:
            print("[WS] Invalid JSON:", message)
            await websocket.send(json.dumps({"status": "error", "message": "Invalid JSON"}))

# ========== Main ==========
async def main():
    mqtt_cli = mqtt_init(client_id="ws-bridge-server")
    setup_amr_status_subscriptions(mqtt_cli)
    host = os.getenv("WS_HOST", "0.0.0.0")
    port = int(os.getenv("WS_PORT", "8765"))
    async with websockets.serve(lambda ws: handle_client(ws, mqtt_cli), host, port):
        print(f"WebSocket server started on ws://{host}:{port}")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
