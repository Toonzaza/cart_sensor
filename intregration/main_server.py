#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, websockets
from typing import Any, Dict, List, Optional, Tuple

from fn_server import (
    mqtt_init, now_fields,
    publish_job_topics, publish_detect_config, detect_mode_any,
    setup_amr_status_subscriptions, persist_state_and_log, _fill_two_slots,
    validate_and_map_goal, map_status_to_op
)

# -------- Normalizer (KEEP POSITIONS) --------
def _canon_keep(x: Any) -> Optional[str]:
    """
    รักษาตำแหน่ง: 'None'/None/"" -> None, อื่น ๆ -> str(x)
    """
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() == "none":
        return None
    return s

def normalize_payload(data: Any) -> Tuple[Optional[Dict[str, Any]], List[str], List[str]]:
    """
    พยายามรับทุกรูปแบบ แล้วคืน:
    - norm: {op, cuh_ids[2], kit_ids[2], goal_id, goal_name}  # คงตำแหน่ง
    - errors: รายการสาเหตุไม่ผ่าน (ถ้าไม่ว่าง -> ไม่ประมวลผล)
    - warnings: ข้อควรทราบ แต่ยังประมวลผลได้

    รูปแบบที่ลอง:
    1) list[6]: [status|op, CUH1, CUH2, KIT1, KIT2, DOT]
    2) list[5]: [CUH1, CUH2, KIT1, KIT2, DOT]
    3) object: {status|op, cuh_ids, kit_ids, dot|goal_id|goal}
    """
    errors, warns = [], []

    op: Optional[str] = None
    c1 = c2 = k1 = k2 = None
    dot_raw: Optional[str] = None

    try:
        if isinstance(data, list):
            if len(data) == 6:
                s_or_op, c1, c2, k1, k2, last = data
                op = map_status_to_op(s_or_op)  # approved/returning/request/return
                dot_raw = last
            elif len(data) == 5:
                # legacy: ไม่มี status/op -> default ใช้ Request
                c1, c2, k1, k2, last = data
                op = "Request"
                warns.append("legacy-5-items: default OP=Request")
                dot_raw = last
            else:
                errors.append("unsupported list length (expect 5 or 6)")
        elif isinstance(data, dict):
            # flexible object (ยอมรับ keyed)
            op = map_status_to_op(data.get("status")) or map_status_to_op(data.get("op"))
            cuh = data.get("cuh_ids") or [None, None]
            kit = data.get("kit_ids") or [None, None]
            # รองรับทั้ง list ยาว >=2 หรือ <2
            c1 = cuh[0] if len(cuh) >= 1 else None
            c2 = cuh[1] if len(cuh) >= 2 else None
            k1 = kit[0] if len(kit) >= 1 else None
            k2 = kit[1] if len(kit) >= 2 else None
            dot_raw = data.get("dot") or data.get("goal_id") or data.get("goal")
        else:
            errors.append("payload must be list or object")
    except Exception as e:
        errors.append(f"normalize exception: {e}")

    # --- Canonicalize (KEEP POSITIONS) ---
    cuh2 = [_canon_keep(c1), _canon_keep(c2)]
    kit2 = [_canon_keep(k1), _canon_keep(k2)]

    # --- validations ---
    if not op:
        errors.append("missing/invalid status or op (expect: approved/returning or Request/Return)")

    # ต้องมีอย่างน้อย 1 ค่าใน 4 ช่องแรก
    if all(v is None for v in (cuh2[0], cuh2[1], kit2[0], kit2[1])):
        errors.append("at least one of CUH/KIT must be present")

    goal_id, goal_name, gerr = validate_and_map_goal(dot_raw)
    if gerr:
        errors.append(gerr)

    if errors:
        return None, errors, warns

    # ✅ คืนแบบคงตำแหน่ง (ไม่กรอง/ไม่รัดรูป)
    return {
        "op": op,
        "cuh_ids": cuh2,   # เช่น [None, "CUH22-1030"]
        "kit_ids": kit2,   # เช่น [None, "MXK22-1049"]
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
            cuh_ids = norm["cuh_ids"]   # คงตำแหน่ง
            kit_ids = norm["kit_ids"]   # คงตำแหน่ง
            goal_id = norm["goal_id"]
            goal_name = norm["goal_name"]

            ts, d, t, iso = now_fields()

            # persist + MQTT + detect
            persist_state_and_log(cuh_ids, kit_ids, goal_id, ts, d, t, iso, op=op, goal_name=goal_name)
            publish_job_topics(mqtt_cli, cuh_ids, kit_ids, goal_id, ts, d, t, iso, goal_name=goal_name)
            publish_detect_config(mqtt_cli, cuh_ids, kit_ids, goal_id, ts, d, t, iso)

            mode = detect_mode_any([x for x in cuh_ids if x is not None],
                                   [x for x in kit_ids if x is not None],
                                   goal_id)

            mapped = {
                "op": op,
                "cuh_ids": _fill_two_slots([x for x in cuh_ids if True]),  # cuh_ids ยาว 2 อยู่แล้ว
                "kit_ids": _fill_two_slots([x for x in kit_ids if True]),
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

