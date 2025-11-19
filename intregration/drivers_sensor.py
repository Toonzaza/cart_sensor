#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, json, threading
from typing import Optional, Tuple

# ===== GPIO =====
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
try:
    from gpiozero import DigitalInputDevice
except Exception as e:
    DigitalInputDevice = None
    print(f"[GPIO] gpiozero not available: {e}", file=sys.stderr)

DEBOUNCE_MS   = 200
GPIO_PULL_UP  = False

def make_gpio_input(pin: int) -> Optional[DigitalInputDevice]:
    if DigitalInputDevice is None:
        print("[GPIO] gpiozero not available -> skip GPIO"); return None
    try:
        return DigitalInputDevice(pin, pull_up=GPIO_PULL_UP, bounce_time=DEBOUNCE_MS/1000.0)
    except Exception as e:
        print(f"[GPIO] cannot claim GPIO{pin}: {e}"); return None

# ===== Serial base =====
import serial

# ===== BARCODE (MCR12 only) =====
BARCODE_BAUD = 9600

def _mcr12_frame(cmd, da_bytes_12):
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    checksum = (256 - (sum(data) & 0xFF)) & 0xFF
    data.append(checksum)
    return data

def mcr12_enable(ser, delay_ms=0):
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
    da = [0x01, 0x00] + [0x00]*10
    ser.write(_mcr12_frame(0x01, da))

def barcode_open(port: Optional[str], baud: int = BARCODE_BAUD, timeout: float = 0.1) -> Optional[serial.Serial]:
    if not port: return None
    try:
        s = serial.Serial(port, baud, timeout=timeout)
        print(f"[BARCODE] open {port} ok"); return s
    except Exception as e:
        print(f"[BARCODE] open {port} failed: {e}"); return None

def barcode_scan_until(ser: serial.Serial, max_seconds: Optional[float]=None) -> Optional[str]:
    """สแกนต่อเนื่องจนได้ 1 บรรทัด แล้วหยุด (ตาม logic โค้ดของคุณ)"""
    if ser is None: return None
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
            if (max_seconds is not None) and ((time.time() - t0) > max_seconds):
                break
    finally:
        mcr12_disable(ser)
    return line

# ===== RFID (Elara JSON/RCI) =====
ELARA_BAUD = 115200

def elara_open(port: str) -> Optional[serial.Serial]:
    try:
        s = serial.Serial(port, ELARA_BAUD, timeout=0.2)
        print(f"[ELARA] open {port} ok"); return s
    except Exception as e:
        print(f"[ELARA] open {port} failed: {e}"); return None

def jsend(elara, obj):
    if not elara: return
    elara.write((json.dumps(obj) + '\r\n').encode('utf-8'))

def jread(elara, timeout=0.3):
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

def elara_set_manual_mode(elara, save=False):
    if not elara: return
    jsend(elara, {"Cmd":"StopRZ","RZ":["ALL"]}); _ = jread(elara, 0.2)
    jsend(elara, {"Cmd":"SetCfg","Cfg":{"RdrStart":"NOTACTIVE"}}); _ = jread(elara, 0.2)
    jsend(elara, {"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]});   _ = jread(elara, 0.2)
    jsend(elara, {"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]});    _ = jread(elara, 0.2)
    jsend(elara, {"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI","MB"]}}); _ = jread(elara, 0.2)
    if save:
        jsend(elara, {"Cmd":"Save"}); _ = jread(elara, 0.5)

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

def elara_read_until(elara, max_seconds: Optional[float], n_words_to_decode: int) -> Tuple[Optional[str], Optional[int], Optional[list], Optional[str]]:
    if not elara:
        print("[ELARA] no port")
        return (None, None, None, None)
    jsend(elara, {"Cmd":"StopRZ","RZ":[0]}); _ = jread(elara, 0.1)
    jsend(elara, {"Cmd":"StartRZ","RZ":[0]})
    t0 = time.time()
    epc, rssi = None, None
    last_words, ascii_txt = None, None
    try:
        while True:
            for s in jread(elara, 0.3):
                try:
                    msg = json.loads(s)
                except Exception:
                    continue
                if msg.get("Report") == "TagEvent":
                    epc  = msg.get('EPC') or msg.get('UII')
                    rssi = msg.get('RSSI')
                    last_words, ascii_txt = _decode_lastN_ascii_from_msg(msg, n_words_to_decode)
                    raise StopIteration
            if (max_seconds is not None) and ((time.time() - t0) > max_seconds):
                break
    except StopIteration:
        pass
    finally:
        jsend(elara, {"Cmd":"StopRZ","RZ":[0]})
    return (epc, rssi, last_words, ascii_txt)

# ===== Locks =====
ELARA_LOCK = threading.Lock()
BARCODE_LOCKS = {'1': threading.Lock(), '2': threading.Lock()}
