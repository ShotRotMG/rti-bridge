# rti-bridge

Home Assistant bridge for RTI AV hardware, so it can be driven without the RTI XP-8v processor.

| Device | What it is | Transport | Status |
|---|---|---|---|
| **AD-8x** (x2) | 8-zone audio amplifier, 16 zones total | Telnet (port 23) → MQTT discovery | Working |
| **VHD-8x** | 8x10 HDBaseT video matrix | HTTP API → MQTT discovery | Planned (v2.1.0) |

Fork of [srhunt-cyber/RTI-AD8x-Home-Assistant-bridge](https://github.com/srhunt-cyber/RTI-AD8x-Home-Assistant-bridge) (MIT),
with stability fixes: continue-on-zone-failure, poll reconnect, and no credentials in source.

## Layout

```
bridge/                       Python bridge (rti_ad8x_bridge.py, settings.py, web.py, static/) + mqtt_test.py
config.example.yaml           copy to config/config.yaml
Dockerfile, docker-compose.yml container build (see Deploy)
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
  An optional `name` sets the device name in Home Assistant. You can change it at any time;
  entity IDs don't change.
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
| `LOG_LEVEL` | `logging.level` (`DEBUG` shows every telnet command) |
| `WEB_PORT`, `WEB_PASSWORD` | `web.port`, `web.password` |
| `CONFIG_PATH` | location of config.yaml |

## Web UI

The bridge serves a page on **port 8088**, for example `http://10.0.0.103:8088/`:

- **Status:** each amp's online state and last poll, and every zone with power, mute, source, volume,
  bass and treble. It uses the same commands as Home Assistant, plus an "All zones off" button.
- **Config:** a form for MQTT, polling, amps, zones and source labels, or the raw YAML.
  **Save & apply** validates first, writes `config.yaml` (comments are kept, and the previous file is saved
  as `config.yaml.bak`), then restarts the bridge. Passwords are never sent to the browser.
  You can add or remove amps here. Removed amps' entities are cleaned up if `cleanup_orphans` is on.
- **Logs:** the last 1000 log lines, with a filter.

If `config.yaml` is missing or invalid, the bridge doesn't start but the web UI still does, showing the
error so you can fix the config from the browser.

The page is open to anyone on the LAN unless you set `web.password` or the `WEB_PASSWORD` env var.
With a password set, the browser asks for it; any username works. Load is light: the page polls the bridge
every 3s while open and never talks to the amps directly.

## Deploy

### Portainer (recommended)

1. On the Docker host, create the config folder and put your config in it:
   ```bash
   mkdir -p /home/charro/docker/bridges/rti-bridge/config
   # copy config.example.yaml there as config.yaml and edit it
   ```
2. **Stop the old `rti-ad8x-bridge` stack first.** Two bridges polling the same amps will fight
   over the telnet sessions.
3. Portainer → Stacks → Add stack → **Repository**
   - Repository URL: `https://github.com/ShotRotMG/rti-bridge`
   - Authentication: on. Use your GitHub username and a personal access token with read access to this repo.
   - Compose path: `docker-compose.yml`
   - Environment variables:
     - `CONFIG_DIR` = `/home/charro/docker/bridges/rti-bridge/config`
     - `MQTT_PASS` = your broker password, so it can stay out of config.yaml
4. Deploy. To update later, use **Pull and redeploy** on the stack and tick "Re-pull image and redeploy",
   which rebuilds from the latest commit.

### Docker Compose from a clone

```bash
git clone https://github.com/ShotRotMG/rti-bridge && cd rti-bridge
cp config.example.yaml config/config.yaml   # edit it
docker compose up -d --build
docker compose logs -f
```

### systemd (no Docker)

See `deploy/systemd/rti-ad8x-bridge.service` and `scripts/restart-bridge.sh`. Set `CONFIG_PATH` in
`/etc/default/rti-ad8x-bridge` if config.yaml isn't at `/config/config.yaml` or `./config/config.yaml`.

## MQTT topics

- State: `rti/ad8x/<amp>/zone/<n>/{power,mute,source,source_label,volume,bass,treble}` (retained)
- Commands: `rti/ad8x/<amp>/zone/<n>/set/<cmd>` (`source` accepts a number or a label), where cmd is power, mute, toggle_mute, source, volume,
  bass, treble, volume_up/down, bass_up/down, or treble_up/down
- All off: publish `OFF` to `rti/ad8x/all/command`
- Raw telnet: publish to `rti/ad8x/<amp>/raw`; the reply lands on `rti/ad8x/<amp>/ack/raw`
- Health: `rti/ad8x/bridge/status`, `rti/ad8x/diagnostics/*`, `rti/ad8x/network_status/<amp>`

Volume on the wire is attenuation (0 = loudest, 75 = quietest). Home Assistant entities invert it,
so 75 is loudest in the UI.
