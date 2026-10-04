# rti-bridge

Home Assistant bridge for RTI AV hardware, so it can be driven without the RTI XP-8v processor.

| Device | What it is | Transport | Status |
|---|---|---|---|
| **AD-8x** (x2) | 8-zone audio amplifier, 16 zones total | Telnet (port 23) → MQTT discovery | Working |
| **VHD-8x** | 8x10 HDBaseT video matrix | HTTP API → MQTT discovery | Planned |

Fork of [srhunt-cyber/RTI-AD8x-Home-Assistant-bridge](https://github.com/srhunt-cyber/RTI-AD8x-Home-Assistant-bridge) (MIT),
with stability fixes: continue-on-zone-failure, poll reconnect, and no credentials in source.

## Layout

```
bridge/                       Python bridge (rti_ad8x_bridge.py) + mqtt_test.py
deploy/systemd/               systemd unit, if you run it outside Docker
scripts/restart-bridge.sh     restarts the systemd service
docs/                         protocol and setup notes from upstream
examples/upstream-homeassistant/
                              upstream's HA dashboards/automations — reference only;
                              their zone names (Kitchen, Great Room...) are not ours
```

## Configuration

MQTT settings come from environment variables. Copy `.env.example` to `.env` and fill it in.
`.env` is gitignored — never commit credentials.

| Variable | Default | |
|---|---|---|
| `MQTT_HOST` | `127.0.0.1` | broker address |
| `MQTT_PORT` | `1883` | |
| `MQTT_USER` / `MQTT_PASS` | empty | no quotes around values |
| `MQTT_BASE` | `rti/ad8x` | topic prefix |
| `POLL_INTERVAL` | `20` | seconds between full zone polls |
| `LOG_LEVEL` | `INFO` | |

Amp IPs and zone names are still in `AMPS` / `ZONE_NAMES` at the top of `bridge/rti_ad8x_bridge.py`.

## MQTT topics

- State: `rti/ad8x/<amp>/zone/<n>/{power,mute,source,volume,bass,treble}` (retained)
- Commands: `rti/ad8x/<amp>/zone/<n>/set/<cmd>`, where cmd is power, mute, toggle_mute, source, volume,
  bass, treble, volume_up/down, bass_up/down, or treble_up/down
- All off: publish `OFF` to `rti/ad8x/all/command`
- Raw telnet: publish to `rti/ad8x/<amp>/raw`; the reply lands on `rti/ad8x/<amp>/ack/raw`
- Health: `rti/ad8x/bridge/status`, `rti/ad8x/diagnostics/*`, `rti/ad8x/network_status/<amp>`

Volume on the wire is attenuation (0 = loudest, 75 = quietest). Home Assistant entities invert it,
so 75 is loudest in the UI.
