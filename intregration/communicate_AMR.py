#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, telnetlib, signal
import paho.mqtt.client as mqtt

# ---- CONFIG ----
STATE_PATH = "/home/fibo/cart_ws/intregration/data/state.json"
GOALS_MAP_PATH = "/home/fibo/cart_ws/intregration/data/goals_map.json"

MQTT_HOST  = "127.0.0.1"
MQTT_PORT  = 1883
BASE       = "smartcart"
SUB_TOPIC  = f"{BASE}/toggle_omron"   # << subscribe trigger จาก match_id

AMR_HOST   = "192.168.0.3"
AMR_PORT   = 7171
AMR_PASS   = "adept"
TELNET_TIMEOUT = 5.0

# ---- helpers ----
def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[LOAD] {path} failed: {e}")
        return {}

def _resolve_goal(goal_id: str, goals_map: dict):
    """
    คืน (cmd, goal_name)
    รองรับ goals_map:
      - {"DOT400002": "Goal2"}
      - {"DOT400002": {"goal":"Goal2", "cmd":"goto"}}
    ถ้าไม่พบ คืน (None, None)
    """
    if not goal_id or not isinstance(goals_map, dict):
        return (None, None)
    entry = goals_map.get(goal_id)
    if entry is None:
        return (None, None)
    if isinstance(entry, str):
        return ("goto", entry)  # default cmd
    if isinstance(entry, dict):
        goal_name = entry.get("goal") or entry.get("arcl_goal") or entry.get("name")
        cmd = entry.get("cmd") or entry.get("command") or "goto"
        if goal_name:
            return (cmd, goal_name)
    return (None, None)

def _telnet_send_amr(cmd_lines):
    """
    เปิด telnet → ส่ง password → ส่งชุดคำสั่ง → ปิด
    cmd_lines: list[str] เช่น ["goto Goal2", "waitTaskFinish"]
    """
    try:
        print(f"[TELNET] connect {AMR_HOST}:{AMR_PORT}")
        tn = telnetlib.Telnet(AMR_HOST, AMR_PORT, TELNET_TIMEOUT)

        # ส่งรหัสผ่านทันที (ส่วนใหญ่ ARCL จะอ่านเป็นบรรทัดแรก)
        tn.write((AMR_PASS + "\n").encode("ascii"))
        time.sleep(0.2)  # รอ login

        for line in cmd_lines:
            out = (line + "\n").encode("ascii")
            print(f"[TELNET] >> {line}")
            tn.write(out)
            time.sleep(0.1)

        # อ่านทิ้งเล็กน้อย (optional)
        try:
            resp = tn.read_very_eager().decode("utf-8", "ignore")
            if resp:
                print("[TELNET] <<", resp.strip()[:500])
        except Exception:
            pass

        tn.close()
        print("[TELNET] closed.")
        return True
    except Exception as e:
        print(f"[TELNET] error: {e}")
        return False

# ---- MQTT handlers ----
def on_connect(client, userdata, flags, rc):
    print("communicate_AMR running. Ctrl+C to quit.")
    print(f"[MQTT] sub {SUB_TOPIC}")
    client.subscribe(SUB_TOPIC)

def on_message(client, userdata, msg):
    # รับ trigger จาก match_id
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[MQTT] bad payload: {e}")
        return

    # goal_id อาจอยู่ที่ payload["goal_id"] หรือ payload["latest_job_ids"]["goal_id"]
    goal_id = payload.get("goal_id")
    if goal_id is None:
        lj = payload.get("latest_job_ids") or {}
        goal_id = lj.get("goal_id")

    print(f"[TOGGLE] received. goal_id={goal_id}")

    # โหลด goals_map + state ปัจจุบัน (เผื่ออยาก log/ตรวจ)
    goals_map = _load_json(GOALS_MAP_PATH)
    state = _load_json(STATE_PATH)

    cmd, goal_name = _resolve_goal(goal_id, goals_map)
    if not cmd or not goal_name:
        print(f"[MAP] goal_id '{goal_id}' not found in {GOALS_MAP_PATH}")
        return

    # เตรียมคำสั่ง ARCL
    # ค่าเริ่มต้น: "goto <goal_name>"
    # ถ้าคุณต้องการเพิ่มลำดับ เช่น waitTaskFinish; map ใน goals_map อาจระบุรายการคำสั่งเองได้
    cmd_lines = [f"{cmd} {goal_name}"]

    # ส่งไปที่ AMR
    ok = _telnet_send_amr(cmd_lines)
    if ok:
        print(f"[AMR] command sent: {cmd_lines}")
    else:
        print("[AMR] failed to send command.")

def main():
    cli = mqtt.Client(client_id="communicate_AMR")
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, 30)

    def _exit(*_):
        try: cli.loop_stop(); cli.disconnect()
        finally: os._exit(0)

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)
    cli.loop_forever()

if __name__ == "__main__":
    main()
