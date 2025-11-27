# SmartCart Backend System
Real-time Sensor Processing · ID Matching · AMR Dispatching · WebSocket API · MQTT Architecture

<div align="center">

![System Architecture](https://media.discordapp.net/attachments/1397044177564336148/1443431488636059748/content.png?ex=69290bd4&is=6927ba54&hm=57bd0b0d69fcb1a4767fc3b53190689db6a3e0b27f40578c866a8ab0bda93958&=&format=webp&quality=lossless&width=735&height=1104)

</div>

---

## 📋 Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Getting Started](#getting-started)
  - [Raspberry Pi Setup](#raspberry-pi-setup)
  - [Running the System](#running-the-system)
- [Configuration](#configuration)
  - [AMR Goals Mapping](#amr-goals-mapping)
  - [AMR Communication Settings](#amr-communication-settings)
- [Testing & Development](#testing--development)
  - [Sending Mock Data](#sending-mock-data)
  - [Cart Sensor Debugging](#cart-sensor-debugging)
- [System Architecture](#system-architecture)
- [Python Scripts Overview](#python-scripts-overview)

---

## Overview

This repository contains the backend system for **SmartCart**, responsible for coordinating:

- 📡 Photoelectric sensors  
- 📊 Barcode scanners  
- 🔖 RFID reader  
- 💡 LED indicators  
- 🌐 WebSocket server  
- 🤖 AMR communication via ARCL  
- 📨 Central MQTT message bus  

The system is designed for **modularity, fault-tolerance, and clean separation of logic**.

---

## Requirements

- Python ≥ 3.9
- MQTT Broker (Mosquitto recommended)
- Python packages: `paho-mqtt`, `websockets`, `gpiozero`, `lgpio`, `pyserial`

---

## Getting Started

### Raspberry Pi Setup

Use PuTTY for remote SSH connection to Raspberry Pi 5.

#### Network Information

**Find MAC Address:**
```bash
ip link
```

**Raspberry Pi Network Details:**
```
Ethernet MAC (eth0):  2c:cf:67:f4:61:fd
Wi-Fi MAC (wlan0):    2c:cf:67:f4:61:fe
Fixed IP (MTH Project WIFI):  192.168.0.50
SSH Port:             22
Username:             fibo
Password:             1234
```

<div align="center">

![Raspberry Pi Connection](https://media.discordapp.net/attachments/1380408352743362692/1442791621107388507/image.png?ex=6926b7e8&is=69256668&hm=fbca3bb642b2a770ea5d37299a0ecec4dba808a2df514f6564012217bf34d6e4&=&format=webp&quality=lossless)

</div>

### Running the System

After establishing an SSH connection, follow these steps:

1. **Activate the virtual environment:**
   ```bash
   source cart_env/bin/activate
   ```

2. **Navigate to the integration directory:**
   ```bash
   cd cart_ws/integration/
   ```

3. **Run the main script:**
   ```bash
   python3 run_all.py
   ```

---

## Configuration

### AMR Goals Mapping

To configure AMR destination goals, edit the `goals_map.json` file:

```bash
cd cart_ws/integration/data/
```

**Format:** `goals_map.json`
```json
{
  "DOT400002": "Goal13",
  "DOT400003": "Goal7",
  "DOT500101": "Dock_A"
}
```

The first column represents the **Taster Name**, and the second column represents the **Goal Name**. Ensure these match your actual deployment configuration.

### AMR Communication Settings

Configure AMR communication parameters in `communicate_AMR.py` located at `cart_ws/integration/`:

#### Goal Names (ARCL related)
```python
PICKUP_GOAL   = os.getenv("PICKUP_GOAL",  "ROEQ_SAF_cart500_entry")  # Pickup (Home)
DROPOFF_GOAL  = os.getenv("DROPOFF_GOAL", "ROEQ_SAF_cart500")        # Dropoff (Home)
```

#### Wait Duration at Goals
```python
WAIT_DURATION = int(os.getenv("WAIT_DURATION", "15"))  # seconds
```

#### Text-to-Speech Message
```python
COUNTDOWN_MSG = os.getenv("COUNTDOWN_MSG", "5 4 3 2 1 0 Good luck")
```

---

## Testing & Development

### Sending Mock Data

If you don't have a server or web application to send data to SmartCart, you can use the mock data script:

```bash
cd cart_ws
python3 send_to_pi.py
```

**Payload Format:** `[OP, CUH1, CUH2, MXK1, MXK2, DOT]`

**Available Mock Payloads:**
```python
payload_1 = ["Request", "CUH22-1043", "CUH22-1044", "MXK20-1003", "MXK20-1004", "DOT400002"]
payload_2 = ["Return",  "None",       "None",       "CUH22-1043", "None",       "DOT400002"]
payload_3 = ["Request", "None",       "None",       "None",       "None",       "DOT400002"]
payload_4 = ["Return",  "1245452",    "None",       "None",       "None",       "DOT400002"]
payload_5 = ["Request", "CUH22-1030", "None",       "MXK22-1049", "None",       "DOT400002"]
payload_6 = ["Request", "None",       "CUH22-1030", "MXK22-1049", "None",       "DOT400002"]

payload = payload_6  # Select the payload to send
```

### Cart Sensor Debugging

The `Cart_sensor` directory contains debugging tools (not used in main process):

- **`test.py`** - Read GPIO pin values
- **`on_gpio.py`** - Set GPIO pins high
- **`barcode_read.py`** - Test barcode scanner
- **`RFID_read.py`** - Test RFID reader

---

## System Architecture

<div align="center">

![System Architecture Diagram](https://media.discordapp.net/attachments/1380408352743362692/1442803069334192219/content.png?ex=6926c291&is=69257111&hm=dc80e48031800150066113254d3816bc1a4a0ae596f54acffbcb07d47869962f&=&format=webp&quality=lossless&width=594&height=891)

</div>

The system follows a modular architecture with MQTT as the central message bus, enabling:
- Decoupled sensor modules
- Real-time data processing
- Fault-tolerant operation
- Scalable AMR integration

---

# Python Scripts Overview

This document explains the purpose of all **10 Python scripts** included in the SmartCart backend system.  
Each script has a clearly defined responsibility and communicates with others through MQTT, WebSocket, GPIO, or Telnet.

---

# 📚 Table of Contents

- [1. drivers_sensor.py](#1-drivers_sensorpy--low-level-hardware-drivers)
- [2. bus_sensor.py](#2-bus_sensorpy--mqtt-bus-wrapper)
- [3. detect_sensor.py](#3-detect_sensorpy--sensor-event-orchestrator)
- [4. main_sensor.py](#4-main_sensorpy--sensor-node-entry-point)
- [5. led_actuator.py](#5-led_actuatorpy--led-control-node)
- [6. fn_server.py](#6-fn_serverpy--shared-backend-utilities)
- [7. main_server.py](#7-main_serverpy--websocket--mqtt-bridge)
- [8. match_id.py](#8-match_idpy--id-matching-engine)
- [9. communicate_AMR.py](#9-communicate_amrpy--amr-telnetarcl-controller)
- [10. run_all.py](#10-run_allpy--main-orchestrator--process-manager)

---

# 1. **drivers_sensor.py** – Low-Level Hardware Drivers

Handles all **hardware communication**:

- Photoelectric sensors (GPIO)
- Barcode scanners (MCR12 via serial)
- RFID reader (ThingMagic Elara via JSON/RCI protocol)

### Responsibilities:
- Open/close serial ports  
- Build MCR12 command frames  
- Enable/disable barcode scanning  
- Read scan lines  
- Configure Elara reader  
- Decode RFID EPC/MB into ASCII  

### Used by:
- `detect_sensor.py`
- `main_sensor.py`

This is the **lowest-level layer** in the system.

---

# 2. **bus_sensor.py** – MQTT Bus Wrapper

Provides a small abstraction layer for publishing sensor events to MQTT.

### Responsibilities:
- Connect to MQTT broker  
- Maintain base topic  
- Publish sensor JSON messages  

### Used by:
- `main_sensor.py`
- `detect_sensor.py`

This bridges **hardware → MQTT bus**.

---

# 3. **detect_sensor.py** – Sensor Event Orchestrator

Handles GPIO events and triggers barcode/RFID reads.

### Responsibilities:
- Watch photoelectric sensor GPIO edges  
- Publish photo state changes  
- Launch background threads to:
  - Scan barcode  
  - Read RFID  
- Publish decoded values to MQTT  

### Used by:
- `main_sensor.py`  
- Provides data for `match_id.py` & `run_all.py`

---

# 4. **main_sensor.py** – Sensor Node Entry Point

The main process running on Raspberry Pi to produce sensor events.

### Responsibilities:
- Initialize MQTT bus  
- Open barcode & RFID serial ports  
- Configure Elara reader  
- Create sensor node (`SensorNode`)  
- Loop until interrupted  
- Clean up on exit  

### Used by:
- Started as subprocess by `run_all.py`

---

# 5. **led_actuator.py** – LED Control Node

Controls green/red LEDs using MQTT commands.

### Responsibilities:
- Subscribe to `smartcart/led/cmd`  
- Set LEDs according to result:
  - **ok** → green ON  
  - **nok** → red ON  
  - **skip/reset** → both OFF  
- Cleanup GPIO on shutdown  

### Used by:
- `match_id.py`  
- `run_all.py`

---

# 6. **fn_server.py** – Shared Backend Utilities

Common helper functions used by several scripts.

### Responsibilities:
- Manage `state.json`, `job_ids.jsonl`, `goals_map.json`  
- Normalize CUH/KIT IDs  
- Map DOT → goal name  
- Publish job data to MQTT  
- Publish detect configuration  
- Parse AMR ARCL lines  
- Safe atomic file writes  

### Used by:
- `main_server.py`
- `run_all.py`
- `communicate_AMR.py` (optional utilities)

---

# 7. **main_server.py** – WebSocket → MQTT Bridge

Entry point for external systems such as **MES, C# client, or WebApp**.

### Responsibilities:
- Accept WebSocket connections  
- Receive job payloads  
- Normalize/validate job data  
- Save to `state.json` and append log  
- Publish:
  - `job/latest`
  - `job/event`
  - Detect configuration  
- Respond success/error to WebSocket clients  

### Used by:
- Upstream systems  
- `run_all.py` & `match_id.py` consume its MQTT output

---

# 8. **match_id.py** – ID Matching Engine

Implements SmartCart’s ID validation logic.

### Responsibilities:
- Subscribe to `smartcart/sensor`  
- Compare scan results with job IDs  
- Maintain match state  
- Publish:
  - LED commands (`smartcart/led/cmd`)  
  - Match summary (`smartcart/match`)  

### Used by:
- `run_all.py` FSM (especially in Request/Return)  
- `communicate_AMR.py` for Return flow waiting logic  

---

# 9. **communicate_AMR.py** – AMR Telnet/ARCL Controller

Handles communication with AMR (Omron LD / ROEQ Module).

### Responsibilities:
- Connect via Telnet  
- Enable ARCL monitors  
- Parse AMR output and publish to MQTT  
- Execute mission sequence:
  - Pickup → Goal → Dropoff → Home  
- Handle Request vs Return logic  
- Listen for:
  - `smartcart/toggle_omron`
  - `smartcart/match`  

### Used by:
- Triggered by FSM in `run_all.py`  

---

# 10. **run_all.py** – Main Orchestrator & Process Manager

The **brain** of the entire SmartCart backend.

### Responsibilities:

#### 1. Startup
- Reset job & FSM state  
- Reset LEDs  
- Clear `job/latest` retained message  

#### 2. Launch Processes
Starts the following subprocesses:
- `led_actuator.py`
- `main_server.py`
- `match_id.py`
- `communicate_AMR.py`
- `main_sensor.py`

#### 3. FSM (Finite State Machine)
- Listen to MQTT topics:
  - `job/latest`
  - `match`
  - `sensor`
  - `amr/status`
  - `amr/connected`
- Manage job queue  
- Handle Request/Return workflows  
- Trigger AMR missions via `smartcart/toggle_omron`  
- Detect arrival events and complete jobs  

#### 4. Monitoring
- Watches child process health  
- Logs status continuously  

---

# 🔗 System Flow Summary

1. **External system → `main_server.py` (WebSocket)**  
   → publishes job to MQTT and updates state.

2. **Sensors → `main_sensor.py` → MQTT**  
   → consumed by `match_id.py` and FSM.

3. **ID Matching → `match_id.py`**  
   → updates LED and sends match summary.

4. **Orchestration → `run_all.py`**  
   → decides when to dispatch AMR.

5. **AMR Actions → `communicate_AMR.py`**  
   → executes missions and reports back.

6. **Shared Utilities → `fn_server.py`**  
   → supports everything with consistent logic.

---

## 🤝 Contributing

Mr. Saharat Masamran

Mr. Natpurichakorn Chongsukphiphat

## 📧 Contact

poppeth000@gmail.com