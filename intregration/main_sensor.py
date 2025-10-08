#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import time, argparse, threading
from bus_sensor import MqttBus
import drivers_sensor as drv

GPIO_PHOTO_BARCODE1 = 23
GPIO_PHOTO_BARCODE2 = 24
GPIO_PHOTO_RFID_A   = 25
GPIO_PHOTO_RFID_B   = 16

BARCODE_PORTS = {'1': '/dev/barcode0', '2': '/dev/barcode1'}

def start_barcode_trigger(pin, ser, timeout, dev_key, bus: MqttBus):
    sensor = drv.make_gpio_input(pin)
    if sensor is None or ser is None: return None
    print(f"[GPIO] BARCODE{dev_key} armed on GPIO{pin}")

    lock = drv.BARCODE_LOCKS.get(dev_key)

    def on_falling():
        def worker():
            if not lock.acquire(blocking=False):
                print(f"[BARCODE{dev_key}] busy"); return
            try:
                code = drv.barcode_read_once(ser, timeout)
                payload = {
                    "source":"detect_sensor",
                    "pos_gpio":pin,
                    "type":"barcode",
                    "device":f"barcode{dev_key}",
                    "value":code, "text":code,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                bus.publish_sensor(payload)
            finally:
                lock.release()
        threading.Thread(target=worker, daemon=True).start()

    sensor.when_deactivated = on_falling
    sensor.when_activated = None
    return sensor

def start_rfid_trigger(pin, elara, timeout, last_words, bus: MqttBus):
    sensor = drv.make_gpio_input(pin)
    if sensor is None or elara is None: return None
    print(f"[GPIO] RFID armed on GPIO{pin}")

    def on_falling():
        def worker():
            with drv.ELARA_LOCK:
                epc, rssi, ascii_txt = drv.elara_read_once(elara, max_seconds=timeout, last_words=last_words)
            bus.publish_sensor({
                "source":"detect_sensor",
                "pos_gpio":pin,
                "type":"rfid",
                "value":{"epc":epc,"rssi":rssi,"ascii":ascii_txt},
                "text":ascii_txt,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
        threading.Thread(target=worker, daemon=True).start()

    sensor.when_deactivated = on_falling
    sensor.when_activated = None
    return sensor

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-base", default="smartcart")
    ap.add_argument("--barcode1-timeout", type=float, default=5.0)
    ap.add_argument("--barcode2-timeout", type=float, default=5.0)
    ap.add_argument("--rfid-timeout", type=float, default=None)
    ap.add_argument("--rfid-words", type=int, default=5)
    args = ap.parse_args()

    bus = MqttBus(args.mqtt_host, args.mqtt_port, args.mqtt_base)

    # เปิดอุปกรณ์
    ser1 = drv.barcode_open(BARCODE_PORTS['1'])
    ser2 = drv.barcode_open(BARCODE_PORTS['2'])
    elara = drv.elara_open(); drv.elara_set_manual_mode(elara)

    # ติดตั้ง trigger 4 จุด
    _ = start_barcode_trigger(GPIO_PHOTO_BARCODE1, ser1, args.barcode1_timeout, '1', bus)
    _ = start_barcode_trigger(GPIO_PHOTO_BARCODE2, ser2, args.barcode2_timeout, '2', bus)
    _ = start_rfid_trigger(GPIO_PHOTO_RFID_A, elara, args.rfid_timeout, args.rfid_words, bus)
    _ = start_rfid_trigger(GPIO_PHOTO_RFID_B, elara, args.rfid_timeout, args.rfid_words, bus)

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
