# SmartCart Backend System

Real-time Sensor Processing · ID Matching · AMR Dispatching · WebSocket API · MQTT Architecture

This repository contains the backend system for **SmartCart**, responsible for coordinating:

- Photoelectric sensors  
- Barcode scanners  
- RFID reader  
- LED indicators  
- WebSocket server  
- AMR communication via ARCL  
- Central MQTT message bus  

The system is designed for **modularity, fault-tolerance, and clean separation of logic**.

---

## Requirements

- Python ≥ 3.9

- MQTT Broker (Mosquitto recommended)

- Python packages:

paho-mqtt, websockets, gpiozero, lgpio, pyserial
paho-mqtt, websockets, gpiozero, lgpio, pyserial


## How to Run Raspberry Pi in Smart Cart
Use Putty for remote to command a Rasberry Pi5 (SSH)

Rasberry Pi IP (Fix)

    192,168.0.50

Port

    22

Username

    fibo

Password

    1234

![image](https://media.discordapp.net/attachments/1380408352743362692/1442791621107388507/image.png?ex=6926b7e8&is=69256668&hm=fbca3bb642b2a770ea5d37299a0ecec4dba808a2df514f6564012217bf34d6e4&=&format=webp&quality=lossless)



*After that you can follow this step for start Smart Cart command.*

Open terminal and command

    source cart_env/bin/activate
    cd cart_ws/intregration/

And you can run python scrip

    python3 run_all.py 


## System Architecture

![image](https://media.discordapp.net/attachments/1380408352743362692/1442803069334192219/content.png?ex=6926c291&is=69257111&hm=dc80e48031800150066113254d3816bc1a4a0ae596f54acffbcb07d47869962f&=&format=webp&quality=lossless&width=594&height=891)