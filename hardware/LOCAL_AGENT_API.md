# Local Agent Architecture

Recommended future architecture:

Browser Dashboard <-> localhost companion agent <-> ESP32/Arduino/Radar/IP camera/LAN devices

The agent should expose authenticated localhost endpoints for:
- LAN device discovery
- Serial/USB radar
- Bluetooth adapters not available to Web Bluetooth
- hardware status
- device configuration

Do not expose unrestricted shell execution through the agent.
