# arcl_emulator_server.py
import socket
import threading
import datetime
import json

HOST = "0.0.0.0"   # ฟังทุก interface
PORT = 7171        # เลือกพอร์ตที่คุณตั้งใน MobilePlanner

# สถานะจำลอง
state = {
    "x": 1.23,
    "y": 2.34,
    "theta": 0.12,
    "mode": "idle",
    "goal": None,
    "timestamp": str(datetime.datetime.now())
}

def handle_client(conn, addr):
    print(f"[conn] {addr} connected")
    with conn:
        conn.sendall(b"ARCL-EMULATOR-READY\n")  # initial banner (ไม่บังคับ)
        buf = b""
        while True:
            try:
                data = conn.recv(4096)
                if not data:
                    print(f"[conn] {addr} closed")
                    break
                buf += data
                # แยกบรรทัด (ARCL มักส่งเป็น newline terminated)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode("utf-8", errors="ignore").strip()
                    if not cmd:
                        continue
                    print(f"[recv {addr}] {cmd}")
                    resp = process_command(cmd)
                    # ส่ง response + newline
                    conn.sendall((resp + "\n").encode("utf-8"))
            except ConnectionResetError:
                print(f"[conn] {addr} reset")
                break
            except Exception as e:
                print("Exception:", e)
                break

def process_command(cmd):
    global state
    lower = cmd.lower()
    # ตัวอย่าง parsing คำสั่งพื้นฐาน (ปรับรูปแบบจริงตาม ARCL ที่ MobilePlanner ใช้)
    if lower in ("ping", "hello"):
        return "pong"
    if lower.startswith("getposition") or lower.startswith("get_position") or lower == "getPosition":
        # ตอบในรูปแบบง่าย ๆ
        state["timestamp"] = str(datetime.datetime.now())
        return f"POSITION {state['x']} {state['y']} {state['theta']}"
    if lower.startswith("getallstatus") or lower == "getAllStatus":
        state["timestamp"] = str(datetime.datetime.now())
        # ตอบเป็น JSON string หรือเป็น ARCL text ขึ้นกับที่ MobilePlanner คาดหวัง
        return json.dumps({
            "x": state["x"],
            "y": state["y"],
            "theta": state["theta"],
            "mode": state["mode"],
            "goal": state["goal"],
            "ts": state["timestamp"]
        })
    if lower.startswith("setgoal"):
        # ex: setGoal 3.0 4.0 0.0
        parts = cmd.split()
        try:
            x = float(parts[1]); y = float(parts[2]); theta = float(parts[3]) if len(parts)>3 else 0.0
            state["goal"] = {"x": x, "y": y, "theta": theta}
            state["mode"] = "moving"
            return f"OK setGoal {x} {y} {theta}"
        except:
            return "ERROR bad setGoal"
    if lower.startswith("cancelgoal"):
        state["goal"] = None
        state["mode"] = "idle"
        return "OK cancel"
    # default
    return f"UNKNOWN_CMD {cmd}"

def main():
    print("ARCL emulator listening on", HOST, PORT)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((HOST, PORT))
    s.listen(5)
    try:
        while True:
            conn, addr = s.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("stopping")
    finally:
        s.close()

if __name__ == "__main__":
    main()
