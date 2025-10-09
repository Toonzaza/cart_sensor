#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, argparse
from bus_sensor import MqttBus
import drivers_sensor as drv
from detect_sensor import SensorNode

# ===== ปรับได้ตามฮาร์ดแวร์ =====
BARCODE_PORTS = {'1': '/dev/barcode0', '2': '/dev/barcode1'}
ELARA_TTY     = '/dev/elara0'

def main():
    ap = argparse.ArgumentParser(description="SmartCart detect_sensor → match_id (MCR12-only)")
    # MQTT
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-base", default="smartcart")
    ap.add_argument("--mqtt-user", default=None)
    ap.add_argument("--mqtt-pass", default=None)
    ap.add_argument("--device-id", default="pi5-01")
    # RFID decode words
    ap.add_argument("--rfid-words", type=int, default=5)
    args = ap.parse_args()

    # MQTT
    bus = MqttBus(args.mqtt_host, args.mqtt_port, args.mqtt_base,
                  user=args.mqtt_user, password=args.mqtt_pass,
                  client_id=f"{args.device_id}-sensor")

    # เปิดพอร์ต Barcode (fixed path เท่านั้น)
    ser1 = drv.barcode_open(BARCODE_PORTS.get('1'))
    ser2 = drv.barcode_open(BARCODE_PORTS.get('2'))

    # เปิด RFID (fixed path เท่านั้น)
    elara = drv.elara_open(ELARA_TTY)
    if elara:
        drv.elara_set_manual_mode(elara)

    # ติดตั้ง trigger + run
    _ = SensorNode(
        bus=bus,
        ser_map={'1': ser1, '2': ser2},
        elara=elara,
        # device_id=args.rfid_words,
        rfid_words=args.rfid_words
    )

    print("===== RUNNING (Ctrl+C to quit) =====")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
        for s in (ser1, ser2):
            try: s.close()
            except: pass
        try: elara.close()
        except: pass
        print("Stopped.")

if __name__ == "__main__":
    main()
