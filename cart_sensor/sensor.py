#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import serial, json, time, argparse, sys

import paho.mqtt.client as mqtt
from datetime import datetime

mqtt_cli = None

def mqtt_connect(host, port, user=None, password=None, client_id="reader-node", keepalive=30):
    global mqtt_cli
    mqtt_cli = mqtt.Client(client_id=client_id, clean_session=True)
    if user and password:
        mqtt_cli.username_pw_set(user, password)
    mqtt_cli.connect(host, port, keepalive)
    mqtt_cli.loop_start()

def mqtt_pub(topic, obj, qos=0, retain=False):
    if mqtt_cli:
        payload = json.dumps(obj, ensure_ascii=False)
        mqtt_cli.publish(topic, payload, qos=qos, retain=retain)

def now_ms():
    return int(time.time() * 1000)


# ================== ค่าพื้นฐาน/พอร์ต ==================
BARCODE_PORTS = {
    '1': '/dev/barcode0',  # -> /dev/ttyACM1 ไม่มี plate ช่องบน
    '2': '/dev/barcode1',  # -> /dev/ttyACM2 มี plate ช่องล่าง
}
BARCODE_BAUD = 9600

ELARA_TTY   = '/dev/elara0'   # -> /dev/ttyACM3 ช่องบนขวา
ELARA_BAUD  = 115200
ELARA_SAVE  = False

MAX_WAIT_UNTIL_READ = None
DEFAULT_RFID_WORDS = 5

# ================== MCR12: serial command helpers ==================
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

def mcr12_scan_until(ser, max_seconds=MAX_WAIT_UNTIL_READ):
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

    # ตัด padding 0000 ด้านท้าย
    while words and words[-1] == '0000':
        words.pop()
    if not words:
        return (None, None)

    lastN = words[-n_words:] if len(words) >= n_words else words
    ascii_text = _words_to_ascii(lastN, big_endian=True)
    return (lastN, ascii_text)

def elara_read_until(max_seconds, n_words_to_decode):

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

# ================== main ==================
def main():
    parser = argparse.ArgumentParser(description="Smart cart barcode/RFID utility")
    parser.add_argument("--rfid-words", type=int, default=DEFAULT_RFID_WORDS,
                        help="จำนวนคำ (16-bit words) ท้ายที่ต้องการถอด ASCII จาก RFID (ค่าเริ่มต้น 5)")
    
    # ---- MQTT options ----
    parser.add_argument("--mqtt-host", default="127.0.0.1", help="MQTT broker host")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--mqtt-user", default=None)
    parser.add_argument("--mqtt-pass", default=None)
    parser.add_argument("--mqtt-base", default="smartcart", help="MQTT topic base")
    parser.add_argument("--device-id", default="pi5-01", help="who publishes (for trace)")

    
    args = parser.parse_args()

    # เปิด Elara และตั้งค่าโหมด manual
    elara_open()
    elara_set_manual_mode()

        # เปิด MQTT
    try:
        mqtt_connect(args.mqtt_host, args.mqtt_port, args.mqtt_user, args.mqtt_pass,
                     client_id=f"{args.device_id}-reader")
        print(f"[MQTT] connected to {args.mqtt_host}:{args.mqtt_port}")
    except Exception as e:
        print(f"[MQTT] failed: {e}")

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
    print(f"3 = read /dev/elara0   (อ่านจนพบแท็ก + ถอด {args.rfid_words} คำท้ายเป็น ASCII)")
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
                    if code: 
                        print(f"[BARCODE{sel}] {code}")
                        mqtt_pub(
                            f"{args.mqtt_base}/read/barcode/{sel}",
                            {
                                "type": "barcode",
                                "source": f"barcode{sel}",
                                "code": code,
                                "device": args.device_id,
                                "ts": now_ms()
                            },
                            qos=0, retain=False
                        )
                    else:    print(f"[BARCODE{sel}] no read (timeout)")
                except KeyboardInterrupt:
                    print(f"[BARCODE{sel}] canceled")
            elif sel == '3':
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
                        
                        mqtt_pub(
                            f"{args.mqtt_base}/read/rfid",
                            {
                                "type": "rfid",
                                "source": "elara0",
                                "epc": epc,
                                "rssi": rssi,
                                "last_words": last_words or [],
                                "ascii": ascii_txt or "",
                                "device": args.device_id,
                                "ts": now_ms()
                            },
                            qos=0, retain=False
                        )
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
        for s in sers.values():
            try: s.close()
            except: pass
        if elara:
            try: elara.close()
            except: pass

if __name__ == "__main__":
    main()
