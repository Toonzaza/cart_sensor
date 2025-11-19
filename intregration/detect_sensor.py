#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import time, threading
from bus_sensor import MqttBus
import drivers_sensor as drv

# GPIO mapping
GPIO_PHOTO_BARCODE1 = 23
GPIO_PHOTO_BARCODE2 = 24
GPIO_PHOTO_RFID_A   = 25
GPIO_PHOTO_RFID_B   = 16

PHOTO_NAMES = {
    GPIO_PHOTO_BARCODE1: "barcode1",
    GPIO_PHOTO_BARCODE2: "barcode2",
    GPIO_PHOTO_RFID_A:   "rfidA",
    GPIO_PHOTO_RFID_B:   "rfidB",
}

class SensorNode:
    def __init__(self, bus: MqttBus, ser_map: dict, elara, rfid_words: int = 5):
        """
        ser_map: {'1': serial_or_None, '2': serial_or_None}
        elara  : serial_or_None
        """
        self.bus = bus
        self.ser_map = ser_map
        self.elara = elara
        self.rfid_words = rfid_words
        self._install_triggers()

    # ---------- helpers ----------
    def _publish_photo_state(self, pin, state, name):
        """
        state: 1 = beam clear (no object), 0 = blocked (object present)
        """
        payload = {
            "sensor": "photo",
            "gpio": pin,
            "value": {"state": int(bool(state)), "name": name}
        }
        self.bus.publish_sensor(payload)

    def _install_triggers(self):
        # BARCODE triggers
        if self.ser_map.get('1'):
            self._arm_barcode(GPIO_PHOTO_BARCODE1, '1')
        if self.ser_map.get('2'):
            self._arm_barcode(GPIO_PHOTO_BARCODE2, '2')

        # RFID triggers
        if self.elara:
            self._arm_rfid(GPIO_PHOTO_RFID_A)
            self._arm_rfid(GPIO_PHOTO_RFID_B)

    # ---------- BARCODE ----------
    def _arm_barcode(self, pin: int, dev_key: str):
        sensor = drv.make_gpio_input(pin)
        if sensor is None:
            return
        print(f"[GPIO] BARCODE{dev_key} armed on GPIO{pin}")
        lock = drv.BARCODE_LOCKS.get(dev_key)
        name = PHOTO_NAMES.get(pin, f"barcode{dev_key}")

        # ส่งสถานะเริ่มต้นหนึ่งครั้ง (มีประโยชน์กับ FSM)
        try:
            self._publish_photo_state(pin, 1 if sensor.value else 0, name)
        except Exception:
            pass

        def on_falling():
            # โฟโต้ถูกบัง (มีของ) → state=0
            val = 1 if sensor.value else 0
            t = time.monotonic()
            self._publish_photo_state(pin, 0, name)
            print(f"[GPIO] (BARCODE{dev_key}) FALLING @ {t:.3f} GPIO{pin} value={val} → scan (MCR12) until success ...")

            def worker():
                if not lock.acquire(blocking=False):
                    print(f"[BARCODE{dev_key}] busy; skip"); return
                try:
                    code = drv.barcode_scan_until(self.ser_map[dev_key], max_seconds=None)  # wait until success
                    payload = {
                        "sensor": f"barcode{dev_key}",
                        "gpio": pin,
                        "value": {"code": code}
                    }
                    self.bus.publish_sensor(payload)
                finally:
                    lock.release()
            threading.Thread(target=worker, daemon=True).start()

        def on_rising():
            # โฟโต้โล่ง (ยกของออก) → state=1
            self._publish_photo_state(pin, 1, name)

        sensor.when_deactivated = on_falling    # falling edge (active-low)
        sensor.when_activated   = on_rising     # rising edge

    # ---------- RFID ----------
    def _arm_rfid(self, pin: int):
        sensor = drv.make_gpio_input(pin)
        if sensor is None:
            return
        print(f"[GPIO] RFID armed on GPIO{pin}")
        name = PHOTO_NAMES.get(pin, "rfid")

        # ส่งสถานะเริ่มต้นหนึ่งครั้ง
        try:
            self._publish_photo_state(pin, 1 if sensor.value else 0, name)
        except Exception:
            pass

        def on_falling():
            # โฟโต้ถูกบัง → state=0
            val = 1 if sensor.value else 0
            t = time.monotonic()
            self._publish_photo_state(pin, 0, name)
            print(f"[GPIO] (RFID) FALLING @ {t:.3f} GPIO{pin} value={val} → read Elara until tag ...")

            def worker():
                with drv.ELARA_LOCK:
                    epc, rssi, last_words, ascii_txt = drv.elara_read_until(
                        self.elara, max_seconds=None, n_words_to_decode=self.rfid_words
                    )
                payload = {
                    "sensor": "rfid0",
                    "gpio": pin,
                    "value": {"ascii": ascii_txt or ""}  # ส่งเฉพาะ ascii ตามสัญญา
                }
                self.bus.publish_sensor(payload)
            threading.Thread(target=worker, daemon=True).start()

        def on_rising():
            # โฟโต้โล่ง → state=1
            self._publish_photo_state(pin, 1, name)

        sensor.when_deactivated = on_falling
        sensor.when_activated   = on_rising

