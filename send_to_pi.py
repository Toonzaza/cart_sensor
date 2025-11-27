# send_list_once_fixed.py
import os, json, websocket

# ---------- CONFIG ----------
# DEFAULT_URL = "ws://192.168.1.102:8765"
DEFAULT_URL = "ws://192.168.0.50:8765" # IP Rasberry Pi
URL = os.getenv("WS_URL", DEFAULT_URL)

DEFAULT_OP = os.getenv("OP", "Request")

# format payload : [OP, CUH1, CUH2, MXK1, MXK2, DOT]
payload_1 = ["Request",  "CUH22-1043", "CUH22-1044", "MXK20-1003", "MXK20-1004", "DOT400002"]
payload_2 = ["Return",   "None",       "None",       "CUH22-1043", "None",       "DOT400002"]
payload_3 = ["Request",  "None",       "None",       "None",       "None",       "DOT400002"]
payload_4 = ["Return",   "1245452",    "None",       "None",       "None",       "DOT400002"]
payload_5 = ["Request",   "CUH22-1030",       "None",       "MXK22-1049", "None",       "DOT400002"]
payload_6 = ["Request",   "None",       "CUH22-1030",       "MXK22-1049", "None",       "DOT400002"]

legacy_payload = ["CUH22-1030", "CUH22-1044", "MXK20-1003", "MXK20-1004", "DOT400002"]

payload = payload_6  #  this payload must  send

BATCH_SEND = False
BATCH_LIST = [payload_1, payload_2, payload_3, payload_4, legacy_payload]
# ---------------------------


def _is_none_token(x) -> bool:
    
    return (x is None) or (isinstance(x, str) and x.strip() == "None")

def _canon_value(x):
    
    return None if _is_none_token(x) else str(x)

def _validate_and_prepare(p):
    
    if not isinstance(p, list):
        return False, "payload must be list"

    if len(p) == 6:
        op_raw, cuh1_raw, cuh2_raw, mxk1_raw, mxk2_raw, dot_raw = p
        op = str(op_raw)
        if op not in ("Request", "Return"):
            return False, f"OP must be 'Request' or 'Return'  (ได้ '{op_raw}')"
    elif len(p) == 5:
        cuh1_raw, cuh2_raw, mxk1_raw, mxk2_raw, dot_raw = p
        op = str(DEFAULT_OP)
        if op not in ("Request", "Return"):
            return False, f"DEFAULT_OP must be 'Request' or 'Return'  (ได้ '{DEFAULT_OP}')"
    else:
        return False,

    cuh1 = _canon_value(cuh1_raw)
    cuh2 = _canon_value(cuh2_raw)
    mxk1 = _canon_value(mxk1_raw)
    mxk2 = _canon_value(mxk2_raw)
    dot  = _canon_value(dot_raw)

    if dot is None:
        return False, "Error if DOT is None/'None'"

    
    if all(v is None for v in (cuh1, cuh2, mxk1, mxk2)):
        return False, 
    
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
