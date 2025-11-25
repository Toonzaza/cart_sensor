# send_list_once_fixed.py
import os, json, websocket

# ---------- CONFIG ----------
# DEFAULT_URL = "ws://192.168.1.102:8765"
DEFAULT_URL = "ws://192.168.0.50:8765"
URL = os.getenv("WS_URL", DEFAULT_URL)

# ถ้า payload เป็นแบบเก่า (5 ช่อง) จะใช้ OP นี้เป็นค่าเริ่มต้น (ต้องเป็น "Request" หรือ "Return")
DEFAULT_OP = os.getenv("OP", "Request")

# ตัวอย่าง payload แบบใหม่ (6 ช่อง): [OP, CUH1, CUH2, MXK1, MXK2, DOT]
payload_1 = ["Request",  "CUH22-1043", "CUH22-1044", "MXK20-1003", "MXK20-1004", "DOT400002"]
payload_2 = ["Return",   "None",       "None",       "CUH22-1043", "None",       "DOT400002"]  # ✅ มีอย่างน้อย 1 ตัว
payload_3 = ["Request",  "None",       "None",       "None",       "None",       "DOT400002"]  # ❌ ห้าม (4 ช่องแรกว่างหมด)
payload_4 = ["Return",   "1245452",    "None",       "None",       "None",       "DOT400002"]
payload_5 = ["Request",   "CUH22-1030",       "None",       "MXK22-1049", "None",       "DOT400002"]
payload_6 = ["Request",   "None",       "CUH22-1030",       "MXK22-1049", "None",       "DOT400002"]

# (ยังรองรับแบบเก่า 5 ช่อง): [CUH1, CUH2, MXK1, MXK2, DOT]
legacy_payload = ["CUH22-1030", "CUH22-1044", "MXK20-1003", "MXK20-1004", "DOT400002"]

# เลือกว่าจะส่งชุดไหน
payload = payload_6  # หรือ legacy_payload

# ส่งเป็นชุด ๆ
BATCH_SEND = False
BATCH_LIST = [payload_1, payload_2, payload_3, payload_4, legacy_payload]
# ---------------------------


def _is_none_token(x) -> bool:
    """True ถ้าเป็น None หรือสตริง 'None' แบบตรงตัว"""
    return (x is None) or (isinstance(x, str) and x.strip() == "None")

def _canon_value(x):
    """แปลง 'None' -> None, อย่างอื่นคงเดิมเป็นสตริง"""
    return None if _is_none_token(x) else str(x)

def _validate_and_prepare(p):
    """
    รูปแบบที่รับ:
      - ใหม่: [OP, CUH1, CUH2, MXK1, MXK2, DOT]  โดย OP ต้องเป็น 'Request' หรือ 'Return' เท่านั้น
      - เก่า: [CUH1, CUH2, MXK1, MXK2, DOT]      จะเติม OP=DEFAULT_OP ให้ (ต้องเป็น 'Request' หรือ 'Return')

    กติกา (ทั้งสองโหมด):
      - DOT ต้องไม่ None/'None'
      - 4 ช่องแรก (CUH1, CUH2, MXK1, MXK2) ต้องมีอย่างน้อย 1 ค่าไม่เป็น None/'None'

    คืน (ok: bool, data_or_errmsg) ; ถ้า ok คืนลิสต์ 6 ช่องตามรูปแบบใหม่
    """
    if not isinstance(p, list):
        return False, "payload ต้องเป็น list"

    if len(p) == 6:
        op_raw, cuh1_raw, cuh2_raw, mxk1_raw, mxk2_raw, dot_raw = p
        op = str(op_raw)
        if op not in ("Request", "Return"):
            return False, f"OP ต้องเป็น 'Request' หรือ 'Return' เท่านั้น (ได้ '{op_raw}')"
    elif len(p) == 5:
        cuh1_raw, cuh2_raw, mxk1_raw, mxk2_raw, dot_raw = p
        op = str(DEFAULT_OP)
        if op not in ("Request", "Return"):
            return False, f"DEFAULT_OP ต้องเป็น 'Request' หรือ 'Return' เท่านั้น (ได้ '{DEFAULT_OP}')"
    else:
        return False, "payload ต้องยาว 6 (ใหม่) หรือ 5 (เก่า)"

    cuh1 = _canon_value(cuh1_raw)
    cuh2 = _canon_value(cuh2_raw)
    mxk1 = _canon_value(mxk1_raw)
    mxk2 = _canon_value(mxk2_raw)
    dot  = _canon_value(dot_raw)

    if dot is None:
        return False, "ห้ามส่งเมื่อ DOT เป็น None/'None'"

    # ✅ บังคับทั้ง Request/Return ต้องมีอย่างน้อย 1 คิท
    if all(v is None for v in (cuh1, cuh2, mxk1, mxk2)):
        return False, "ต้องมีอย่างน้อย 1 kit ใน 4 ช่องแรก (CUH1, CUH2, MXK1, MXK2)"

    # คืนรูปแบบใหม่ (6 ช่อง) — ส่ง None เป็น null ใน JSON
    return True, [op, cuh1, cuh2, mxk1, mxk2, dot]

def _send_one(prepared):
    print(f"→ Connecting to {URL}")
    ws = websocket.create_connection(URL)
    print("✅ Connected")
    msg = json.dumps(prepared, ensure_ascii=False)
    ws.send(msg)
    print(f"🛰️  Sent: {msg}")
    try:
        ws.settimeout(2.0)
        print("📩 Recv:", ws.recv())
    except Exception:
        print("… no reply")
    ws.close()
    print("👋 Done\n")

def main():
    if BATCH_SEND:
        for idx, p in enumerate(BATCH_LIST, 1):
            ok, out = _validate_and_prepare(p)
            if not ok:
                print(f"[{idx}] ❌ ไม่ส่ง: {out} | payload={p}")
                continue
            print(f"[{idx}] ✅ ส่ง: {out}")
            _send_one(out)
    else:
        ok, out = _validate_and_prepare(payload)
        if not ok:
            print(f"❌ Data Incorrectly Formatted: {out} | payload={payload}")
            return
        print(f"✅ ส่ง: {out}")
        _send_one(out)

if __name__ == "__main__":
    main()
