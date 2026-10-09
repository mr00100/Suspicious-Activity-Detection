# Radar / Hardware Protocol

A future ESP32/Arduino/local-agent can send JSON records to the dashboard gateway:

```json
{
  "type": "radar",
  "targets": [
    {"id": 1, "distance": 2.1, "angle": 30, "strength": 0.8},
    {"id": 2, "distance": 1.4, "angle": -20, "strength": 0.7}
  ]
}
```

Only received targets should be rendered. If the sensor sends an empty `targets` array, the radar displays NO SIGNAL.

For LAN/Wi-Fi discovery, use a local companion agent with explicit user permission. A browser cannot freely enumerate arbitrary LAN hosts.
