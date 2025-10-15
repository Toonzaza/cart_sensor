#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, signal
import paho.mqtt.client as mqtt

# ใช้ gpiozero กับ lgpio backend
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
from gpiozero import OutputDevice

BASE       = "smartcart"
MQTT_HOST  = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT  = int(os.getenv("MQTT_PORT", "1883"))
LED_CMD_TOPIC = f"{BASE}/led/cmd"

PIN_MAP = {
    "cuh1": (20, 21),
    "cuh2": (17, 27),
    "kit1": (5,  6),
    "kit2": (13, 19),
}

_outputs: dict[int, OutputDevice] = {}

def gpio_setup():
    pins = {p for pair in PIN_MAP.values() for p in pair}
    for p in pins:
        _outputs[p] = OutputDevice(p, active_high=True, initial_value=False)

def set_pair(green_pin: int, red_pin: int, result: str):
    gp = _outputs[int(green_pin)]
    rp = _outputs[int(red_pin)]
    if result == "ok":
        gp.on();  rp.off()
    elif result == "nok":
        gp.off(); rp.on()
    else:
        gp.off(); rp.off()

def cleanup():
    for dev in _outputs.values():
        try: dev.off(); dev.close()
        except: pass

def on_connect(client, userdata, flags, rc):
    print(f"[LED] connected rc={rc}; sub {LED_CMD_TOPIC}")
    client.subscribe(LED_CMD_TOPIC, qos=1)

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        print(f"[LED] bad payload: {e}")
        return

    target = data.get("target")
    result = data.get("result")   # "ok" | "nok" | "skip"
    gpin   = data.get("green_gpio")
    rpin   = data.get("red_gpio")

    if target not in PIN_MAP:
        print(f"[LED] unknown target: {target} payload={data}")
        return
    if result not in ("ok","nok","skip"):
        print(f"[LED] invalid result: {result}")
        return
    if gpin is None or rpin is None:
        gpin, rpin = PIN_MAP[target]

    print(f"[LED] target={target} -> {result} (G={gpin}, R={rpin})")
    try:
        set_pair(int(gpin), int(rpin), result)
    except Exception as e:
        print(f"[LED] set_pair error: {e}")

def main():
    gpio_setup()
    cli = mqtt.Client(client_id="led_actuator_gpiozero")
    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

    def _exit(*_):
        try:
            cli.loop_stop(); cli.disconnect()
        finally:
            cleanup()
            os._exit(0)

    signal.signal(signal.SIGINT, _exit)
    signal.signal(signal.SIGTERM, _exit)
    cli.loop_forever()

if __name__ == "__main__":
    main()
