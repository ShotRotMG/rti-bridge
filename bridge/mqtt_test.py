"""Quick MQTT connectivity check. Uses the same env vars as the bridge."""
import os
import time

import paho.mqtt.client as mqtt

HOST = os.getenv("MQTT_HOST", "127.0.0.1")
PORT = int(os.getenv("MQTT_PORT", "1883"))
USER = os.getenv("MQTT_USER", "")
PASS = os.getenv("MQTT_PASS", "")


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"Connected to MQTT broker as {USER or '(anonymous)'}")
    else:
        print(f"Connection failed: {rc}")


client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
if USER:
    client.username_pw_set(USER, PASS)
client.on_connect = on_connect

print(f"Attempting to connect to MQTT broker at {HOST}:{PORT}...")
client.connect(HOST, PORT, 60)
client.loop_start()
time.sleep(5)
client.loop_stop()
