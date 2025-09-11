# import asyncio
# import websockets

# async def echo(websocket):
#     async for message in websocket:
#         print(f"Received: {message}")
#         await websocket.send(f"Echo: {message}")

# async def main():
#     async with websockets.serve(echo, "0.0.0.0", 8765):
#         print("WebSocket server started on ws://0.0.0.0:8765")
#         await asyncio.Future()  # run forever

# # Start the server
# asyncio.run(main())


import asyncio
import websockets
import json

async def handle_client(websocket):
    async for message in websocket:
        try:
            data = json.loads(message)
            
            # Example: handle new order
            if data.get("type") == "new_order":
                print("New Order Received:", data)
                # Send acknowledgment back to browser
                await websocket.send(json.dumps({"status": "received", "type": "new_order"}))
            
            # Example: handle position
            elif data.get("type") == "position":
                print("Position Received:", data["value"])
                # Send a response back
                await websocket.send(json.dumps({"status": "ok", "type": "position", "value": data["value"]}))
        
        except json.JSONDecodeError:
            print("Raw message:", message)
            await websocket.send(json.dumps({"status": "error", "message": "Invalid JSON"}))

async def main():
    async with websockets.serve(handle_client, "0.0.0.0", 8765):
        print("WebSocket server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # run forever

asyncio.run(main())
