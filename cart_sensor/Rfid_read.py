from gpiozero import Button
import serial, json, time

PHOTO_IN_BCM = 18  # กำหนดพอร์ต GPIO ที่เชื่อมต่อกับ Photoelectric sensor (ไม่ใช้งานในตอนนี้)
TTY = '/dev/elara0'  # ชื่อพอร์ตของ Elara
BAUD = 115200

ser = serial.Serial(TTY, BAUD, timeout=0.2)

def jsend(obj):
    """ส่งคำสั่ง JSON ไปยัง Elara"""
    ser.write((json.dumps(obj) + '\r\n').encode('utf-8'))

def jread(timeout=1.5):
    """อ่านข้อมูล JSON จาก Elara ภายในเวลา limit"""
    t0 = time.time()
    lines = []
    while time.time() - t0 < timeout:
        ln = ser.readline()
        if ln:
            s = ln.decode('utf-8','ignore').strip()
            if s:
                lines.append(s)
        else:
            time.sleep(0.02)
    return lines

# --- คอนฟิกครั้งแรก (ทำในโค้ดได้ หรือทำใน TCT ไปแล้วก็ข้ามได้) ---
jsend({"Cmd":"GetInfo"})
_ = jread(0.5)

# โปรไฟล์อ่านครั้งเดียว + ผูก RZ 0
jsend({"Cmd":"SetProf","Prof":[{"ID":1,"DwnCnt":1}]})
jsend({"Cmd":"SetRZ","RZ":[{"ID":0,"ProfIDs":[1]}]})
# ลดฟิลด์รายงานให้สั้น (ตัวอย่าง)
jsend({"Cmd":"SetRpt","Rpt":{"Fields":["EPC","RSSI"]}})
# jsend({"Cmd":"Save"})  # ถ้าต้องการบันทึกถาวร

# แทนการใช้ปุ่มจากเซ็นเซอร์, ใช้ input() แทน
def manual_trigger():
    input("Press Enter to trigger RFID read...")  # ให้ผู้ใช้กด Enter เพื่อทริก

def single_read(window=1.5):
    """เริ่มการอ่านแท็ก"""
    # เริ่มการอ่าน
    jsend({"Cmd":"StartRZ","RZ":[0]})
    first_tag = None
    for s in jread(window):
        if '"Report":"TagEvent"' in s:
            first_tag = s
            break
    # หยุด (DwnCnt=1 จะหยุดเอง แต่สั่งซ้ำปลอดภัย)
    jsend({"Cmd":"StopRZ","RZ":[0]})
    if first_tag:
        print("TAG:", first_tag)
        return True
    else:
        print("No tag within window")
        return False

print("Ready. Waiting for manual trigger…")
while True:
    manual_trigger()     # รอให้ผู้ใช้กด Enter เพื่อทริก
    single_read(window=1.0)  # เปิดอ่านสั้น ๆ 1 วินาที (ปรับตามจริง)
