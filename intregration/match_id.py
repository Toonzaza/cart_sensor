#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, time, argparse
import paho.mqtt.client as mqtt

def ts_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")

def parse_args():
    p = argparse.ArgumentParser(description="Detect-mode logger for SmartCart")
    p.add_argument("--host", default=os.getenv("MQTT_HOST", "127.0.0.1"))
    p.add_argument("--port", default=int(os.getenv("MQTT_PORT", "1883")), type=int)
    p.add_argument("--user", default=os.getenv("MQTT_USER"))
    p.add_argument("--password", default=os.getenv("MQTT_PASS"))
    p.add_argument("--base", default=os.getenv("MQTT_BASE", "smartcart"))
    p.add_argument("--station", default=os.getenv("STATION_ID", "slot1"))
    p.add_argument("--client-id", default="detect-mode-logger")
    p.add_argument("--show-payload", action="store_true",
                   help="แสดง payload เต็มทุกครั้ง (default: แสดงเฉพาะ mode)")
    p.add_argument("--ignore-first-retained", action="store_true",
                   help="ละเว้นข้อความ retained ครั้งแรก (กัน log ซ้ำตอนเริ่ม)")
    return p.parse_args()

def main():
    args = parse_args()

    topic_desired = f"{args.base}/detect/{args.station}/desired"
    topic_mode    = f"{args.base}/detect/{args.station}/mode"

    print(f"[{ts_now()}] MQTT connecting to {args.host}:{args.port}")
    print(f"[{ts_now()}] Subscribing: '{topic_desired}' (QoS1), '{topic_mode}' (QoS0)")

    first_retained_seen = {"desired": False, "mode": False}

    def on_connect(cli, userdata, flags, rc, properties=None):
        if rc == 0:
            print(f"[{ts_now()}] MQTT connected OK")
            cli.subscribe([(topic_desired, 1), (topic_mode, 0)])
        else:
            print(f"[{ts_now()}] MQTT connect failed rc={rc}")

    def on_message(cli, userdata, msg):
        nonlocal first_retained_seen

        topic = msg.topic
        payload_raw = msg.payload.decode("utf-8", "ignore")
        is_retained = getattr(msg, "retain", False)

        # เลือกละเว้น retained ข้อความแรกได้ (กัน log ซ้ำตอนเปิดโปรแกรม)
        key = "desired" if topic == topic_desired else "mode"
        if args.ignore_first_retained and is_retained and not first_retained_seen[key]:
            first_retained_seen[key] = True
            print(f"[{ts_now()}] (skip first retained) {topic}")
            return
        first_retained_seen[key] = True

        mode_val = None
        try:
            data = json.loads(payload_raw)
            if isinstance(data, dict) and "mode" in data:
                mode_val = data.get("mode")
        except Exception:
            # ถ้า payload ไม่ใช่ JSON (เช่น ส่งเป็น "start"/"stop") ก็ปล่อยผ่าน
            data = payload_raw

        tag = "RET" if is_retained else "LIVE"
        if mode_val is not None:
            print(f"[{ts_now()}] [{tag}] {topic} -> mode={mode_val}")
        else:
            print(f"[{ts_now()}] [{tag}] {topic} -> (no 'mode' field)")

        if args.show_payload:
            print(f"    payload: {payload_raw}")

    cli = mqtt.Client(client_id=args.client_id, clean_session=True)
    if args.user:
        cli.username_pw_set(args.user, args.password or "")
    cli.on_connect = on_connect
    cli.on_message = on_message

    # ทำ auto-reconnect เป็นขั้นบันได
    cli.reconnect_delay_set(min_delay=1, max_delay=30)

    cli.connect(args.host, args.port, keepalive=60)
    try:
        cli.loop_forever()
    except KeyboardInterrupt:
        print(f"\n[{ts_now()}] Task End.")

if __name__ == "__main__":
    main()
