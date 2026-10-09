# Hardware integration

## Local radar agent

1. Install project requirements.
2. Run `python hardware/local_agent.py`.
3. The dashboard connects to `ws://127.0.0.1:8765/ws`.
4. Authorized ESP32/Arduino software sends real measurements to `POST http://127.0.0.1:8765/radar`.

Example payload:
```json
{"targets":[{"id":1,"angle":35,"distance":0.42,"strength":0.8}]}
```
An empty `targets` array means **NO SIGNAL**. The agent does not invent targets and does not scan arbitrary LAN hosts.
