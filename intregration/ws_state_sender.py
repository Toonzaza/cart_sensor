#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, time, asyncio, websockets

WS_URL = os.getenv("WS_URL", "ws://192.168.1.101:8765")

STATE_MAP = {
    "1": "Parking",
    "2": "Going To Goals",
    "3": "Arrived Goals",
    "4": "Cart Not Clear",
    "5": "Error Need Help",
}

HELP = """
=== AMR State Sender ===
Connected to: {url}
Keys:
  1 = Parking
  2 = Going To Goals
  3 = Arrived Goals
  4 = Cart Not Clear
  5 = Error Need Help
  h = Help
  q = Quit
------------------------
"""

async def send_loop(ws: websockets.WebSocketClientProtocol):
    loop = asyncio.get_running_loop()
    print(HELP.format(url=WS_URL))
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.05); continue
        key = line.strip().lower()
        if key == "q":
            print("[CLIENT] Quit requested.")
            await ws.close()
            raise SystemExit(0)
        if key == "h":
            print(HELP.format(url=WS_URL)); continue
        if key in STATE_MAP:
            # payload = json.dumps([STATE_MAP[key]], ensure_ascii=False)
            payload = json.dumps({"type":"cart_state", "value":STATE_MAP[key]}, ensure_ascii=False)
            try:
                await ws.send(payload)
                print(f"[CLIENT] Sent -> {payload}")
            except Exception as e:
                print(f"[CLIENT] Send failed: {e}")
        else:
            print("[CLIENT] Unknown key. Press 'h' for help.")

async def recv_loop(ws: websockets.WebSocketClientProtocol):
    try:
        async for msg in ws:
            print(f"[SERVER] {msg}")
    except websockets.ConnectionClosedOK:
        print("[CLIENT] Connection closed by server.")
    except Exception as e:
        print(f"[CLIENT] Receive error: {e}")

async def connect_and_run():
    backoff = 1.0
    while True:
        try:
            print(f"[CLIENT] Connecting to {WS_URL} ...")
            async with websockets.connect(WS_URL, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                print("[CLIENT] Connected.")
                # start send/recv tasks
                sender = asyncio.create_task(send_loop(ws))
                receiver = asyncio.create_task(recv_loop(ws))
                done, pending = await asyncio.wait(
                    {sender, receiver}, return_when=asyncio.FIRST_EXCEPTION
                )
                for task in pending: task.cancel()
                # if we’re here, one of the loops ended (disconnect or quit)
        except (ConnectionRefusedError, OSError, websockets.InvalidURI) as e:
            print(f"[CLIENT] Connect failed: {e}")
        except websockets.InvalidHandshake as e:
            print(f"[CLIENT] Handshake failed: {e}")
        except SystemExit:
            return
        print(f"[CLIENT] Reconnecting in {backoff:.1f}s ... (Ctrl+C to exit)")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 10.0)  # capped backoff

def main():
    try:
        asyncio.run(connect_and_run())
    except KeyboardInterrupt:
        print("\n[CLIENT] Bye.")

if __name__ == "__main__":
    main()
