#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")  # Ubuntu on RPi

import serial, json, time, threading, signal
from gpiozero import DigitalInputDevice

# ================== CONFIG ==================
ELARA_TTY   = '/dev/elara0'
ELARA_BAUD  = 115200

PHOTO_GPIO  = 16          # โฟโต้อิเล็กทริกที่ GPIO16 (active-LOW)
DEBOUNCE_S  = 0.03
COOLDOWN_S  = 0.8
ELARA_WINDOW_S = 3.0      # เวลารอรายงานหลัง Start
VERBOSE_RAW = True

RFID_LAST_WORDS = 5       # จำนวนคำ 16-bit ท้ายที่ถอด ASCII

# ================== LOG ==================
def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)

# ================== ELARA I/O ==================
elara = None
io_lock = threading.Lock()

def elara_open():
    global elara
    if elara and elara.is_open:
        return True
    try:
        elara = serial.Serial(ELARA_TTY, ELARA_BAUD, timeout=0.1, write_timeout=0.5)
        try:
            elara.reset_input_buffer()
            elara.reset_output_buffer()
        except Exception:
            pass
        log(f"[ELARA] open {ELARA_TTY} ok")
        return True
    except Exception as e:
        log(f"[ELARA] open {ELARA_TTY} failed: {e}")
        elara = None
        return False

def _send(obj):
    if not elara: return
    s = json.dumps(obj, separators=(',',':'))  # กำจัดช่องว่างให้เรียบสุด
    elara.write((s + '\r\n').encode('utf-8'))
    if VERBOSE_RAW:
        log(f"[ELARA-TX] {s}")

def _read_lines(duration):
    if not elara: return []
    t0 = time.time()
    out = []
    while time.time() - t0 < duration:
        ln = elara.readline()
        if ln:
            s = ln.decode('utf-8', 'ignore').strip()
            if s:
                if VERBOSE_RAW:
                    log(f"[ELARA-RAW] {s}")
                out.append(s)
        else:
            time.sleep(0.02)
    return out

def _looks_like_error(s):
    try:
        msg = json.loads(s)
    except Exception:
        return False
    return 'ErrID' in msg or (msg.get('Report','').lower().startswith('unknown'))

def _try_cmd(variants, read_for=0.25, stop_on_success=True, label="CMD"):
    """
    ส่งคำสั่งหลายรูปแบบ (fallback) จนกว่าจะเห็นว่ารับได้ (ไม่มี ErrID ใน RAW)
    คืน True/False
    """
    ok = False
    for obj in variants:
        _send(obj)
        lines = _read_lines(read_for)
        if not lines:
            # ไม่มีอะไรตอบกลับ ก็ถือว่า "ไม่นับผิด" ลองตัวถัดไป
            ok = True
            if stop_on_success: break
            continue
        # ถ้าไม่มีบรรทัดไหนเป็น error → success
        if not any(_looks_like_error(s) for s in lines):
            ok = True
            if stop_on_success: break
    if not ok:
        log(f"[ELARA] {label} variants all returned error (continuing anyway)")
    return ok

# ================== Decode helpers (เหมือนโค้ดเดิม) ==================
def _split_words_from_mb(mb_field):
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
    if not isinstance(epc_hex, str):
        return []
    h = ''.join([c for c in epc_hex.strip().lower() if c in '0123456789abcdef'])
    if len(h) % 4 != 0:
        h = h.zfill((len(h) + 3)//4 * 4)
    return [h[i:i+4] for i in range(0, len(h), 4)]

def _words_to_ascii(words, big_endian=True):
    bs = bytearray()
    for w in words:
        val = int(w, 16)
        hi, lo = ((val >> 8) & 0xFF, val & 0xFF)
        bs += bytes([hi, lo]) if big_endian else bytes([lo, hi])
    return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in bs)

def _decode_lastN_ascii_from_msg(msg, n_words):
    words = []
    if 'MB' in msg:
        words = _split_words_from_mb(msg['MB'])
    if not words and msg.get('EPC'):
        words = _split_words_from_epc(msg['EPC'])
    if not words:
        return (None, None)
    while words and words[-1] == '0000':
        words.pop()
    if not words:
        return (None, None)
    lastN = words[-n_words:] if len(words) >= n_words else words
    ascii_text = _words_to_ascii(lastN, big_endian=True)
    return (lastN, ascii_text)

def _find_tag_info(msg):
    """พยายามหยิบ EPC/UII/RSSI จากกุญแจที่เป็นไปได้หลายแบบ"""
    keys = ('EPC','UII','RSSI')
    out = {}
    def walk(x):
        if isinstance(x, dict):
            for k,v in x.items():
                if k in keys and k not in out:
                    out[k] = v
                walk(v)
        elif isinstance(x, list):
            for it in x:
                walk(it)
    walk(msg)
    return out

