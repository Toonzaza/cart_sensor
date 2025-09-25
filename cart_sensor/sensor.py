#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Cart IO Utility (Barcode x2 + Elara RFID)
- Dynamic device discovery (robust to /dev/ttyACM* changing)
- Prefers udev symlinks if available (/dev/barcode0, /dev/barcode1, /dev/elara0)
- Falls back to VID/PID + by-path ordering if no symlinks
"""

import os, sys, glob, time, json, argparse
import serial
from serial.tools import list_ports

# ========== Vendor/Product IDs ==========
VID_ST          = 0x0483      # STMicroelectronics (barcode)
PID_ST_VCP      = 0x5740      # Virtual COM Port
PID_ST_ALT      = 0x0011      # Seen on some devices (your lsusb)
VID_ELARA       = 0x2008      # Novanta / ThingMagic (Elara)
PID_ELARA       = 0x2001

# ========== Defaults ==========
BARCODE_BAUD    = 9600
ELARA_BAUD      = 115200
ELARA_SAVE      = False          # Save Elara config after set?
MAX_WAIT_UNTIL_READ = None       # None = wait indefinitely for a read

DEFAULT_RFID_WORDS = 5           # last N 16-bit words to decode to ASCII

# ========== Helpers (filesystem) ==========
def _exists(p: str) -> bool:
    return bool(p) and os.path.exists(p)

def _by_path_of(dev_path: str) -> str | None:
    """Return the /dev/serial/by-path/* symlink that resolves to dev_path (for stable ordering)."""
    for p in glob.glob("/dev/serial/by-path/*"):
        try:
            if os.path.realpath(p) == os.path.realpath(dev_path):
                return p
        except Exception:
            pass
    return None

# ========== Discovery ==========
def discover_barcode_ports(prefer_symlink=True) -> dict:
    """
    Return mapping {'1': path_for_scanner_1, '2': path_for_scanner_2}
    Priority:
      1) /dev/barcode0, /dev/barcode1 (udev symlinks you created)
      2) Any serial ports with VID=0483 and PID in {5740,0011}, ordered by /dev/serial/by-path
    """
    # Prefer udev names if present (most stable)
    if prefer_symlink and (_exists("/dev/barcode0") or _exists("/dev/barcode1")):
        out = {}
        if _exists("/dev/barcode0"): out['1'] = "/dev/barcode0"
        if _exists("/dev/barcode1"): out['2'] = "/dev/barcode1"
        return out

    # Scan COM ports and filter by VID/PID
    devices = []
    for p in list_ports.comports():
        try:
            if p.vid != VID_ST:          # not ST
                continue
            if p.pid not in (PID_ST_VCP, PID_ST_ALT):
                continue
            devices.append(p.device)      # e.g., /dev/ttyACM2
        except Exception:
            continue

    if not devices:
        return {}

    # Order deterministically by by-path string
    decorated = []
    for dev in devices:
        bypath = _by_path_of(dev) or ""
        decorated.append((bypath, dev))
    decorated.sort(key=lambda x: x[0])

    out = {}
    if len(decorated) >= 1:
        out['1'] = decorated[0][1]
    if len(decorated) >= 2:
        out['2'] = decorated[1][1]
    return out

def discover_elara_port(prefer_symlink=True) -> str | None:
    """
    Return device path for Elara (RFID reader).
    Priority:
      1) /dev/elara0 (udev)
      2) /dev/serial/by-id/* that mentions Elara/Novanta
      3) VID/PID match (2008:2001)
    """
    if prefer_symlink and _exists("/dev/elara0"):
        return "/dev/elara0"

    for p in glob.glob("/dev/serial/by-id/*"):
        name = os.path.basename(p)
        if any(k in name for k in ("Elara", "Novanta", "2008_2001")):
            return p

    for p in list_ports.comports():
        if p.vid == VID_ELARA and p.pid == PID_ELARA:
            return p.device
    return None

# ========== Barcode (MCR12-style) ==========
def _mcr12_frame(cmd, da_bytes_12):
    """Build frame: STX(0x02), CMD, 12 data bytes, ETX(0x03), checksum."""
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    checksum = (256 - (sum(data) & 0xFF)) & 0xFF
    data.append(checksum)
    return data

def mcr12_enable(ser, delay_ms=0):
    """
    Start scanning:
    - delay_ms = 0 => continuous scan until mcr12_disable
    - delay_ms > 0 => one-shot scan window of delay_ms
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
    """Stop scanning."""
    da = [0x01, 0x00] + [0x00]*10
    ser.write(_mcr12_frame(0x01, da))

def mcr12_scan_until(ser, max_seconds=MAX_WAIT_UNTIL_READ):
    """
    Scan until a line (ending CR/LF) arrives, then stop.
    Return decoded string, or None on timeout/none.
    """
    try: ser.reset_input_buffer()
    except Exception: pass

    mcr12_enable(ser, delay_ms=0)
    t0   = time.time()
    buf  = bytearray()
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

# ========== Elara (JSON/RCI) ==========
elara = None

def elara_open(elara_path):
    global elara
    try:
        elara = serial.Serial(elara_path, ELARA_BAUD, timeout=0.2)
        print(f"[ELARA] open {elara_path} ok")
    except Exception as e:
        print(f"[ELARA] open {elara_path} failed: {e}")
        elara = None

def jsend(obj):
    if not elara: return
    elara.write((json.dumps(obj) + '\r\n').encode('utf-8'))

def jread(timeout=0.3):
    """Short poll — return list of lines."""
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
    """Quiet until StartRZ; report EPC/RSSI/MB."""
    if not elara: return
    jsend({"Cmd":"StopRZ","RZ":["ALL"]}); _ = jread(0.2)
    jsend({"Cmd":"SetCfg","Cfg":{"RdrStart":"NOTACTIVE"}}); _ = jread(0.2)
    jsend({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]});   _ = jread(0.2)
    jsend({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]});    _ = jread(0.2)
    jsend({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI","MB"]}}); _ = jread(0.2)
    if ELARA_SAVE:
        jsend({"Cmd":"Save"}); _ = jread(0.5)

def _split_words_from_mb(mb_field):
    """MB -> list of 4-hex words."""
    words = []
    if isinstance(mb_field, list):
        for entry in mb_field:
            if isinstance(entry, list) and len(entry) >= 3 and isinstance(entry[2], str):
                parts = [p.strip().lower() for p in entry[2].split(':') if p.strip()]
                for p in parts:
                    if len(p) == 4 and all(c in '0123456789abcdef' for c in p):
                        words.append(p)
    return words

def _split_words_from_epc(epc_hex: str):
    """EPC hex -> list of 4-hex words."""
    if not isinstance(epc_hex, str):
        return []
    h = ''.join([c for c in epc_hex.strip().lower() if c in '0123456789abcdef'])
    if len(h) % 4 != 0:
        h = h.zfill((len(h) + 3)//4 * 4)
    return [h[i:i+4] for i in range(0, len(h), 4)]

def _words_to_ascii(words, big_endian=True):
    """Join 16b words -> bytes -> ASCII (non-printables -> '.')."""
    bs = bytearray()
    for w in words:
        val = int(w, 16)
        hi, lo = ((val >> 8) & 0xFF, val & 0xFF)
        bs += bytes([hi, lo]) if big_endian else bytes([lo, hi])
    return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in bs)

def _decode_lastN_ascii_from_msg(msg, n_words):
    """
    Prefer MB words; fallback to EPC words.
    Trim trailing '0000'; take last N words.
    """
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

def elara_read_until(max_seconds, n_words_to_decode):
    """
    Start RZ0, wait for TagEvent once, then StopRZ.
    Return (epc, rssi, last_words, ascii_txt) or (None, None, None, None)
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

# ========== CLI / Main ==========
def main():
    ap = argparse.ArgumentParser(description="Smart cart barcode/RFID utility (auto-discovery).")
    ap.add_argument("--rfid-words", type=int, default=DEFAULT_RFID_WORDS,
                    help="จำนวนคำ (16-bit words) ท้ายที่ต้องการถอด ASCII จาก RFID (ค่าเริ่มต้น 5)")
    ap.add_argument("--no-symlink", action="store_true",
                    help="ไม่ใช้ udev symlink (/dev/barcode0/1, /dev/elara0) แม้มี — บังคับค้นหาจาก VID/PID")
    ap.add_argument("--barcode-baud", type=int, default=BARCODE_BAUD)
    ap.add_argument("--elara-baud", type=int, default=ELARA_BAUD)
    args = ap.parse_args()

    global ELARA_BAUD
    ELARA_BAUD = args.elara_baud

    # --- Discover ---
    BARCODE_PORTS = discover_barcode_ports(prefer_symlink=not args.no_symlink)
    ELARA_TTY     = discover_elara_port(prefer_symlink=not args.no_symlink)

    if BARCODE_PORTS:
        print("[DISCOVER] Barcode map:", BARCODE_PORTS)
    else:
        print("[DISCOVER] No barcode found (VID=0483, PID=5740/0011)")

    if ELARA_TTY:
        print("[DISCOVER] Elara port:", ELARA_TTY)
    else:
        print("[DISCOVER] No Elara found (VID:PID=2008:2001)")

    # --- Open Elara (optional) ---
    if ELARA_TTY:
        elara_open(ELARA_TTY)
        elara_set_manual_mode()

    # --- Open barcode serials ---
    sers: dict[str, serial.Serial] = {}
    for key in ('1', '2'):
        port = BARCODE_PORTS.get(key)
        if not port:
            print(f"[BARCODE{key}] not found")
            continue
        try:
            sers[key] = serial.Serial(port, args.barcode_baud, timeout=0.1)
            print(f"[BARCODE{key}] open {port} ok")
        except Exception as e:
            print(f"[BARCODE{key}] open {port} failed: {e}")

    # --- Simple interactive loop ---
    print("===== WAIT MODE =====")
    print("1 = scan BARCODE#1 (ต่อเนื่องจนอ่านได้)")
    print("2 = scan BARCODE#2 (ต่อเนื่องจนอ่านได้)")
    print(f"3 = read ELARA (รอ TagEvent + ถอด {args.rfid_words} คำท้ายเป็น ASCII)")
    print("q = quit")
    print("----------------------")

    try:
        while True:
            sel = input("> ").strip().lower()
            if sel in ('q', 'quit', 'exit'):
                break
            elif sel in ('1', '2'):
                if sel not in sers:
                    print(f"[BARCODE{sel}] port not open"); continue
                print(f"[BARCODE{sel}] scanning... (Ctrl+C to cancel)")
                try:
                    code = mcr12_scan_until(sers[sel], max_seconds=MAX_WAIT_UNTIL_READ)
                    if code: print(f"[BARCODE{sel}] {code}")
                    else:    print(f"[BARCODE{sel}] no read (timeout)")
                except KeyboardInterrupt:
                    print(f"[BARCODE{sel}] canceled")
            elif sel == '3':
                if not ELARA_TTY or not elara:
                    print("[ELARA] not available")
                    continue
                print(f"[ELARA] reading... decode last {args.rfid_words} word(s) (Ctrl+C to cancel)")
                try:
                    epc, rssi, last_words, ascii_txt = elara_read_until(MAX_WAIT_UNTIL_READ, args.rfid_words)
                    if epc:
                        print(f"[ELARA] EPC={epc} RSSI={rssi}")
                        if last_words:
                            print(f"[ELARA] MB/EPC last {len(last_words)} words: {':'.join(last_words)}")
                            print(f"[ELARA] ASCII: {ascii_txt}")
                        else:
                            print("[ELARA] (no MB/EPC words to decode)")
                    else:
                        print("[ELARA] no tag (timeout)")
                except KeyboardInterrupt:
                    print("[ELARA] canceled")
            elif sel == '':
                continue
            else:
                print("choose 1/2/3 or q")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        # Graceful close
        for s in sers.values():
            try: s.close()
            except: pass
        if elara:
            try: elara.close()
            except: pass

if __name__ == "__main__":
    main()
