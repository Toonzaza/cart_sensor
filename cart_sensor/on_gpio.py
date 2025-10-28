import os
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")

from gpiozero import OutputDevice
from signal import pause

PIN_OUT = 20
out = OutputDevice(PIN_OUT, active_high=True, initial_value=True)  # เปิดทันที (3.3V)

print("GPIO20 = HIGH (3.3V) — กด Ctrl+C เพื่อออก โดยยังคง HIGH ระหว่างรันโปรแกรม")
pause()  # ให้โปรแกรมค้างไว้
