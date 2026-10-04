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
bridge/                       Python bridge (rti_ad8x_bridge.py, settings.py) + mqtt_test.py
config.example.yaml           copy to config/config.yaml
deploy/systemd/               systemd unit, if you run it outside Docker
scripts/restart-bridge.sh     restarts the systemd service
docs/                         protocol and setup notes from upstream
examples/upstream-homeassistant/
                              upstream's HA dashboards/automations — reference only;
                              their zone names (Kitchen, Great Room...) are not ours
```

## Configuration

Everything lives in **`config.yaml`**: MQTT, timing, and every amp with its zones and sources.
Start from `config.example.yaml`. Nothing is hardcoded in the Python.

```bash
cp config.example.yaml config/config.yaml   # config/config.yaml is gitignored
```

The bridge looks for the file at `$CONFIG_PATH`, then `/config/config.yaml`, then `./config/config.yaml`.
If it isn't valid, the bridge logs exactly what's wrong and exits.

- **Amps:** list as many as you have. Each needs an `id` (used in topics, so don't change it once set),
  a `host`, and optionally a `port`, which defaults to 23.
- **Zones:** `1: Kitchen` or `1: { name: Kitchen, id: kitchen }`. The `id` pins the Home Assistant entity,
  so you can rename the zone without breaking dashboards. Unlisted zones show up as "Zone N".
- **Sources:** optional labels, for example `sources: { 1: Sonos 1 }`. Unlabelled inputs show as `1`–`8`.
  Raw `.../source` topics stay numeric. The HA select uses `.../source_label`.
- **Orphan cleanup:** with `discovery.cleanup_orphans: true`, the bridge clears retained HA discovery
  entries for its amps that are no longer in the config, such as renamed or removed zones and removed
  amps. Each removal is logged. Nothing outside this bridge's devices is touched.

Environment variables override the file. Use them to keep the password out of `config.yaml`:

| Variable | Overrides |
|---|---|
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS` | `mqtt.*` (no quotes around values) |
| `MQTT_BASE`, `DISCOVERY_PREFIX` | `mqtt.base_topic`, `mqtt.discovery_prefix` |
| `LOG_LEVEL` | `logging.level` |
| `CONFIG_PATH` | location of config.yaml |

## MQTT topics

- State: `rti/ad8x/<amp>/zone/<n>/{power,mute,source,source_label,volume,bass,treble}` (retained)
- Commands: `rti/ad8x/<amp>/zone/<n>/set/<cmd>` (`source` accepts a number or a label), where cmd is power, mute, toggle_mute, source, volume,
  bass, treble, volume_up/down, bass_up/down, or treble_up/down
- All off: publish `OFF` to `rti/ad8x/all/command`
- Raw telnet: publish to `rti/ad8x/<amp>/raw`; the reply lands on `rti/ad8x/<amp>/ack/raw`
- Health: `rti/ad8x/bridge/status`, `rti/ad8x/diagnostics/*`, `rti/ad8x/network_status/<amp>`

Volume on the wire is attenuation (0 = loudest, 75 = quietest). Home Assistant entities invert it,
so 75 is loudest in the UI.
