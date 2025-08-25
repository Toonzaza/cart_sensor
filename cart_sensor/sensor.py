#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import serial, time, json

BARCODE_PORTS = {
    '1': '/dev/barcode0',  # -> /dev/ttyACM1
    '2': '/dev/barcode1',  # -> /dev/ttyACM2
}
BARCODE_BAUD = 9600  # USB COM มักไม่ซีเรียส แต่กำหนดไว้ให้ pyserial
READ_WINDOW_S = 2.0  # เวลารวมที่รอข้อความบาร์โค้ดกลับมา

# ---------- Serial command (MCR12) ----------
def _mcr12_frame(cmd, da_bytes_12):
    """ประกอบเฟรมตามสเปค: 0x02, CMD, DA0..DA11, 0x03, SUM"""
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    checksum = (256 - (sum(data) & 0xFF)) & 0xFF
    data.append(checksum)
    return data

def mcr12_enable(ser, delay_ms=0):
    """เริ่มสแกน: delay_ms=0 คือ enable ต่อเนื่องจนกว่าจะอ่านได้; >0 คือสแกนภายในเวลาที่กำหนด"""
    # CMD=0x01 (control), DA0=0x01, DA1=0x01 (ทันที) หรือ 0x02 (มีเวลา), DA2..DA3=เวลา (ms, little-endian)
    DA0 = 0x01
    if delay_ms and delay_ms > 0:
        DA1 = 0x02
        DA2 = delay_ms & 0xFF
        DA3 = (delay_ms >> 8) & 0xFF
    else:
        DA1 = 0x01
        DA2 = 0x00
        DA3 = 0x00
    da = [DA0, DA1, DA2, DA3] + [0x00]*8
    ser.write(_mcr12_frame(0x01, da))

def mcr12_disable(ser):
    """หยุดสแกน"""
    da = [0x01, 0x00] + [0x00]*10
    ser.write(_mcr12_frame(0x01, da))

def mcr12_scan_once(ser, delay_ms=1200, window=READ_WINDOW_S):
    """สั่งยิง 1 ครั้ง แล้วอ่านคืน 1 บรรทัด"""
    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    mcr12_enable(ser, delay_ms=delay_ms)

    t0 = time.time()
    buf = bytearray()
    line = None
    while time.time() - t0 < window:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            # ถ้ามี CR/LF ก็ถือว่าได้ 1 บรรทัดแล้ว
            if b'\r' in buf or b'\n' in buf:
                line = buf.replace(b'\r', b'\n').split(b'\n')[0].decode('utf-8', 'ignore').strip()
                break
        else:
            time.sleep(0.01)

    # ปิดการสแกน (แม้แบบมีเวลา จะหยุดเอง แต่สั่งไว้เพื่อความชัวร์)
    mcr12_disable(ser)
    return line

# ---------- ส่วนของ Elara (ยกมาของเดิม) ----------
ELARA_TTY  = '/dev/elara0'
ELARA_BAUD = 115200
elara = None
try:
    elara = serial.Serial(ELARA_TTY, ELARA_BAUD, timeout=0.2)
except Exception as e:
    print(f"[ELARA] open {ELARA_TTY} failed: {e}")

def jsend(obj):
    if not elara: return
    elara.write((json.dumps(obj) + '\r\n').encode('utf-8'))

def jread(timeout=1.5):
    if not elara: return []
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        ln = elara.readline()
        if ln:
            s = ln.decode('utf-8','ignore').strip()
            if s: lines.append(s)
        else:
            time.sleep(0.02)
    return lines

def elara_init_once():
    if not elara: return
    jsend({"Cmd":"GetInfo"}); _ = jread(0.5)
    jsend({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]})
    jsend({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]})
    jsend({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI"]}})
    _ = jread(0.3)

def elara_single_read(window=1.0):
    if not elara:
        print("[ELARA] no port"); return False
    jsend({"Cmd":"StartRZ","RZ":[0]})
    tag = None
    for s in jread(window):
        try:
            msg = json.loads(s)
            if msg.get("Report") == "TagEvent": tag = msg; break
        except Exception:
            pass
    jsend({"Cmd":"StopRZ","RZ":[0]})
    if tag:
        print(f"[ELARA] EPC={tag.get('EPC')} RSSI={tag.get('RSSI')}")
        return True
    print("[ELARA] no tag"); return False

# ---------- main ----------
def main():
    elara_init_once()

    # เปิดพอร์ตบาร์โค้ดไว้ล่วงหน้า
    sers = {}
    for key, port in BARCODE_PORTS.items():
        try:
            sers[key] = serial.Serial(port, BARCODE_BAUD, timeout=0.1)
            print(f"[BARCODE{key}] open {port} ok")
        except Exception as e:
            print(f"[BARCODE{key}] open {port} failed: {e}")

    print("WAIT MODE: 1=/dev/barcode0, 2=/dev/barcode1, 3=ELARA, q=quit")
    while True:
        sel = input("> ").strip().lower()
        if sel in ('q','quit','exit'):
            break
        elif sel in ('1','2') and sel in sers:
            code = mcr12_scan_once(sers[sel], delay_ms=1200, window=2.0)
            if code: print(f"[BARCODE{sel}] {code}")
            else:    print(f"[BARCODE{sel}] no read")
        elif sel == '3':
            elara_single_read(window=1.0)
        else:
            print("choose 1/2/3 or q")

    for s in sers.values():
        try: s.close()
        except: pass
    if elara:
        try: elara.close()
        except: pass

if __name__ == "__main__":
    main()