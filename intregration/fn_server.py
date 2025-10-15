# fn_server.py
import os, json, time, unicodedata, re, tempfile
from typing import Dict, Any, List, Optional
import paho.mqtt.client as mqtt

# ========= PATHS (ค่าจาก Web App) =========
DATA_DIR   = os.path.expanduser("~/cart_ws/intregration/data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LOG_PATH   = os.path.join(DATA_DIR, "job_ids.jsonl")
os.makedirs(DATA_DIR, exist_ok=True)

# ========= ENV / CONFIG =========
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
MQTT_BASE = os.getenv("MQTT_BASE", "smartcart")
STATION_ID = os.getenv("STATION_ID", "slot1")

TOPIC_JOB_LATEST = f"{MQTT_BASE}/job/latest"
TOPIC_JOB_EVENT  = f"{MQTT_BASE}/job/event"
TOPIC_DETECT_DESIRED = f"{MQTT_BASE}/detect/{STATION_ID}/desired"
TOPIC_DETECT_MODE    = f"{MQTT_BASE}/detect/{STATION_ID}/mode"

# AMR topics (input from communicate_AMR)
TOPIC_AMR_STATUS_IN = f"{MQTT_BASE}/amr/status"
TOPIC_AMR_CONN_IN   = f"{MQTT_BASE}/amr/connected"

# ========= Utils =========
def now_fields():
    ts = time.time()
    lt = time.localtime(ts)
    return (
        ts,
        time.strftime("%Y-%m-%d", lt),
        time.strftime("%H:%M:%S", lt),
        time.strftime("%Y-%m-%dT%H:%M:%S%z", lt),
    )

def _to_none_token(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    s_norm = ''.join(ch for ch in unicodedata.normalize('NFKD', s)
                     if not unicodedata.combining(ch))
    return None if s_norm.lower() == "none" or s == "" else s

def normalize_ids(vals: List[Any]) -> List[str]:
    """คัด None/'None'/ว่าง ออก แล้วคืน list[str]"""
    out: List[str] = []
    for v in vals:
        nv = _to_none_token(v)
        if nv is not None:
            out.append(nv)
    return out

def _fill_two_slots(vals: List[str]) -> List[Optional[str]]:
    """ทำให้ยาว 2 ช่องเสมอ (เติม None)"""
    v = list(vals[:2])
    if len(v) == 0:
        return [None, None]
    if len(v) == 1:
        return [v[0], None]
    return v  # len == 2

# ========= Safe write =========
def _atomic_write(path: str, text: str):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False, encoding="utf-8") as tmp:
        tmp.write(text); tmp.flush(); os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def persist_state_and_log(cuh_ids: List[str], kit_ids: List[str], goal: str, ts, d, t, iso):
    """บันทึก state.json เป็น arrays 2 ช่อง + null ตาม slot; log แบบเดียวกัน"""
    cuh2 = _fill_two_slots(cuh_ids)
    kit2 = _fill_two_slots(kit_ids)

    payload = {
        "ts": ts, "date": d, "time": t, "iso": iso,
        "goal_id": goal,
        "cuh_ids": cuh2,
        "kit_ids": kit2
    }
    # legacy single (ช่อง 1 ถ้ามี)
    if cuh2[0] is not None:
        payload["cuh_id"] = cuh2[0]
    if kit2[0] is not None:
        payload["kit_id"] = kit2[0]

    state = {"latest_job_ids": payload}
    _atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

# ========= MQTT =========
def mqtt_init(client_id: str = "ws-bridge-server") -> mqtt.Client:
    cli = mqtt.Client(client_id=client_id, clean_session=True)
    if MQTT_USER: cli.username_pw_set(MQTT_USER, MQTT_PASS or "")
    cli.connect(MQTT_HOST, MQTT_PORT, 60)
    cli.loop_start()
    return cli

def mqtt_pub(cli: mqtt.Client, topic: str, obj: Dict[str, Any], qos=0, retain=False):
    cli.publish(topic, json.dumps(obj, ensure_ascii=False), qos=qos, retain=retain)

# ========= ARCL parse (ย่อ) =========
import re
_re_state        = re.compile(r"\bstate\s*[:=]\s*([A-Za-z_]+)", re.I)
_re_task_state   = re.compile(r"\btask\s*state\s*[:=]\s*([A-Za-z_]+)", re.I)
_re_batt_pct     = re.compile(r"\b(batt|battery).{0,10}?(\d{1,3})\s*%")
_re_batt_chg     = re.compile(r"\b(charging|discharging|dock(ed)?|undock(ed)?)", re.I)
_re_loc          = re.compile(r"\b(localization|loc)\s*[:=]\s*([A-Za-z_]+)", re.I)
_re_pose         = re.compile(r"\b(x|y|theta)\s*[:=]\s*(-?\d+(\.\d+)?)", re.I)

def parse_arcl_line(line: str) -> Dict[str, Any]:
    out = {}
    m = _re_state.search(line);       out["state"] = m.group(1).upper() if m else None
    m = _re_task_state.search(line);  out["task_state"] = m.group(1).upper() if m else None

    batt = {}
    p = _re_batt_pct.search(line)
    if p:
        try: batt["percent"] = int(p.group(2))
        except: pass
    c = _re_batt_chg.search(line)
    if c:
        kw = c.group(1).lower()
        batt["charging"] = ("charg" in kw) or ("dock" in kw)
    out["battery"] = batt if batt else None

    m = _re_loc.search(line);         out["localization"] = m.group(2).upper() if m else None

    pose = {}
    for k, v, _ in _re_pose.findall(line):
        try: pose[k.lower()] = float(v)
        except: pass
    out["pose"] = pose if pose else None

    return {k: v for k, v in out.items() if v is not None}

# ========= Mode / Publish =========
def detect_mode_any(cuh_ids: List[str], kit_ids: List[str], goal: Optional[str]) -> Optional[str]:
    if goal is None: return None
    has_cuh = len([x for x in cuh_ids if x is not None]) > 0
    has_kit = len([x for x in kit_ids if x is not None]) > 0
    if has_cuh and has_kit: return "BOTH"
    if has_cuh and not has_kit: return "CUH_ONLY"
    if has_kit and not has_cuh: return "KIT_ONLY"
    return None

def publish_job_topics(cli: mqtt.Client, cuh_ids: List[str], kit_ids: List[str], goal: str, ts, d, t, iso):
    cuh2 = _fill_two_slots(cuh_ids)
    kit2 = _fill_two_slots(kit_ids)
    payload = {
        "ts": ts, "date": d, "time": t, "iso": iso,
        "goal_id": goal,
        "cuh_ids": cuh2,
        "kit_ids": kit2,
        "cuh_id": cuh2[0],
        "kit_id": kit2[0]
    }
    mqtt_pub(cli, TOPIC_JOB_LATEST, payload, qos=1, retain=True)
    mqtt_pub(cli, TOPIC_JOB_EVENT,  payload, qos=0, retain=False)

def publish_detect_config(cli: mqtt.Client, cuh_ids: List[str], kit_ids: List[str], goal: str, ts, d, t, iso):
    mode = detect_mode_any(cuh_ids, kit_ids, goal)
    if not mode: return
    cuh2 = _fill_two_slots(cuh_ids)
    kit2 = _fill_two_slots(kit_ids)
    desired = {
        "req_id": f"{int(ts*1000)}",
        "mode": mode,
        "goal_id": goal,
        "cuh_ids": cuh2,
        "kit_ids": kit2,
        "cuh_id": cuh2[0],
        "kit_id": kit2[0],
        "edge": "rising",
        "window_ms": 1500,
        "ts": ts, "date": d, "time": t, "iso": iso
    }
    mqtt_pub(cli, TOPIC_DETECT_DESIRED, desired, qos=1, retain=True)
    mqtt_pub(cli, TOPIC_DETECT_MODE, {"mode": mode, "ts": ts}, qos=0, retain=False)

# ========= MQTT subscription (AMR) =========
def setup_amr_status_subscriptions(cli: mqtt.Client):
    def _on_connect(c, u, f, rc):
        print(f"[MQTT] connected rc={rc}; sub {TOPIC_AMR_CONN_IN}, {TOPIC_AMR_STATUS_IN}")
        c.subscribe(TOPIC_AMR_CONN_IN, qos=1)
        c.subscribe(TOPIC_AMR_STATUS_IN, qos=0)

    def _on_message(c, u, msg):
        topic = msg.topic
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            print(f"[AMR][MQTT] bad payload on {topic}: {e}")
            return
        ts, d, t, iso = now_fields()

        if topic == TOPIC_AMR_CONN_IN:
            connected = bool(data.get("connected"))
            print(f"[AMR][{iso}] CONNECTED = {connected}")
            return

        if topic == TOPIC_AMR_STATUS_IN:
            raw_ts = data.get("ts")
            line = data.get("line", "")
            parsed = parse_arcl_line(line)
            pretty = []
            if "state" in parsed:        pretty.append(f"state={parsed['state']}")
            if "task_state" in parsed:   pretty.append(f"task={parsed['task_state']}")
            if "battery" in parsed:
                b = parsed["battery"]; pretty.append("battery=" + ",".join([f"{k}:{v}" for k,v in b.items()]))
            if "localization" in parsed: pretty.append(f"loc={parsed['localization']}")
            if "pose" in parsed:
                p = parsed["pose"]; pretty.append("pose=" + ",".join([f"{k}:{v:.3f}" for k,v in p.items()]))
            if pretty:
                print(f"[AMR][{iso}] " + " | ".join(pretty) + f"  || raw: {line}")
            else:
                print(f"[AMR][{iso}] raw: {line} (unparsed)  raw_ts={raw_ts}")

    cli.on_connect = _on_connect
    cli.on_message = _on_message
