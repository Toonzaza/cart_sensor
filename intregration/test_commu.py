#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, asyncio, websockets, sys
from typing import Set

HOST = os.getenv("WS_HOST", "0.0.0.0")
PORT = int(os.getenv("WS_PORT", "8765"))
TARGET_PUSH_IP = os.getenv("TARGET_PUSH_IP", "192.168.1.100")  # ส่งเฉพาะไคลเอนต์ IP นี้

STATE_MAP = {
    "1": "Parking",
    "2": "Going To Goals",
    "3": "Arrived Goals",
    "4": "Cart Not Clear",
    "5": "Error Need Help",
}

CONNECTED: Set[websockets.WebSocketServerProtocol] = set()

def _pretty(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)

async def broadcast_list_one(text: str):
    """ส่ง payload เป็นลิสต์ 1 ช่อง เฉพาะ client ที่ IP ตรง TARGET_PUSH_IP"""
    if not CONNECTED:
        print("[WS] No clients connected; skip broadcast.")
        return
    payload = json.dumps([text], ensure_ascii=False)
    dead = []
    sent = 0
    for ws in list(CONNECTED):
        try:
            peer = getattr(ws, "remote_address", None)
            if not peer:
                continue
            ip = peer[0]
            if ip != TARGET_PUSH_IP:
                continue
            if ws.closed:
                dead.append(ws); continue
            await ws.send(payload)
            sent += 1
        except Exception as e:
            print(f"[WS] send error to {getattr(ws, 'remote_address', None)}: {e}")
            dead.append(ws)
    for ws in dead:
        CONNECTED.discard(ws)
    print(f'[WS] Broadcast -> {payload}  (sent={sent}, clients_tracked={len(CONNECTED)})')

async def handle(ws):
    peer = getattr(ws, "remote_address", None)
    CONNECTED.add(ws)
    print(f"[WS] CONNECT from {peer} (tracked={len(CONNECTED)})")
    try:
        async for msg in ws:
            print("\n" + "="*80)
            print(f"[WS] RAW: {msg!r}")
            parsed = None
            try:
                parsed = json.loads(msg)
                print("[WS] JSON parsed OK:")
                print(_pretty(parsed))
                if isinstance(parsed, list) and len(parsed) == 6:
                    status, cuh1, cuh2, kit1, kit2, dot = parsed
                    print("\n[WS] Interpreted (new spec):")
                    print(f"  status = {status}")
                    print(f"  CUH    = [{cuh1}, {cuh2}]")
                    print(f"  KIT    = [{kit1}, {kit2}]")
                    print(f"  DOT    = {dot}")
                else:
                    print("[WS] (Note) Not a 6-item list (new spec).")
            except json.JSONDecodeError as e:
                print(f"[WS] JSON decode error: {e}")

            # ส่ง ACK กลับให้ฝั่งเว็บเห็นผล
            ack = {
                "status": "ok",
                "echo_len": len(msg),
                "is_json": parsed is not None,
                "hint": "Expect [status, CUH1, CUH2, KIT1, KIT2, DOT]"
            }
            await ws.send(json.dumps(ack, ensure_ascii=False))
            print("[WS] -> ACK sent.")
    except Exception as e:
        print(f"[WS] EXCEPTION from {peer}: {e}")
    finally:
        CONNECTED.discard(ws)
        print(f"[WS] CLOSE {peer} (tracked={len(CONNECTED)})")

async def stdin_key_loop():
    """
    อ่านคีย์จาก stdin:
      1..5 = ส่งสถานะตาม STATE_MAP
      h    = แสดงเมนู
      q    = ออกจากโปรแกรม
    """
    print_menu()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.05)
            continue
        key = line.strip()
        if key.lower() == "q":
            print("[WS] Quit requested. Shutting down...")
            for ws in list(CONNECTED):
                try: await ws.close()
                except: pass
            raise SystemExit(0)
        if key.lower() == "h":
            print_menu(); continue
        if key in STATE_MAP:
            await broadcast_list_one(STATE_MAP[key])
        else:
            print("[WS] Unknown key. Press 'h' for help.")

def print_menu():
    print("\n=== AMR State Simulator (Target IP: {}) ===".format(TARGET_PUSH_IP))
    print("  1 = Parking")
    print("  2 = Going To Goals")
    print("  3 = Arrived Goals")
    print("  4 = Cart Not Clear")
    print("  5 = Error Need Help")
    print("  h = Help / show this menu")
    print("  q = Quit")
    print("----------------------------")
    print(f"WebSocket server: ws://{HOST}:{PORT}")
    print("Type 1-5 then Enter to broadcast to the target IP only.\n")

async def main():
    print(f"[WS] Starting server on ws://{HOST}:{PORT} (TARGET_PUSH_IP={TARGET_PUSH_IP})")
    async with websockets.serve(
        handle, HOST, PORT,
        max_size=None, max_queue=None, ping_interval=20, ping_timeout=20
    ):
        await stdin_key_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        print("\n[WS] KeyboardInterrupt - bye.")
