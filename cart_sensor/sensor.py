#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import serial, json, time

# ================== พอร์ต/บอดเรต ==================
BARCODE_PORTS = {
    '1': '/dev/barcode0',  # -> /dev/ttyACM1 ไม่มี plate ช่องบน
    '2': '/dev/barcode1',  # -> /dev/ttyACM2 มี plate ช่องล่าง
}
BARCODE_BAUD = 9600       # USB COM ของ MCR12 ใส่เลขให้ pyserial พอ

ELARA_TTY  = '/dev/elara0'  # -> /dev/ttyACM3 ช่องบนขวา
ELARA_BAUD = 115200

# ตั้ง True ถ้าต้องการ Save ค่าลงเครื่องถาวร (เขียนแฟลช)
ELARA_SAVE = False

# หากอยากกันค้างนานเกิน กำหนดเวลารอสูงสุด (วินาที). ตั้ง None = รอไม่จำกัด
MAX_WAIT_UNTIL_READ = None  # เช่น 20, 30, หรือ None

# ================== MCR12: serial command helpers ==================
def _mcr12_frame(cmd, da_bytes_12):
    """ประกอบเฟรมตามสเปค MCR12: STX(0x02), CMD, DA0..DA11(12B), ETX(0x03), SUM"""
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    checksum = (256 - (sum(data) & 0xFF)) & 0xFF
    data.append(checksum)
    return data

def mcr12_enable(ser, delay_ms=0):
    """
    เริ่มสแกน: delay_ms=0 = ยิงต่อเนื่องจนหยุดเองด้วยคำสั่ง disable,
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

def mcr12_scan_until(ser, max_seconds=MAX_WAIT_UNTIL_READ):
    """
    สั่งสแกนต่อเนื่องจนกว่าจะอ่านได้ 1 บรรทัด จากนั้นหยุดและคืนค่าสตริงบาร์โค้ด
    ยกเลิกกลางคันด้วย Ctrl+C ได้
    """
    # เคลียร์บัฟเฟอร์ก่อน
    try: ser.reset_input_buffer()
    except Exception: pass

    mcr12_enable(ser, delay_ms=0)  # โหมดต่อเนื่อง
    t0 = time.time()
    buf = bytearray()
    line = None
    try:
        while True:
            chunk = ser.read(256)  # timeout ของพอร์ตกำหนดไว้สั้น ๆ
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
        mcr12_disable(ser)  # หยุดไม่ว่ากรณีใด ๆ

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

def jread(timeout=0.3):
    """อ่านเป็นช่วงสั้น ๆ เพื่อนำไปวนซ้ำรวบยอดเอง"""
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
    # หยุดและเคลียร์ก่อน กันค้าง
    jsend({"Cmd":"StopRZ","RZ":["ALL"]}); _ = jread(0.2)

    jsend({"Cmd":"SetCfg","Cfg":{"RdrStart":"NOTACTIVE"}}); _ = jread(0.2)
    jsend({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]});   _ = jread(0.2)
    jsend({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]});    _ = jread(0.2)
    jsend({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI"]}}); _ = jread(0.2)

    if ELARA_SAVE:
        jsend({"Cmd":"Save"}); _ = jread(0.5)

def elara_read_until(max_seconds=MAX_WAIT_UNTIL_READ):
    """
    เริ่มอ่าน RZ0 และวนจนกว่าจะเจอ TagEvent แล้วหยุด
    คืนค่า (epc, rssi) หรือ (None, None) ถ้าไม่เจอในเวลาที่กำหนด
    ยกเลิกกลางคันด้วย Ctrl+C ได้
    """
    if not elara:
        print("[ELARA] no port")
        return (None, None)

    # กันค้างเดิม
    jsend({"Cmd":"StopRZ","RZ":[0]}); _ = jread(0.1)

    # เริ่มอ่าน
    jsend({"Cmd":"StartRZ","RZ":[0]})

    t0 = time.time()
    epc, rssi = None, None
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
                    # เจอแล้ว ออกจากลูป
                    raise StopIteration

            if max_seconds is not None and (time.time() - t0) > max_seconds:
                break
    except StopIteration:
        pass
    finally:
        # หยุดอ่านทันที
        jsend({"Cmd":"StopRZ","RZ":[0]})

    return (epc, rssi)

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
    print("1 = scan /dev/barcode0 (สแกนต่อเนื่องจนได้ค่า)")
    print("2 = scan /dev/barcode1 (สแกนต่อเนื่องจนได้ค่า)")
    print("3 = read /dev/elara0   (อ่านต่อเนื่องจนได้แท็ก)")
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
                print(f"[BARCODE{sel}] scanning... (Ctrl+C to cancel)")
                try:
                    code = mcr12_scan_until(sers[sel], max_seconds=MAX_WAIT_UNTIL_READ)
                    if code:
                        print(f"[BARCODE{sel}] {code}")
                    else:
                        print(f"[BARCODE{sel}] no read (timeout)")
                except KeyboardInterrupt:
                    print(f"[BARCODE{sel}] canceled")
            elif sel == '3':
                print("[ELARA] reading... (Ctrl+C to cancel)")
                try:
                    epc, rssi = elara_read_until(max_seconds=MAX_WAIT_UNTIL_READ)
                    if epc:
                        print(f"[ELARA] EPC={epc} RSSI={rssi}")
                    else:
                        print("[ELARA] no tag (timeout)")
                except KeyboardInterrupt:
                    print("[ELARA] canceled")
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
