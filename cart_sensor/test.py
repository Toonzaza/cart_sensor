import  os, time

os.environ.setdefault("GPIOZERO_PIN_FACTORY","lgpio")

from gpiozero import DigitalInputDevice

PIN = 16

sensor= DigitalInputDevice(PIN,pull_up=False)

print("Reading GPIO23....(Ctrl+C to stop)")

while True:
    v = 1 if sensor.value else 0


    print(v)
    time.sleep(0.1)