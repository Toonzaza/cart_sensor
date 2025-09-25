#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, re, sys
from typing import List, Tuple

# ให้พาธสอดคล้องกับ server.py ของคุณ
DATA_DIR   = os.path.expanduser("~/cart_ws/data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

CUH_PAT  = re.compile(r"^CUH[\w-]+$", re.IGNORECASE)
KIT_PAT  = re.compile(r"^MXK[\w-]+$", re.IGNORECASE)
GOAL_PAT = re.compile(r"^DOT[\w-]+$", re.IGNORECASE)

def _classify_ids_from_list(tokens: List[str]):
    """
    tokens: เช่น ["CUH22-1043","MXK20-1003","DOT400002"]
    return: {"cuh_id":..., "kit_id":..., "goal_id":...}
    """
    cuh = kit = goal = None
    for t in tokens:
        s = str(t).strip().upper()
        if CUH_PAT.match(s):
            cuh = s
        elif KIT_PAT.match(s):
            kit = s
        elif GOAL_PAT.match(s):
            goal = s
        else:
            # ignore โทเคนที่ไม่เข้าแพทเทิร์น
            pass
    missing = []
    if not cuh:  missing.append("CUH*")
    if not kit:  missing.append("MXK*")
    if not goal: missing.append("DOT*")
    if missing:
        raise ValueError(f"missing or invalid: {', '.join(missing)}")
    return {"cuh_id": cuh, "kit_id": kit, "goal_id": goal}

def _load_latest_job_ids(state_path: str = STATE_PATH):
    """
    อ่านค่า latest_job_ids จาก state.json
    โครงไฟล์ (ตาม server.py):
    {
      "latest_job_ids": { "ts": 169..., "cuh_id": "...", "kit_id": "...", "goal_id": "..." }
    }
    """
    if not os.path.exists(state_path):
        raise FileNotFoundError(f"state file not found: {state_path}")
    with open(state_path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    latest = obj.get("latest_job_ids") or {}
    cuh = (latest.get("cuh_id") or "").strip().upper()
    kit = (latest.get("kit_id") or "").strip().upper()
    goal = (latest.get("goal_id") or "").strip().upper()
    if not (cuh and kit and goal):
        raise ValueError("state.json has no complete latest_job_ids")
    return {"cuh_id": cuh, "kit_id": kit, "goal_id": goal}

def match_tokens(tokens: List[str]) -> Tuple[bool, str]:
    """
    ตรวจว่า tokens ตรงกับ latest_job_ids ใน state.json หรือไม่
    คืน (is_match, detail_message)
    """
    scanned = _classify_ids_from_list(tokens)
    latest  = _load_latest_job_ids()

    ok = (
        scanned["cuh_id"]  == latest["cuh_id"] and
        scanned["kit_id"]  == latest["kit_id"] and
        scanned["goal_id"] == latest["goal_id"]
    )
    if ok:
        return True, "match ID True"
    else:
        # สร้างรายละเอียดว่าไม่ตรงตรงไหน
        diff = []
        for k in ("cuh_id","kit_id","goal_id"):
            if scanned[k] != latest[k]:
                diff.append(f"{k}: scanned={scanned[k]} latest={latest[k]}")
        return False, "match ID False | " + "; ".join(diff)

# ---------- โหมด CLI ----------
# ใช้งาน:
#   python match_id.py CUH22-1043 MXK20-1003 DOT400002
# หรือ:
#   python match_id.py '["CUH22-1043","MXK20-1003","DOT400002"]'
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: match_id.py CUH... MXK... DOT... | '[\"CUH...\",\"MXK...\",\"DOT...\"]'")
        sys.exit(2)

    # รองรับใส่เป็น JSON list หรือ 3 อาร์กิวเมนต์
    arg = sys.argv[1]
    if arg.startswith("["):
        try:
            tokens = json.loads(arg)
        except Exception as e:
            print(f"invalid JSON list: {e}")
            sys.exit(2)
    else:
        tokens = sys.argv[1:]
    try:
        is_ok, msg = match_tokens(tokens)
        print(msg)
        sys.exit(0 if is_ok else 1)
    except Exception as e:
        print(f"error: {e}")
        sys.exit(2)
