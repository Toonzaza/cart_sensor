#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import serial, json, time

# ================== พอร์ต/บอดเรต ==================
BARCODE_PORTS = {
    '1': '/dev/barcode0',  # -> /dev/ttyACM1 ไม่มี plate ช่องบน
    '2': '/dev/barcode1',  # -> /dev/ttyACM2 มี late ช่องล่าง
}
BARCODE_BAUD = 9600       # สำหรับ USB COM ของ MCR12 ให้ตั้งไว้ที่ใดก็ได้, pyserial ยังต้องการตัวเลข

ELARA_TTY  = '/dev/elara0'  # -> /dev/ttyACM3 ช่องบนขวา
ELARA_BAUD = 115200

# ตั้ง True ถ้าต้องการ Save ค่าลงเครื่องถาวร (เขียนแฟลช)
ELARA_SAVE = False

# ================== MCR12: serial command helpers ==================
def _mcr12_frame(cmd, da_bytes_12):
    """ประกอบเฟรมตามสเปค MCR12: STX(0x02), CMD, DA0..DA11(12B), ETX(0x03), SUM"""
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    checksum = (256 - (sum(data) & 0xFF)) & 0xFF
    data.append(checksum)
    return data

def mcr12_enable(ser, delay_ms=0):
    """
    เริ่มสแกน: delay_ms=0 = ยิงทันทีต่อเนื่องจนหยุด,
    delay_ms>0 = ยิงแบบกำหนดเวลา (มิลลิวินาที)
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

def mcr12_scan_once(ser, delay_ms=1200, window=2.0):
    """
    สั่งยิง 1 ครั้ง (delay_ms) แล้วรอผล 1 บรรทัดภายใน window วินาที
    คืนค่า: สตริงบาร์โค้ด หรือ None
    """
    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    mcr12_enable(ser, delay_ms=delay_ms)

    t0   = time.time()
    buf  = bytearray()
    line = None
    while time.time() - t0 < window:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if b'\r' in buf or b'\n' in buf:
                line = buf.replace(b'\r', b'\n').split(b'\n')[0].decode('utf-8', 'ignore').strip()
                break
        else:
            time.sleep(0.01)

    # ปิดสแกน (กันพลาด)
    mcr12_disable(ser)
    return line

# ================== ELARA: JSON/RCI helpers ==================
elara = None
try:
    elara = serial.Serial(ELARA_TTY, ELARA_BAUD, timeout=0.2)
    print(f"[ELARA] open {ELARA_TTY} ok")
except Exception as e:
    print(f"[ELARA] open {ELARA_TTY} failed: {e}")

def jsend(obj):
    if not elara:
        return
    elara.write((json.dumps(obj) + '\r\n').encode('utf-8'))

def jread(timeout=1.5):
    if not elara:
        return []
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        ln = elara.readline()
        if ln:
            s = ln.decode('utf-8', 'ignore').strip()
            if s:
                lines.append(s)
        else:
            time.sleep(0.02)
    return lines

def elara_set_manual_mode():
    """
    ตั้งค่าให้ Elara เงียบสนิทจนกว่าจะสั่ง StartRZ (software-only)
    - RdrStart = NOTACTIVE
    - SpotProfile (ID=1) ให้อ่านแบบครั้งเดียวต่อการสั่ง (DwnCnt=1)
    - ผูก ReadZone 0 กับ Profile 1
    - เลือกฟิลด์รายงาน EPC,RSSI (ย่อแพ็กเก็ต)
    """
    if not elara:
        return
    # หยุดก่อน กันค้าง
    jsend({"Cmd":"StopRZ","RZ":["ALL"]}); _ = jread(0.2)

    jsend({"Cmd":"SetCfg","Cfg":{"RdrStart":"NOTACTIVE"}}); _ = jread(0.2)
    jsend({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]});   _ = jread(0.2)
    jsend({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]});    _ = jread(0.2)
    jsend({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI"]}}); _ = jread(0.2)

    if ELARA_SAVE:
        jsend({"Cmd":"Save"}); _ = jread(0.5)

def elara_single_read(window=1.0):
    """
    เริ่มอ่าน RZ0 เมื่อสั่ง และหยุดหลังเจอแท็กแรก/ครบเวลา
    คืนค่า True/False
    """
    if not elara:
        print("[ELARA] no port")
        return False

    # เริ่มอ่าน
    jsend({"Cmd":"StartRZ","RZ":[0]})

    tag = None
    for s in jread(window):
        try:
            msg = json.loads(s)
        except Exception:
            continue
        if msg.get("Report") == "TagEvent":
            tag = msg
            break

    # หยุดอ่านทันที
    jsend({"Cmd":"StopRZ","RZ":[0]})

    if tag:
        epc  = tag.get('EPC') or tag.get('UII')
        rssi = tag.get('RSSI')
        print(f"[ELARA] EPC={epc} RSSI={rssi}")
        return True
    else:
        print("[ELARA] no tag")
        return False

# ================== main ==================
def main():
    # Init Elara ให้เป็นโหมด software-only
    elara_set_manual_mode()

    # เปิดพอร์ตบาร์โค้ดไว้ล่วงหน้า
    sers = {}
    for key, port in BARCODE_PORTS.items():
        try:
            sers[key] = serial.Serial(port, BARCODE_BAUD, timeout=0.1)
            print(f"[BARCODE{key}] open {port} ok")
        except Exception as e:
            print(f"[BARCODE{key}] open {port} failed: {e}")

    print("===== WAIT MODE =====")
    print("1 = scan /dev/barcode0 (MCR12)")
    print("2 = scan /dev/barcode1 (MCR12)")
    print("3 = read /dev/elara0 (RFID)")
    print("q = quit")
    print("----------------------")

    try:
        while True:
            sel = input("> ").strip().lower()
            if sel in ('q', 'quit', 'exit'):
                break
            elif sel in ('1', '2'):
                if sel not in sers:
                    print(f"[BARCODE{sel}] port not open")
                    continue
                code = mcr12_scan_once(sers[sel], delay_ms=1200, window=2.0)
                if code:
                    print(f"[BARCODE{sel}] {code}")
                else:
                    print(f"[BARCODE{sel}] no read")
            elif sel == '3':
                elara_single_read(window=1.0)
            elif sel == '':
                # Enter เฉย ๆ = รอคำสั่งต่อ
                continue
            else:
                print("choose 1/2/3 or q")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        for s in sers.values():
            try: s.close()
            except: pass
        if elara:
            try: elara.close()
            except: pass

if __name__ == "__main__":
    main()
