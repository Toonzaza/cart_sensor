import websocket

def on_message(ws, message):
    print(f"Received position data: {message}")

def on_error(ws, error):
    print(f"Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print("### Connection closed ###")

def on_open(ws):
    print("Connected to server, waiting for position data...")

if __name__ == "__main__":
    websocket.enableTrace(True)  # Optional, for debug output
    ws = websocket.WebSocketApp(
        "ws://<YOUR_PC_IP>:8080",    # WebSocket server URL (replace with your PC's IP and port)
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()
