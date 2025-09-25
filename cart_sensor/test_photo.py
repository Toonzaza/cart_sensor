#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, json, argparse, threading
import serial

# ================== GPIO (Trigger from GPIO16 & GPIO20) ==================
# ใช้ gpiozero + lgpio ตามตัวอย่างของคุณ
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
try:
    from gpiozero import DigitalInputDevice
except Exception as e:
    DigitalInputDevice = None
    print(f"[GPIO] gpiozero not available: {e}", file=sys.stderr)

GPIO_PIN_RFID     = 16       # ใช้ขา GPIO16 เป็น trigger RFID
GPIO_PULL_UP      = False    # เหมือนตัวอย่างของคุณ
DEBOUNCE_MS       = 200      # กันเด้งขอบสั้นๆ

# ================== ค่าพื้นฐาน/พอร์ต ==================
BARCODE_PORTS = {
    '1': '/dev/barcode0',  # -> /dev/ttyACM1 ไม่มี plate ช่องบน
    '2': '/dev/barcode1',  # -> /dev/ttyACM2 มี plate ช่องล่าง
}
BARCODE_BAUD = 9600

ELARA_TTY   = '/dev/elara0'   # -> /dev/ttyACM3 ช่องบนขวา
ELARA_BAUD  = 115200
ELARA_SAVE  = False

# ถ้าอยากกันรอนานเกินไป ให้กำหนดวินาที; None = รอไม่จำกัด
MAX_WAIT_UNTIL_READ = None

# จำนวน "คำ" ท้าย (16-bit words) ที่ต้องการถอดเป็น ASCII จาก RFID (ปรับได้ตอนรันด้วย --rfid-words)
DEFAULT_RFID_WORDS = 5

# ================== MCR12: serial command helpers ==================
def _mcr12_frame(cmd, da_bytes_12):
    """ประกอบเฟรมตามสเปค MCR12: STX(0x02), CMD, DA0..DA11(12B), ETX(0x03), SUM"""
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    checksum = (256 - (sum(data) & 0xFF)) & 0xFF
    data.append(checksum)
    return data

def mcr12_enable(ser, delay_ms=0):
    """
    เริ่มสแกน: delay_ms=0 = ยิงต่อเนื่องจนหยุดด้วย disable,
    delay_ms>0 = ยิงแบบกำหนดเวลา (ms)
    """
    DA0 = 0x01
    if delay_ms and delay_ms > 0:
        DA1 = 0x02
        DA2 =  delay_ms        & 0xFF
        DA3 = (delay_ms >> 8)  & 0xFF
    else:
        DA1, DA2, DA3 = 0x01, 0x00, 0x00
    da = [DA0, DA1, DA2, DA3] + [0x00]*8
    ser.write(_mcr12_frame(0x01, da))

def mcr12_disable(ser):
    """หยุดสแกน"""
    da = [0x01, 0x00] + [0x00]*10
    ser.write(_mcr12_frame(0x01, da))

def mcr12_scan_until(ser, max_seconds=MAX_WAIT_UNTIL_READ):
    """สแกนต่อเนื่องจนกว่าจะอ่านได้ 1 บรรทัด แล้วหยุด/คืนข้อความบาร์โค้ด (ยกเลิกด้วย Ctrl+C)"""
    try: ser.reset_input_buffer()
    except Exception: pass
    mcr12_enable(ser, delay_ms=0)
    t0 = time.time()
    buf = bytearray()
    line = None
    try:
        while True:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                if b'\r' in buf or b'\n' in buf:
                    line = buf.replace(b'\r', b'\n').split(b'\n')[0].decode('utf-8', 'ignore').strip()
                    if line:
                        break
            else:
                time.sleep(0.01)
            if max_seconds is not None and (time.time() - t0) > max_seconds:
                break
    finally:
        mcr12_disable(ser)
    return line

# ================== ELARA: JSON/RCI helpers ==================
elara = None
def elara_open():
    global elara
    try:
        elara = serial.Serial(ELARA_TTY, ELARA_BAUD, timeout=0.2)
        print(f"[ELARA] open {ELARA_TTY} ok")
    except Exception as e:
        print(f"[ELARA] open {ELARA_TTY} failed: {e}")
        elara = None

def jsend(obj):
    if not elara: return
    elara.write((json.dumps(obj) + '\r\n').encode('utf-8'))

