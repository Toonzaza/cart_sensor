# main_server.py
import os, json, asyncio, websockets
from fn_server import (
    mqtt_init, now_fields, normalize_ids, _to_none_token,
    publish_job_topics, publish_detect_config, detect_mode_any,
    setup_amr_status_subscriptions, persist_state_and_log, _fill_two_slots,
    MQTT_BASE, STATE_PATH
)

# ========== WebSocket Handler ==========
async def handle_client(websocket, mqtt_cli):
    async for message in websocket:
        # 1) parse
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            await websocket.send(json.dumps({"status":"error","message":"Invalid JSON"}))
            continue

        # 2) only accept list of 6: [OP, CUH1, CUH2, MXK1, MXK2, DOT]
        if not (isinstance(data, list) and len(data) == 6):
            await websocket.send(json.dumps({"status":"error","message":"expect list [OP,CUH1,CUH2,MXK1,MXK2,DOT]"}))
            continue

        op, cuh1, cuh2, mxk1, mxk2, dot_raw = data
        if op not in ("Request", "Return"):
            await websocket.send(json.dumps({"status":"error","message":"OP must be 'Request' or 'Return'"}))
            continue

        cuh_ids = normalize_ids([cuh1, cuh2])
        kit_ids = normalize_ids([mxk1, mxk2])
        goal = _to_none_token(dot_raw)

        cuh2_arr = _fill_two_slots(cuh_ids)
        kit2_arr = _fill_two_slots(kit_ids)

        # validations
        if goal is None:
            await websocket.send(json.dumps({"status":"error","message":"goal_id must not be None"}))
            continue
        if (cuh2_arr[0] is None and cuh2_arr[1] is None) and (kit2_arr[0] is None and kit2_arr[1] is None):
            await websocket.send(json.dumps({"status":"error","message":"at least one of CUH/KIT must be present"}))
            continue

        ts, d, t, iso = now_fields()

        # 3) persist (write state.json) + publish MQTT
        persist_state_and_log([x for x in cuh2_arr if x], [x for x in kit2_arr if x], goal, ts, d, t, iso)

        # ----- inject OP into state.json (ให้ FSM อ่าน op ได้แน่) -----
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            latest = st.get("latest_job_ids") or {}
            latest["op"] = op
            st["latest_job_ids"] = latest
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as g:
                json.dump(st, g, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            print(f"[STATE] failed to write op: {e}")

        publish_job_topics(mqtt_cli, [x for x in cuh2_arr if x], [x for x in kit2_arr if x], goal, ts, d, t, iso)
        publish_detect_config(mqtt_cli, [x for x in cuh2_arr if x], [x for x in kit2_arr if x], goal, ts, d, t, iso)

        mode = detect_mode_any([x for x in cuh2_arr if x], [x for x in kit2_arr if x], goal)
        mapped = {"cuh_ids": cuh2_arr, "kit_ids": kit2_arr, "goal_id": goal, "mode": mode, "op": op}

        print(f"[WS][{iso}] LIST op={op} cuh={cuh2_arr} kit={kit2_arr} goal={goal} mode={mode}")

        await websocket.send(json.dumps({
            "status":"ok","type":"job_ids","mapped":mapped,
            "ts":ts,"date":d,"time":t,"iso":iso
        }))

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

