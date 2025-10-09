#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import paho.mqtt.client as mqtt

class MqttBus:
    def __init__(self, host="127.0.0.1", port=1883, base="smartcart",
                 user=None, password=None, client_id="sensor-node", keepalive=30):
        self.base = base.rstrip("/")
        # ใช้ API v1 เพื่อให้เข้ากับโค้ดเดิมของคุณ
        self.cli = mqtt.Client(client_id=client_id, clean_session=True)
        if user and password:
            self.cli.username_pw_set(user, password)
        self.cli.connect(host, port, keepalive)
        self.cli.loop_start()
        print(f"[MQTT] connected to {host}:{port}, base='{self.base}'")

    def publish_sensor(self, payload: dict, qos=0, retain=False):
        topic = f"{self.base}/sensor"  # ให้ match_id รับจากที่นี่
        self.cli.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=retain)
        print(f"[PUB] {topic}: {payload}")

    def close(self):
        try:
            self.cli.loop_stop()
            self.cli.disconnect()
        except Exception:
            pass