def jread(timeout=0.3):
    """อ่านสั้น ๆ (สำหรับวนซ้ำเอง)"""
    if not elara: return []
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        ln = elara.readline()
        if ln:
            s = ln.decode('utf-8', 'ignore').strip()
            if s: lines.append(s)
        else:
            time.sleep(0.02)
    return lines

def elara_set_manual_mode():
    """ตั้งค่าให้ Elara เงียบจนกว่าจะสั่ง StartRZ และรายงาน EPC,RSSI,MB"""
    if not elara: return
    jsend({"Cmd":"StopRZ","RZ":["ALL"]}); _ = jread(0.2)
    jsend({"Cmd":"SetCfg","Cfg":{"RdrStart":"NOTACTIVE"}}); _ = jread(0.2)
    jsend({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]});   _ = jread(0.2)
    jsend({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]});    _ = jread(0.2)
    jsend({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI","MB"]}}); _ = jread(0.2)
    if ELARA_SAVE:
        jsend({"Cmd":"Save"}); _ = jread(0.5)

# --------- ตัวช่วยแตก "คำ" และถอด ASCII จาก MB/EPC ---------
def _split_words_from_mb(mb_field):
    """
    รับ MB รูป [[bank, offset, ":hhhh:hhhh:..."], ...]
    คืน list คำ 4-hex (เช่น ["4d58","4b32",...]) ตามลำดับซ้าย->ขวา
    """
    words = []
    if isinstance(mb_field, list):
        for entry in mb_field:
            if isinstance(entry, list) and len(entry) >= 3 and isinstance(entry[2], str):
                parts = [p.strip().lower() for p in entry[2].split(':') if p.strip()]
                for p in parts:
                    if len(p) == 4 and all(c in '0123456789abcdef' for c in p):
                        words.append(p)
    return words

def _split_words_from_epc(epc_hex):
    """
    รับ EPC เป็น hex string เช่น '4d584b32322d313034390000'
    คืน list คำ 4-hex ต่อเนื่องซ้าย->ขวา
    """
    if not isinstance(epc_hex, str):
        return []
    h = ''.join([c for c in epc_hex.strip().lower() if c in '0123456789abcdef'])
    if len(h) % 4 != 0:
        h = h.zfill((len(h) + 3)//4 * 4)
    return [h[i:i+4] for i in range(0, len(h), 4)]

def _words_to_ascii(words, big_endian=True):
    """รวมคำ 16 บิตเป็นไบต์แล้วถอดเป็น ASCII (นอกช่วง 32..126 แทนด้วย '.')"""
    bs = bytearray()
    for w in words:
        val = int(w, 16)
        hi, lo = ((val >> 8) & 0xFF, val & 0xFF)
        bs += bytes([hi, lo]) if big_endian else bytes([lo, hi])
    return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in bs)

def _decode_lastN_ascii_from_msg(msg, n_words):
    """
    ดึงคำจาก MB ถ้ามี; ถ้าไม่มีใช้ EPC
    - ตัด padding '0000' ท้ายออกก่อน
    - เลือก N คำสุดท้าย (ถ้ามีน้อยกว่า ใช้เท่าที่มี)
    คืนค่า: (last_words_list, ascii_text) หรือ (None, None)
    """
    words = []
    if 'MB' in msg:
        words = _split_words_from_mb(msg['MB'])
    if not words and msg.get('EPC'):
        words = _split_words_from_epc(msg['EPC'])

    if not words:
        return (None, None)

    # ตัด padding 0000 ด้านท้าย
    while words and words[-1] == '0000':
        words.pop()
    if not words:
        return (None, None)

    lastN = words[-n_words:] if len(words) >= n_words else words
    ascii_text = _words_to_ascii(lastN, big_endian=True)
    return (lastN, ascii_text)

def elara_read_until(max_seconds, n_words_to_decode):
    """
    เริ่มอ่าน RZ0 แล้ววนจนพบ TagEvent จากนั้นหยุด
    คืนค่า: (epc, rssi, last_words, ascii_text) หรือ (None, None, None, None)
    """
    if not elara:
        print("[ELARA] no port")
        return (None, None, None, None)

    jsend({"Cmd":"StopRZ","RZ":[0]}); _ = jread(0.1)
    jsend({"Cmd":"StartRZ","RZ":[0]})

    t0 = time.time()
    epc, rssi = None, None
    last_words, ascii_txt = None, None

    try:
        while True:
            for s in jread(0.3):
                try:
                    msg = json.loads(s)
                except Exception:
                    continue
                if msg.get("Report") == "TagEvent":
                    epc  = msg.get('EPC') or msg.get('UII')
                    rssi = msg.get('RSSI')
                    last_words, ascii_txt = _decode_lastN_ascii_from_msg(msg, n_words_to_decode)
                    raise StopIteration
            if max_seconds is not None and (time.time() - t0) > max_seconds:
                break
    except StopIteration:
        pass
    finally:
        jsend({"Cmd":"StopRZ","RZ":[0]})

    return (epc, rssi, last_words, ascii_txt)

# ================== GPIO Trigger Threads ==================
_elara_lock   = threading.Lock()
_barcode_lock = threading.Lock()
_stop_event   = threading.Event()

def _print_elara_result(epc, rssi, last_words, ascii_txt):
    if epc:
        print(f"[ELARA] EPC={epc} RSSI={rssi}")
        if last_words:
            print(f"[ELARA] MB/EPC last {len(last_words)} words: {':'.join(last_words)}")
            print(f"[ELARA] ASCII: {ascii_txt}")
        else:
            print("[ELARA] (no MB/EPC words to decode)")
    else:
        print("[ELARA] no tag (timeout)")

def start_gpio_trigger_rfid(n_words, max_wait, gpio_pin=GPIO_PIN_RFID):
    if DigitalInputDevice is None:
        print("[GPIO] Disabled (gpiozero not available)")
        return None

    sensor = DigitalInputDevice(gpio_pin, pull_up=GPIO_PULL_UP,
                                bounce_time=DEBOUNCE_MS/1000.0)

    print(f"[GPIO] RFID trigger armed on GPIO{gpio_pin} (falling edge).")

    def _on_falling():
        now = time.monotonic()
        print(f"[GPIO] (RFID) Falling detected @ {now:.3f}, reading RFID ...")
        def _worker():
            with _elara_lock:
                epc, rssi, last_words, ascii_txt = elara_read_until(max_wait, n_words)
            _print_elara_result(epc, rssi, last_words, ascii_txt)
        threading.Thread(target=_worker, daemon=True).start()

    # ใช้ขาลงแทน
    sensor.when_deactivated = _on_falling
    sensor.when_activated = None

    def _loop():
        while not _stop_event.is_set():
            time.sleep(0.1)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return sensor

def start_gpio_barcode_trigger(gpio_pin, ser, timeout=5.0, name="BARCODE"):
    """
    สายโฟโตต่อ GPIO gpio_pin -> ตกขอบ (falling) จะสั่งให้สแกนบาร์โค้ด 1 ครั้ง
    - ใช้ thread แยก + _barcode_lock กันยิงซ้ำ
    """
    if DigitalInputDevice is None:
        print("[GPIO] Disabled (gpiozero not available)")
        return None
    if ser is None:
        print(f"[{name}] Serial not open; barcode GPIO trigger disabled.")
        return None

    sensor = DigitalInputDevice(gpio_pin, pull_up=GPIO_PULL_UP,
                                bounce_time=DEBOUNCE_MS/1000.0)

    print(f"[GPIO] {name} trigger armed on GPIO{gpio_pin} (falling edge).")

    def _on_falling():
        now = time.monotonic()
        print(f"[GPIO] ({name}) Falling detected @ {now:.3f}, scanning barcode ...")
        def _worker():
            if not _barcode_lock.acquire(blocking=False):
                print(f"[{name}] scan skipped (busy).")
                return
            try:
                code = mcr12_scan_until(ser, max_seconds=timeout)
                if code:
                    print(f"[{name}] READ: {code}")
                else:
                    print(f"[{name}] no read (timeout={timeout}s)")
            finally:
                _barcode_lock.release()
        threading.Thread(target=_worker, daemon=True).start()

    sensor.when_deactivated = _on_falling
    sensor.when_activated = None
    return sensor

# ================== main ==================
def main():
    global GPIO_PIN_RFID

    parser = argparse.ArgumentParser(description="Smart cart barcode/RFID utility (GPIO16 triggers RFID, GPIO20 triggers BARCODE)")
    # RFID
    parser.add_argument("--rfid-words", type=int, default=DEFAULT_RFID_WORDS,
                        help="จำนวนคำ (16-bit words) ท้ายที่ต้องการถอด ASCII จาก RFID (ค่าเริ่มต้น 5)")
    parser.add_argument("--rfid-timeout", type=float, default=MAX_WAIT_UNTIL_READ,
                        help="กำหนดเวลารออ่าน RFID เป็นวินาที; ไม่กำหนด = รอไม่จำกัด")
    parser.add_argument("--gpio-pin", type=int, default=GPIO_PIN_RFID,
                        help="กำหนดขา GPIO ที่ใช้เป็น trigger RFID (ค่าเริ่มต้น 16)")
    # BARCODE via GPIO
    parser.add_argument("--barcode-gpio-pin", type=int, default=20,
                        help="กำหนดขา GPIO ที่ใช้เป็น trigger BARCODE (ค่าเริ่มต้น 20)")
    parser.add_argument("--barcode-dev", choices=['1','2'], default='1',
                        help='เลือกอุปกรณ์บาร์โค้ดที่จะยิงเมื่อ GPIO ทริกเกอร์ ("1"=/dev/barcode0, "2"=/dev/barcode1)')
    parser.add_argument("--barcode-timeout", type=float, default=5.0,
                        help="กำหนดเวลารออ่าน BARCODE (วินาที) เมื่อถูกทริกเกอร์จาก GPIO (ค่าเริ่มต้น 5)")

    args = parser.parse_args()

    # sync ค่า GPIO_PIN_RFID ตาม argument
    GPIO_PIN_RFID = args.gpio_pin

    # เปิด Elara และตั้งค่าโหมด manual
    elara_open()
    elara_set_manual_mode()

    # เปิดพอร์ตบาร์โค้ดไว้ล่วงหน้า
    sers = {}
    for key, port in BARCODE_PORTS.items():
        try:
            sers[key] = serial.Serial(port, BARCODE_BAUD, timeout=0.1)
            print(f"[BARCODE{key}] open {port} ok")
        except Exception as e:
            print(f"[BARCODE{key}] open {port} failed: {e}")
            sers[key] = None

    # เริ่ม GPIO trigger สำหรับ RFID (แทนโหมด 3)
    gpio_rfid = start_gpio_trigger_rfid(args.rfid_words, args.rfid_timeout, gpio_pin=GPIO_PIN_RFID)

    # เริ่ม GPIO trigger สำหรับ BARCODE จากโฟโตที่ GPIO20 (หรือที่กำหนด)
    barcode_ser = sers.get(args.barcode_dev)
    gpio_barcode = start_gpio_barcode_trigger(
        gpio_pin=args.barcode_gpio_pin,
        ser=barcode_ser,
        timeout=args.barcode_timeout,
        name=f"BARCODE{args.barcode_dev}"
    )

    print("===== WAIT MODE =====")
    # print("1 = scan /dev/barcode0 (สแกนต่อเนื่องจนได้ค่า)")
    # print("2 = scan /dev/barcode1 (สแกนต่อเนื่องจนได้ค่า)")
    # print(f"   * RFID: ใช้สัญญาณจาก GPIO{GPIO_PIN_RFID} (falling) เพื่ออ่านแท็กอัตโนมัติ")
    # print(f"   * BARCODE: ใช้สัญญาณจาก GPIO{args.barcode_gpio_pin} (falling) เพื่อยิงอ่าน BARCODE{args.barcode_dev}")
    # print("q = quit")R
    print("----------------------")

    try:
        while True:
            sel = input("> ").strip().lower()
            if sel in ('q', 'quit', 'exit'):
                break
            elif sel in ('1', '2'):
                if sers.get(sel) is None:
                    print(f"[BARCODE{sel}] port not open"); continue
                print(f"[BARCODE{sel}] scanning... (Ctrl+C to cancel)")
                try:
                    code = mcr12_scan_until(sers[sel], max_seconds=None)
                    if code: print(f"[BARCODE{sel}] {code}")
                    else:    print(f"[BARCODE{sel}] no read (timeout)")
                except KeyboardInterrupt:
                    print(f"[BARCODE{sel}] canceled")
            elif sel == '':
                continue
            else:
                print("choose 1/2 or q")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        _stop_event.set()
        for s in sers.values():
            try: s.close()
            except: pass
        if elara:
            try: elara.close()
            except: pass

if __name__ == "__main__":
    main()
