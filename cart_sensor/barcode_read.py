import serial
import time
import sys

# =======================
# CONFIG ส่วนที่ต้องปรับ
# =======================
PORT = "/dev/ttyACM0"
BAUD = 9600

# ใส่คำสั่ง Trigger ของหัวอ่านตามคู่มือ (ถ้าไม่มี/ไม่ต้อง ใช้ None)
# ตัวอย่าง: TRIGGER_ON  = b'\x16T'   # <== แค่ตัวอย่าง! อย่าใช้ถ้าไม่ตรงคู่มือ
#           TRIGGER_OFF = b'\x16U'
TRIGGER_ON  = None
TRIGGER_OFF = None

# ตัวอักษรปิดท้ายบาร์โค้ดที่หัวอ่านส่งกลับ (พบบ่อย: \r, \n, หรือ \r\n)
END_OF_CODE = b'\r'   # ปรับได้ตามหัวอ่านของคุณ

# กำหนดเวลาหน้าต่างสแกน (วินาที) หลังจากสั่ง Trigger
SCAN_WINDOW_SEC = 3.0

# =======================
# ฟังก์ชันช่วย
# =======================
def setup_serial(port: str, baud: int) -> serial.Serial:
    ser = serial.Serial(port, baud, timeout=0.1)  # timeout สั้น ๆ เพื่อวนอ่านทันที
    time.sleep(0.2)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser

def send_trigger(ser: serial.Serial, on: bool):
    """ส่งคำสั่ง Trigger ON/OFF ตามที่ตั้งค่าไว้ด้านบน"""
    try:
        if on and TRIGGER_ON is not None:
            ser.write(TRIGGER_ON)
            ser.flush()
        elif (not on) and TRIGGER_OFF is not None:
            ser.write(TRIGGER_OFF)
            ser.flush()
    except Exception as e:
        print(f"[WARN] ส่ง Trigger ไม่ได้: {e}")

def read_one_code(ser: serial.Serial, window_sec: float, terminator: bytes) -> str | None:
    """
    รออ่านบาร์โค้ด 1 รายการภายในเวลาที่กำหนด
    จะหยุดเมื่อเจอ terminator (เช่น \r) หรือหมดเวลา
    """
    deadline = time.time() + window_sec
    buf = bytearray()
    while time.time() < deadline:
        n = ser.in_waiting
        chunk = ser.read(n if n > 0 else 1)
        if chunk:
            buf.extend(chunk)
            # ถ้ามีตัวจบแล้ว ตัดบรรทัดแรกออกมา
            if terminator and terminator in buf:
                line = bytes(buf).splitlines()[0]
                try:
                    return line.decode("utf-8", errors="ignore").strip()
                except UnicodeDecodeError:
                    return line.decode("latin1", errors="ignore").strip()
        # ผ่อน CPU หน่อย
        time.sleep(0.01)

    # ถ้าไม่มี terminator ให้ลองตีความทั้งบัฟเฟอร์
    if buf:
        try:
            return bytes(buf).decode("utf-8", errors="ignore").strip()
        except UnicodeDecodeError:
            return bytes(buf).decode("latin1", errors="ignore").strip()
    return None

def scan_once(ser: serial.Serial) -> str | None:
    """
    ขั้นตอนสแกนครั้งเดียว:
    - เคลียร์บัฟเฟอร์ทิ้ง
    - Trigger ON (ถ้ามีคำสั่ง)
    - รออ่านจนเจอผลหรือหมดเวลา
    - Trigger OFF (ถ้ามีคำสั่ง)
    """
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # เข้าโหมด SCANNING
    send_trigger(ser, on=True)
    code = None
    try:
        code = read_one_code(ser, SCAN_WINDOW_SEC, END_OF_CODE)
    finally:
        # กลับสู่ WAIT: ปิด Trigger ถ้ามี
        send_trigger(ser, on=False)
    return code

# =======================
# โปรแกรมหลัก (State Machine)
# =======================
def main():
    try:
        ser = setup_serial(PORT, BAUD)
    except Exception as e:
        print(f"[ERROR] เปิดพอร์ต {PORT} ไม่ได้: {e}")
        sys.exit(1)

    print("=== MODE: WAIT ===")
    print("กด Enter เพื่อสแกน | พิมพ์ q แล้ว Enter เพื่อออก")

    try:
        while True:
            user = input("> ").strip().lower()
            if user == "q":
                break

            # กด Enter (ข้อความว่าง) = เริ่ม SCANNING
            if user == "":
                print("=== MODE: SCANNING === (กำลังสั่งหัวอ่าน)")

                code = scan_once(ser)
                if code:
                    print(f"[OK] Barcode: {code}")
                else:
                    print("[NO READ] ไม่ได้บาร์โค้ดภายในเวลาที่กำหนด")

                print("=== กลับสู่ MODE: WAIT ===")
            else:
                print("พิมพ์ว่าง ๆ (Enter อย่างเดียว) เพื่อสแกน หรือ q เพื่อออก")

    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.close()
        except:
            pass
        print("ปิดโปรแกรม")

if __name__ == "__main__":
    main()


