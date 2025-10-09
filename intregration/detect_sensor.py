#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_sensor.py (orchestrator)
- เหมือนเดิมทุกอย่าง ยกเว้น 'payload ที่ publish' จะมีแค่ sensor, gpio, value
- mapping:
    GPIO23 → BARCODE1
    GPIO24 → BARCODE2
    GPIO25 → RFID
    GPIO16 → RFID
"""

import time, threading
from bus_sensor import MqttBus
import drivers_sensor as drv

# GPIO mapping
GPIO_PHOTO_BARCODE1 = 23
GPIO_PHOTO_BARCODE2 = 24
GPIO_PHOTO_RFID_A   = 25
GPIO_PHOTO_RFID_B   = 16

class SensorNode:
    def __init__(self, bus: MqttBus, ser_map: dict, elara, rfid_words: int = 5):
        self.bus = bus
        self.ser_map = ser_map   # {'1': serial or None, '2': serial or None}
        self.elara = elara       # serial or None
        self.rfid_words = rfid_words
        self._install_triggers()

    def _install_triggers(self):
        if self.ser_map.get('1'): self._arm_barcode(GPIO_PHOTO_BARCODE1, '1')
        if self.ser_map.get('2'): self._arm_barcode(GPIO_PHOTO_BARCODE2, '2')
        if self.elara:
            self._arm_rfid(GPIO_PHOTO_RFID_A)
            self._arm_rfid(GPIO_PHOTO_RFID_B)

    # ---------- BARCODE ----------
    def _arm_barcode(self, pin: int, dev_key: str):
        sensor = drv.make_gpio_input(pin)
        if sensor is None: return
        print(f"[GPIO] BARCODE{dev_key} armed on GPIO{pin}")
        lock = drv.BARCODE_LOCKS.get(dev_key)

        def on_falling():
            val = 1 if sensor.value else 0
            t = time.monotonic()
            print(f"[GPIO] (BARCODE{dev_key}) FALLING @ {t:.3f} GPIO{pin} value={val} → scan (MCR12) until success ...")

            def worker():
                if not lock.acquire(blocking=False):
                    print(f"[BARCODE{dev_key}] busy; skip"); return
                try:
                    code = drv.barcode_scan_until(self.ser_map[dev_key], max_seconds=None)  # wait until success
                    # --------- ส่งแบบย่อ: sensor, gpio, value เท่านั้น ---------
                    payload = {
                        "sensor": f"barcode{dev_key}",
                        "gpio": pin,
                        "value": {"code": code}
                    }
                    self.bus.publish_sensor(payload)
                finally:
                    lock.release()
            threading.Thread(target=worker, daemon=True).start()

        sensor.when_deactivated = on_falling
        sensor.when_activated   = None

    # ---------- RFID ----------
    def _arm_rfid(self, pin: int):
        sensor = drv.make_gpio_input(pin)
        if sensor is None: return
        print(f"[GPIO] RFID armed on GPIO{pin}")

        def on_falling():
            val = 1 if sensor.value else 0
            t = time.monotonic()
            print(f"[GPIO] (RFID) FALLING @ {t:.3f} GPIO{pin} value={val} → read Elara until tag ...")

            def worker():
                with drv.ELARA_LOCK:
                    epc, rssi, last_words, ascii_txt = drv.elara_read_until(
                        self.elara, max_seconds=None, n_words_to_decode=self.rfid_words
                    )
                # --------- ส่งแบบย่อ: sensor, gpio, value เท่านั้น ---------
                payload = {
                    "sensor": "rfid0",
                    "gpio": pin,
                    "value": {
                        "ascii": ascii_txt or ""   # <<<<<< เหลือเฉพาะ ascii
                    }
                }
                self.bus.publish_sensor(payload)
            threading.Thread(target=worker, daemon=True).start()

        sensor.when_deactivated = on_falling
        sensor.when_activated   = None
