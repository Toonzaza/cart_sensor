import json, paho.mqtt.client as mqtt

class MqttBus:
    def __init__(self, host="localhost", port=1883, base="smartcart"):
        self.base = base
        self.client = mqtt.Client(client_id="detect_sensor")
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

    def publish_sensor(self, payload: dict):
        topic = f"{self.base}/sensor"
        self.client.publish(topic, json.dumps(payload), qos=1)
        print(f"[PUB] {topic}: {payload}")

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()
