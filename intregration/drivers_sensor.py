import os, sys, time, json, threading, serial
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
try:
    from gpiozero import DigitalInputDevice
except Exception as e:
    DigitalInputDevice = None
    print(f"[GPIO] gpiozero not available: {e}", file=sys.stderr)

DEBOUNCE_MS = 200
GPIO_PULL_UP = False

# ---------- GPIO ----------
def make_gpio_input(pin):
    if DigitalInputDevice is None:
        return None
    return DigitalInputDevice(pin, pull_up=GPIO_PULL_UP, bounce_time=DEBOUNCE_MS/1000.0)

# ---------- Barcode (MCR12 เหมือนของเดิมย่อ) ----------
def _mcr12_frame(cmd, da_bytes_12):
    data = bytearray([0x02, cmd]) + bytearray(da_bytes_12[:12]) + bytearray([0x03])
    data.append((256 - (sum(data) & 0xFF)) & 0xFF)
    return data

def mcr12_enable(ser): ser.write(_mcr12_frame(0x01, [0x01,0x01,0,0] + [0]*8))
def mcr12_disable(ser): ser.write(_mcr12_frame(0x01, [0x01,0x00] + [0]*10))

def barcode_open(port, baud=9600, timeout=0.1):
    try:
        s = serial.Serial(port, baud, timeout=timeout); print(f"[BARCODE] open {port} ok"); return s
    except Exception as e:
        print(f"[BARCODE] open {port} failed: {e}"); return None

def barcode_read_once(ser, max_seconds=None):
    if ser is None: return None
    try: ser.reset_input_buffer()
    except: pass
    mcr12_enable(ser)
    t0, buf, line = time.time(), bytearray(), None
    try:
        while True:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                if b'\r' in buf or b'\n' in buf:
                    line = buf.replace(b'\r', b'\n').split(b'\n')[0].decode('utf-8','ignore').strip()
                    if line: break
            else:
                time.sleep(0.01)
            if (max_seconds is not None) and (time.time()-t0 > max_seconds): break
    finally:
        mcr12_disable(ser)
    return line

# ---------- RFID (Elara เหมือนของเดิมย่อ) ----------
def elara_open(port='/dev/elara0', baud=115200):
    try:
        s = serial.Serial(port, baud, timeout=0.2); print(f"[ELARA] open {port} ok"); return s
    except Exception as e:
        print(f"[ELARA] open {port} failed: {e}"); return None

def elara_set_manual_mode(elara):
    if not elara: return
    def send(obj): elara.write((json.dumps(obj)+'\r\n').encode())
    send({"Cmd":"StopRZ","RZ":["ALL"]}); elara.readline()
    send({"Cmd":"SetCfg","Cfg":{"RdrStart":"NOTACTIVE"}}); elara.readline()
    send({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]}); elara.readline()
    send({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]}); elara.readline()
    send({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI","MB"]}}); elara.readline()

def _split_words_from_mb(mb):
    words=[]; 
    if isinstance(mb, list):
        for e in mb:
            if isinstance(e, list) and len(e)>=3 and isinstance(e[2], str):
                words += [p for p in e[2].split(':') if len(p.strip())==4]
    return words

def _words_to_ascii(words):
    bs=bytearray()
    for w in words:
        v=int(w,16); bs += bytes([(v>>8)&0xFF, v&0xFF])
    return ''.join(chr(b) if 32<=b<=126 else '.' for b in bs)

def elara_read_once(elara, max_seconds=None, last_words=5):
    if not elara: return (None,None,None)
    def send(obj): elara.write((json.dumps(obj)+'\r\n').encode())
    send({"Cmd":"StopRZ","RZ":[0]}); elara.readline()
    send({"Cmd":"StartRZ","RZ":[0]})
    t0=time.time(); epc=None; rssi=None; ascii_txt=None
    try:
        while True:
            s=elara.readline().decode('utf-8','ignore').strip()
            if s:
                try: msg=json.loads(s)
                except: msg={}
                if msg.get("Report")=="TagEvent":
                    epc  = msg.get('EPC') or msg.get('UII')
                    rssi = msg.get('RSSI')
                    words = _split_words_from_mb(msg.get('MB')) or []
                    while words and words[-1]=='0000': words.pop()
                    ascii_txt = _words_to_ascii(words[-last_words:] or [])
                    break
            if (max_seconds is not None) and (time.time()-t0 > max_seconds): break
    finally:
        send({"Cmd":"StopRZ","RZ":[0]})
    return (epc, rssi, ascii_txt)

# ---------- Locks ----------
ELARA_LOCK = threading.Lock()
BARCODE_LOCKS = {'1': threading.Lock(), '2': threading.Lock()}
