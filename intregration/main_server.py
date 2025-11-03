#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, websockets
from typing import Any, Dict, List, Optional, Tuple

from fn_server import (
    mqtt_init, now_fields, normalize_ids,
    publish_job_topics, publish_detect_config, detect_mode_any,
    setup_amr_status_subscriptions, persist_state_and_log, _fill_two_slots,
    validate_and_map_goal, map_status_to_op
)

# -------- Normalizer --------
def normalize_payload(data: Any) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """
    พยายามรับทุกรูปแบบ แล้วคืน:
    - norm: {op, cuh_ids, kit_ids, goal_id, goal_name}
    - errors: รายการสาเหตุไม่ผ่าน (ถ้าไม่ว่าง -> ไม่ประมวลผล)
    - warnings: ข้อควรทราบ แต่ยังประมวลผลได้

    รูปแบบที่ลอง:
    1) list[6]: [status|op, CUH1, CUH2, KIT1, KIT2, DOT]
    2) list[5]: [CUH1, CUH2, KIT1, KIT2, DOT]
    3) object: {status|op, cuh_ids, kit_ids, dot|goal_id}
    """
    errors, warns = [], []

    op: Optional[str] = None
    cuh_raw: List[Any] = []
    kit_raw: List[Any] = []
    dot_raw: Optional[str] = None

    try:
        if isinstance(data, list):
            if len(data) == 6:
                s_or_op, c1, c2, k1, k2, last = data
                op = map_status_to_op(s_or_op)  # approved/returning/request/return
                cuh_raw = [c1, c2]
                kit_raw = [k1, k2]
                dot_raw = last
            elif len(data) == 5:
                # legacy: ไม่มี status/op -> default ใช้ Request
                c1, c2, k1, k2, last = data
                op = "Request"
                warns.append("legacy-5-items: default OP=Request")
                cuh_raw = [c1, c2]
                kit_raw = [k1, k2]
                dot_raw = last
            else:
                errors.append("unsupported list length (expect 5 or 6)")
        elif isinstance(data, dict):
            # flexible object
            op = map_status_to_op(data.get("status")) or map_status_to_op(data.get("op"))
            cuh_raw = data.get("cuh_ids") or []
            kit_raw = data.get("kit_ids") or []
            dot_raw = data.get("dot") or data.get("goal_id") or data.get("goal")
        else:
            errors.append("payload must be list or object")
    except Exception as e:
        errors.append(f"normalize exception: {e}")

    # sanitize lists
    cuh_ids = normalize_ids(cuh_raw)
    kit_ids = normalize_ids(kit_raw)
    cuh2 = _fill_two_slots(cuh_ids)
    kit2 = _fill_two_slots(kit_ids)

    # validations (collect errors butอย่าพัง)
    if not op:
        errors.append("missing/invalid status or op (expect: approved/returning or Request/Return)")
    if (cuh2[0] is None and cuh2[1] is None) and (kit2[0] is None and kit2[1] is None):
        errors.append("at least one of CUH/KIT must be present")

    goal_id, goal_name, gerr = validate_and_map_goal(dot_raw)
    if gerr:
        errors.append(gerr)

    if errors:
        return None, errors, warns

    return {
        "op": op,
        "cuh_ids": [x for x in cuh2 if x],
        "kit_ids": [x for x in kit2 if x],
        "goal_id": goal_id,
        "goal_name": goal_name
    }, errors, warns

# -------- WebSocket Handler --------
async def handle_client(websocket, mqtt_cli):
    peer = getattr(websocket, "remote_address", None)
    print(f"[WS] CONNECT from {peer}")
    try:
        async for message in websocket:
            print("\n" + "="*80)
            print(f"[WS] RX raw: {message!r}")

            # parse JSON
            try:
                data = json.loads(message)
            except json.JSONDecodeError as e:
                err = f"Invalid JSON: {e}"
                print("[WS] " + err)
                await websocket.send(json.dumps({"status":"error","errors":[err]}))
                continue

            norm, errs, warns = normalize_payload(data)
            if errs:
                print("[WS] INVALID:", errs, "| warns:", warns)
                await websocket.send(json.dumps({
                    "status":"error",
                    "errors": errs,
                    "warnings": warns
                }))
                continue

            # valid → process
            op = norm["op"]
            cuh_ids = norm["cuh_ids"]
            kit_ids = norm["kit_ids"]
            goal_id = norm["goal_id"]
            goal_name = norm["goal_name"]

            ts, d, t, iso = now_fields()

            # persist + MQTT + detect
            persist_state_and_log(cuh_ids, kit_ids, goal_id, ts, d, t, iso, op=op, goal_name=goal_name)
            publish_job_topics(mqtt_cli, cuh_ids, kit_ids, goal_id, ts, d, t, iso, goal_name=goal_name)
            publish_detect_config(mqtt_cli, cuh_ids, kit_ids, goal_id, ts, d, t, iso)

            mode = detect_mode_any(cuh_ids, kit_ids, goal_id)
            mapped = {
                "op": op,
                "cuh_ids": _fill_two_slots(cuh_ids),
                "kit_ids": _fill_two_slots(kit_ids),
                "goal_id": goal_id,
                "goal_name": goal_name,
                "mode": mode
            }

            print(f"[WS][{iso}] OK op={op} cuh={mapped['cuh_ids']} kit={mapped['kit_ids']} goal_id={goal_id} -> {goal_name} mode={mode}")
            await websocket.send(json.dumps({
                "status":"ok","type":"job_ids","mapped":mapped,
                "warnings": warns,
                "ts":ts,"date":d,"time":t,"iso":iso
            }))
    except Exception as e:
        print(f"[WS] EXC from {peer}: {e}")
    finally:
        print(f"[WS] CLOSE {peer}")

# -------- Main --------
async def main():
    mqtt_cli = mqtt_init(client_id="ws-bridge-server")
    setup_amr_status_subscriptions(mqtt_cli)
    host = os.getenv("WS_HOST", "0.0.0.0")
    port = int(os.getenv("WS_PORT", "8765"))
    async with websockets.serve(
        lambda ws: handle_client(ws, mqtt_cli),
        host, port,
        max_size=None, max_queue=None, ping_interval=20, ping_timeout=20
    ):
        print(f"WebSocket server started on ws://{host}:{port}")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