# ================== High-level read (single shot) ==================
def elara_read_single_ascii(window=ELARA_WINDOW_S, n_words=RFID_LAST_WORDS):
    """
    Start → read window → Stop. ใช้ fallback หลายรูปแบบของคำสั่ง
    """
    if not elara_open():
        log("[ELARA] no port")
        return False

    # ลองตั้งค่า format รายงานให้แน่ใจ (ไม่บังคับว่าต้องสำเร็จ)
    _try_cmd(
        variants=[
            {"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI","MB"]}},
            {"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI"]}},
            {"Cmd":"SetRpt","RptFields":["EPC","RSSI","MB"]},
        ],
        read_for=0.25, stop_on_success=True, label="SetRpt"
    )

    # กันค้าง: Stop ก่อน
    _try_cmd(
        variants=[
            {"Cmd":"StopRZ","RZ":[0]},
            {"Cmd":"StopRZ","RZ":0},
            {"Cmd":"StopRZ"},
            {"Cmd":"StopRead"},
        ],
        read_for=0.2, stop_on_success=True, label="Stop"
    )

    # เริ่มอ่าน
    log(f"[ELARA] Start (window={window:.1f}s)")
    started_ok = _try_cmd(
        variants=[
            {"Cmd":"StartRZ","RZ":[0]},
            {"Cmd":"StartRZ","RZ":0},
            {"Cmd":"StartRZ"},
            {"Cmd":"StartRead"},
        ],
        read_for=0.15, stop_on_success=True, label="Start"
    )

    tag_msg = None
    try:
        for s in _read_lines(window):
            try:
                msg = json.loads(s)
            except Exception:
                continue
            report = str(msg.get('Report','')).lower()
            # รับทั้ง TagEvent/TagReport หรือข้อความที่มี EPC/UII
            if report in ('tagevent','tagreport','tag') or msg.get('EPC') or msg.get('UII'):
                tag_msg = msg
                break
    finally:
        _try_cmd(
            variants=[
                {"Cmd":"StopRZ","RZ":[0]},
                {"Cmd":"StopRZ","RZ":0},
                {"Cmd":"StopRZ"},
                {"Cmd":"StopRead"},
            ],
            read_for=0.2, stop_on_success=True, label="Stop"
        )

    if not tag_msg:
        log("[ELARA] no tag within window")
        return False

    info = _find_tag_info(tag_msg)
    epc  = info.get('EPC') or info.get('UII')
    rssi = info.get('RSSI')
    last_words, ascii_txt = _decode_lastN_ascii_from_msg(tag_msg, n_words)

    log(f"[ELARA] EPC={epc} RSSI={rssi}")
    if last_words:
        log(f"[ELARA] MB/EPC last {len(last_words)} words: {':'.join(last_words)}")
        log(f"[ELARA] ASCII: {ascii_txt}")
    else:
        log("[ELARA] (no MB/EPC words to decode)")
    return True

# ================== GPIO16 trigger ==================
def handle_detection():
    with io_lock:
        log("[SENSOR] DETECT -> read RFID (single)")
        elara_read_single_ascii(window=ELARA_WINDOW_S, n_words=RFID_LAST_WORDS)

def run_sensor_loop():
    sensor = DigitalInputDevice(PHOTO_GPIO, pull_up=False, bounce_time=None)
    prev = sensor.value
    log(f"[GPIO{PHOTO_GPIO}] init: {'HIGH(Idle)' if prev else 'LOW(Detect)'}")
    last_fire = 0.0
    while True:
        v = sensor.value
        if v != prev:
            log(f"[GPIO{PHOTO_GPIO}] state -> {'HIGH' if v else 'LOW'}")
            if v is False:  # FALLING
                time.sleep(DEBOUNCE_S)
                if sensor.value is False:
                    now = time.time()
                    if now - last_fire >= COOLDOWN_S:
                        last_fire = now
                        log(f"[GPIO{PHOTO_GPIO}] DETECT (falling) -> reading RFID")
                        threading.Thread(target=handle_detection, daemon=True).start()
                    else:
                        log(f"[GPIO{PHOTO_GPIO}] detect ignored (cooldown)")
            prev = v
        time.sleep(0.002)

# ================== main ==================
def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    elara_open()  # เปิดพอร์ตรอไว้ก่อน

    # เฝ้า GPIO16 ในเธรด
    threading.Thread(target=run_sensor_loop, daemon=True).start()

    log("===== READY =====")
    print("GPIO16 ต่ำ (photo detect) ⇒ Start read RFID 1 ครั้ง (ถอด ASCII ท้าย)")
    print("กด Ctrl+C เพื่อออก")
    print("-----------------")

    # main thread ว่างเฉย ๆ ให้ Ctrl+C ได้
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if elara:
            try: elara.close()
            except: pass

if __name__ == "__main__":
    main()