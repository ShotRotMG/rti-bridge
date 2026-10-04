#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RTI AD-8x <-> MQTT bridge
Version 1.8.2
  1.8.1 + stability fixes (Dec 2025) + continue-on-zone-failure
  + poll-reconnect fix (Jun 2026)
  1.8.2: MQTT settings from environment, no credentials in source or logs
  1.9.0: everything from config.yaml (any number of amps), optional source labels,
         pinned zone ids, orphaned discovery cleanup
  1.9.1: Docker image (dependencies baked in at build time)
  1.9.2: compose pull_policy: build so redeploys pick up new commits
  1.9.3: telnet TX logged at DEBUG; one INFO line per incoming command; optional amp names
"""
import os
import sys
import time
import json
import signal
import socket
import logging
import traceback
import threading
from typing import Optional, Tuple

import paho.mqtt.client as mqtt
import psutil  # REQUIRED FOR METRICS

import settings as settings_mod
from settings import ConfigError, Settings, slugify, ZONES_PER_AMP


# LOGGING
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rti_ad8x_bridge")
__version__ = "1.9.3"


# CONFIG - populated from config.yaml by apply_settings() at startup
SETTINGS: Optional[Settings] = None
MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS = "127.0.0.1", 1883, "", ""
MQTT_BASE, DISCOVERY_PREFIX = "rti/ad8x", "homeassistant"
POLL_INTERVAL_SEC = 20.0
CONNECT_TIMEOUT = 6.0
PER_CMD_TIMEOUT = 5.0
POST_SEND_SETTLE = 0.1
INTER_CMD_SLEEP = 0.08
SET_RETRIES = 2
RETRY_SLEEP = 0.2
DUMP_RAW_CHUNKS = True
VOL_COALESCE_SEC = 1.2
VOL_ECHO_SUPPRESS_SEC = 1.0
HEALTH_CHECK_INTERVAL = 30.0
ORPHAN_SCAN_SEC = 5.0  # how long to collect retained discovery configs before cleanup


def apply_settings(cfg: Settings):
    global SETTINGS, MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, MQTT_BASE, DISCOVERY_PREFIX
    global POLL_INTERVAL_SEC, CONNECT_TIMEOUT, PER_CMD_TIMEOUT, POST_SEND_SETTLE, INTER_CMD_SLEEP
    global SET_RETRIES, RETRY_SLEEP, DUMP_RAW_CHUNKS, VOL_COALESCE_SEC, VOL_ECHO_SUPPRESS_SEC
    global HEALTH_CHECK_INTERVAL
    SETTINGS = cfg
    m, t = cfg.mqtt, cfg.timing
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS = m["host"], m["port"], m["user"], m["password"]
    MQTT_BASE, DISCOVERY_PREFIX = m["base_topic"], m["discovery_prefix"]
    POLL_INTERVAL_SEC = t["poll_interval_sec"]
    CONNECT_TIMEOUT = t["connect_timeout_sec"]
    PER_CMD_TIMEOUT = t["per_cmd_timeout_sec"]
    POST_SEND_SETTLE = t["post_send_settle_sec"]
    INTER_CMD_SLEEP = t["inter_cmd_sleep_sec"]
    SET_RETRIES = t["set_retries"]
    RETRY_SLEEP = t["retry_sleep_sec"]
    DUMP_RAW_CHUNKS = t["dump_raw_chunks"]
    VOL_COALESCE_SEC = t["vol_coalesce_sec"]
    VOL_ECHO_SUPPRESS_SEC = t["vol_echo_suppress_sec"]
    HEALTH_CHECK_INTERVAL = t["health_check_interval_sec"]
    logging.getLogger().setLevel(getattr(logging, str(cfg.logging["level"]).upper(), logging.INFO))


EOL, ESC2 = b"\r", b"\x1b" + b"2"

# Connection-fatal socket errors: these mean "the link is dead, reconnect",
# as opposed to a transient single-zone hiccup which we just skip.
_FATAL_SOCK_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def zz(n: int) -> str:
    return f"{n:02d}"


def parse_sta(line: str):
    try:
        if not line or not line.startswith("#") or line == "#?":
            return None
        parts = line[1:].split(",")
        if len(parts) != 5:
            return None
        z, p, m, ss, nvv = [s.strip() for s in parts]
        return {
            "zone": int(z),
            "power": p == "1",
            "mute": m == "1",
            "source": int(ss),
            "vol_0_75": abs(int(nvv)),
        }
    except Exception:
        return None


def parse_tone(line: str):
    try:
        if not line or not line.startswith("$") or line == "$?":
            return None
        parts = line[1:].split(",")
        if len(parts) != 3:
            return None
        z, b, t = [s.strip() for s in parts]
        return {"zone": int(z), "bass": int(b), "treble": int(t)}
    except Exception:
        return None


def _encode_tone(level: int) -> str:
    lvl = max(-12, min(12, int(level)))
    if lvl % 2 != 0:
        lvl = lvl - 1 if lvl > 0 else lvl + 1
    return f"{lvl:02d}" if lvl >= 0 else f"{abs(lvl) + 20:02d}"


def discovery_topic(component: str, object_id: str) -> str:
    return f"{DISCOVERY_PREFIX}/{component}/{object_id}/config"


def device_block(amp_key: str) -> dict:
    return {
        "identifiers": [f"ad8x_{amp_key}"],
        "manufacturer": "RTI",
        "model": "AD-8x",
        "name": SETTINGS.amp(amp_key).device_name,
    }


def zone_object_id(amp_key: str, zone: int, suffix: str) -> str:
    # Same shape as the original bridge, but keyed on the zone's pinned id
    # instead of its display name, so renames don't create new entities.
    zid = SETTINGS.amp(amp_key).zones[zone].id
    return slugify(f"ad8x_{amp_key}_{zid}_{suffix}")


class AmpSession(threading.Thread):
    def __init__(self, amp_name: str, addr: Tuple[str, int], mqttc: mqtt.Client):
        super().__init__(daemon=True)
        self.amp_name = amp_name
        self.addr = addr
        self.mqttc = mqttc
        self.sock: Optional[socket.socket] = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.connected = False
        self._rbuf = b""
        self._zone_states: dict[int, dict] = {}
        self._consecutive_failures = 0
        self._is_down_published = False

    def _cleanup_socket(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None
            self._rbuf = b""

    def _connect(self) -> bool:
        for attempt in range(3):
            self._cleanup_socket()
            try:
                log.info(f"[{self.amp_name}] connecting (attempt {attempt+1}/3)...")
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT)
                s.connect(self.addr)
                s.sendall(ESC2 + EOL)
                time.sleep(POST_SEND_SETTLE)
                s.settimeout(PER_CMD_TIMEOUT)
                self.sock = s
                self.connected = True
                self._rbuf = b""
                self._pub_availability("online")
                log.info(f"[{self.amp_name}] connected")
                return True
            except Exception as e:
                log.warning(f"[{self.amp_name}] connect failed: {e}")
                time.sleep(1.0)
        self.connected = False
        return False

    def _close(self):
        was = self.connected
        self.connected = False
        self._consecutive_failures = 0
        self._is_down_published = False
        self._cleanup_socket()
        if was:
            self._pub_availability("offline")
            log.info(f"[{self.amp_name}] closed")

    def _readline(self, timeout_s: float) -> str:
        end = time.time() + timeout_s
        s = self.sock
        if not s:
            return ""

        def pop_line_from_buffer():
            if not self._rbuf:
                return None
            for sep in (b"\r", b"\n"):
                if sep in self._rbuf:
                    line, _, rest = self._rbuf.partition(sep)
                    self._rbuf = rest.lstrip(b"\r\n")
                    return line.decode(errors="ignore").strip()
            return None

        line = pop_line_from_buffer()
        if line is not None:
            return line

        while time.time() < end:
            try:
                chunk = s.recv(1024)
                if not chunk:
                    break
                if DUMP_RAW_CHUNKS and log.isEnabledFor(logging.DEBUG):
                    log.debug(f"[{self.amp_name}] RXCHUNK {len(chunk)}B: {chunk.hex(' ')}")
                self._rbuf += chunk
                line = pop_line_from_buffer()
                if line is not None:
                    return line
            except socket.timeout:
                pass
            except Exception:
                break
        return ""

    def _read_reply(self, expected_prefix: str, timeout_s: float) -> str:
        end = time.time() + timeout_s
        while time.time() < end:
            remaining = max(0.05, end - time.time())
            line = self._readline(remaining)
            if not line or line == "#?":
                continue
            if line.startswith(expected_prefix):
                return line
        return ""

    def _send_ascii(self, cmd_ascii: str):
        if not self.sock:
            raise RuntimeError("no socket")
        cmd_ascii = cmd_ascii.strip().upper()
        self.sock.sendall(cmd_ascii.encode("ascii", "ignore") + EOL)
        log.debug(f"[{self.amp_name}] TX {cmd_ascii}")

    def _send_only(self, cmd_ascii: str) -> bool:
        with self.lock:
            if not self.connected and not self._connect():
                return False
            try:
                self._send_ascii(cmd_ascii)
                time.sleep(POST_SEND_SETTLE)
                return True
            except Exception as e:
                log.error(f"[{self.amp_name}] send_only error: {e}")
                self._close()
                return False

    def _topic(self, *parts) -> str:
        return "/".join([MQTT_BASE, self.amp_name, *[str(p) for p in parts]])

    def _pub_availability(self, state: str):
        self.mqttc.publish(self._topic("status"), state, retain=True)

    def _pub_zone_full(self, z: int, sta_data: dict, tone_data: dict):
        base = self._topic("zone", z)
        self.mqttc.publish(f"{base}/power", "on" if sta_data["power"] else "off", retain=True)
        self.mqttc.publish(f"{base}/mute", "on" if sta_data["mute"] else "off", retain=True)
        self.mqttc.publish(f"{base}/source", str(sta_data["source"]), retain=True)
        amp_cfg = SETTINGS.amp(self.amp_name)
        self.mqttc.publish(f"{base}/source_label", amp_cfg.source_label(sta_data["source"]), retain=True)
        self.mqttc.publish(f"{base}/bass", str(tone_data["bass"]), retain=True)
        self.mqttc.publish(f"{base}/treble", str(tone_data["treble"]), retain=True)

        buf = self._zone_states.setdefault(z, {})
        if time.time() >= buf.get("suppress_until", 0.0):
            vv = sta_data["vol_0_75"]
            if buf.get("last_published_vol") != vv:
                self.mqttc.publish(f"{base}/volume", str(vv), retain=True)
                buf["last_published_vol"] = vv

        combined = {**sta_data, **tone_data}
        self.mqttc.publish(base, json.dumps(combined, separators=(",", ":")), retain=True)
        buf.update(combined)

    def _pub_volume_only(self, zone: int, v: int):
        buf = self._zone_states.setdefault(zone, {})
        if buf.get("last_published_vol") != v:
            self.mqttc.publish(self._topic("zone", zone, "volume"), str(v), retain=True)
            buf["last_published_vol"] = v

    def _is_zone_on(self, zone: int) -> bool:
        return self._zone_states.get(zone, {}).get("power", False)

    def set_power(self, zone: int, on: bool) -> bool:
        return self._send_and_confirm(zone, f"*ZN{zz(zone)}PWR{'01' if on else '00'}")

    def set_mute(self, zone: int, on: bool) -> bool:
        return self._send_and_confirm(zone, f"*ZN{zz(zone)}MUT{'01' if on else '00'}")

    def toggle_mute(self, zone: int) -> bool:
        return self._send_and_confirm(zone, f"*ZN{zz(zone)}MUT02")

    def all_zones_off_optimistic(self) -> bool:
        return self._send_only("*ZALLPWR00")

    def set_source(self, zone: int, source: int) -> bool:
        if not self._is_zone_on(zone):
            log.warning(f"[{self.amp_name}] Ignoring source change for zone {zone}; power is off.")
            return False
        return self._send_and_confirm(zone, f"*ZN{zz(zone)}SRC{zz(source)}")

    def volume_up(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            log.warning(f"[{self.amp_name}] Ignoring volume up for zone {zone}; power is off.")
            return False
        return self._send_and_confirm(zone, f"*ZN{zz(zone)}VOLUP")

    def volume_down(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            log.warning(f"[{self.amp_name}] Ignoring volume down for zone {zone}; power is off.")
            return False
        return self._send_and_confirm(zone, f"*ZN{zz(zone)}VOLDN")

    def bass_up(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        cur = self._zone_states.get(zone, {}).get("bass", 0)
        return self.set_bass(zone, min(12, cur + 2))

    def bass_down(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        cur = self._zone_states.get(zone, {}).get("bass", 0)
        return self.set_bass(zone, max(-12, cur - 2))

    def treble_up(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        cur = self._zone_states.get(zone, {}).get("treble", 0)
        return self.set_treble(zone, min(12, cur + 2))

    def treble_down(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        cur = self._zone_states.get(zone, {}).get("treble", 0)
        return self.set_treble(zone, max(-12, cur - 2))

    def set_volume(self, zone: int, v: int) -> bool:
        v_clamped = max(0, min(75, int(v)))
        buf = self._zone_states.setdefault(zone, {})
        buf["target_vol"] = v_clamped
        t = buf.get("vol_timer")
        if t and t.is_alive():
            t.cancel()
        t = threading.Timer(VOL_COALESCE_SEC, self._flush_volume, args=(zone,))
        buf["vol_timer"] = t
        t.start()
        return True

    def _flush_volume(self, zone: int):
        buf = self._zone_states.get(zone, {})
        target = buf.get("target_vol")
        if target is None:
            return
        cmd = f"*ZN{zz(zone)}VOL{zz(target)}"
        log.info(f"[{self.amp_name}] Coalesced VOL zone {zz(zone)} -> {target}")
        self._pub_volume_only(zone, target)
        buf["suppress_until"] = time.time() + VOL_ECHO_SUPPRESS_SEC
        ok = self._send_and_confirm(zone, cmd)
        if not ok:
            log.warning(f"[{self.amp_name}] Coalesced volume SET failed. Re-querying.")
            self._send_and_confirm(zone, f"*ZN{zz(zone)}STA00")

    def set_bass(self, zone: int, level: int) -> bool:
        if not self._is_zone_on(zone):
            log.warning(f"[{self.amp_name}] Ignoring bass change for zone {zone}; power is off.")
            return False
        level_clamped = max(-12, min(12, int(level)))
        buf = self._zone_states.setdefault(zone, {})
        buf["target_bass"] = level_clamped
        t = buf.get("bass_timer")
        if t and t.is_alive():
            t.cancel()
        t = threading.Timer(VOL_COALESCE_SEC, self._flush_bass, args=(zone,))
        buf["bass_timer"] = t
        t.start()
        return True

    def _flush_bass(self, zone: int):
        if not self._is_zone_on(zone):
            return
        buf = self._zone_states.get(zone, {})
        target = buf.get("target_bass")
        if target is None:
            return
        cmd = f"*ZN{zz(zone)}BAS{_encode_tone(target)}"
        log.info(f"[{self.amp_name}] Coalesced BASS zone {zz(zone)} -> {target}")
        ok = self._send_and_confirm(zone, cmd)
        if not ok:
            log.warning(f"[{self.amp_name}] Coalesced bass SET failed. Re-querying.")
            self._send_and_confirm(zone, f"*ZN{zz(zone)}STA00")

    def set_treble(self, zone: int, level: int) -> bool:
        if not self._is_zone_on(zone):
            log.warning(f"[{self.amp_name}] Ignoring treble change for zone {zone}; power is off.")
            return False
        level_clamped = max(-12, min(12, int(level)))
        buf = self._zone_states.setdefault(zone, {})
        buf["target_treble"] = level_clamped
        t = buf.get("treble_timer")
        if t and t.is_alive():
            t.cancel()
        t = threading.Timer(VOL_COALESCE_SEC, self._flush_treble, args=(zone,))
        buf["treble_timer"] = t
        t.start()
        return True

    def _flush_treble(self, zone: int):
        if not self._is_zone_on(zone):
            return
        buf = self._zone_states.get(zone, {})
        target = buf.get("target_treble")
        if target is None:
            return
        cmd = f"*ZN{zz(zone)}TRB{_encode_tone(target)}"
        log.info(f"[{self.amp_name}] Coalesced TREBLE zone {zz(zone)} -> {target}")
        ok = self._send_and_confirm(zone, cmd)
        if not ok:
            log.warning(f"[{self.amp_name}] Coalesced treble SET failed. Re-querying.")
            self._send_and_confirm(zone, f"*ZN{zz(zone)}STA00")

    def _send_and_confirm(self, zone: int, cmd_ascii: str) -> bool:
        with self.lock:
            if not self.connected and not self._connect():
                return False
            tries = 0
            while tries <= SET_RETRIES:
                tries += 1
                try:
                    self._send_ascii(cmd_ascii)
                    time.sleep(POST_SEND_SETTLE)
                    self._send_ascii(f"*ZN{zz(zone)}STA00")
                    time.sleep(POST_SEND_SETTLE)
                    sta_line = self._read_reply(f"#{zz(zone)},", PER_CMD_TIMEOUT)
                    self._send_ascii(f"*ZN{zz(zone)}SET00")
                    time.sleep(POST_SEND_SETTLE)
                    tone_line = self._read_reply(f"${zz(zone)},", PER_CMD_TIMEOUT)
                    sta_data, tone_data = parse_sta(sta_line), parse_tone(tone_line)
                    if sta_data and tone_data:
                        self._pub_zone_full(zone, sta_data, tone_data)
                        return True
                    log.warning(f"[{self.amp_name}] Failed to confirm {cmd_ascii} for zone {zone}")
                    if tries <= SET_RETRIES:
                        time.sleep(RETRY_SLEEP)
                        continue
                except Exception as e:
                    log.error(f"[{self.amp_name}] command error: {e}")
                    self._close()
                    if tries <= SET_RETRIES:
                        if self._connect():
                            continue
            return False

    def _keep_alive(self):
        if self.connected:
            try:
                self._send_ascii("*ZN01STA00")
                time.sleep(POST_SEND_SETTLE)
                self._read_reply("#01,", 1.0)
            except _FATAL_SOCK_ERRORS as e:
                # link died between polls; drop it so the next poll reconnects
                log.warning(f"[{self.amp_name}] keep-alive fatal socket error: {e} - reconnecting")
                self._cleanup_socket()
                self._handle_poll_failure()
            except Exception:
                pass

    def _poll_once(self):
        with self.lock:
            if not self.connected and not self._connect():
                self._handle_poll_failure()
                return False
            success_count = 0
            try:
                for z in range(1, ZONES_PER_AMP + 1):
                    if self.stop_flag.is_set():
                        return False
                    try:
                        self._send_ascii(f"*ZN{zz(z)}STA00")
                        time.sleep(INTER_CMD_SLEEP)
                        sta_line = self._read_reply(f"#{zz(z)},", PER_CMD_TIMEOUT)
                        self._send_ascii(f"*ZN{zz(z)}SET00")
                        time.sleep(INTER_CMD_SLEEP)
                        tone_line = self._read_reply(f"${zz(z)},", PER_CMD_TIMEOUT)
                        sta_data, tone_data = parse_sta(sta_line), parse_tone(tone_line)
                        if sta_data and tone_data:
                            self._pub_zone_full(z, sta_data, tone_data)
                            success_count += 1
                        else:
                            log.warning(f"[{self.amp_name}] partial poll fail zone {zz(z)} - skipping")
                    except _FATAL_SOCK_ERRORS as e:
                        # socket is dead - stop hammering the other zones and force a reconnect
                        log.warning(f"[{self.amp_name}] fatal socket error on zone {zz(z)}: {e} - reconnecting")
                        self._cleanup_socket()      # drop the dead fd WITHOUT resetting failure counters
                        self._handle_poll_failure()
                        return False
                    except Exception as e:
                        # transient single-zone issue (parse hiccup, etc.) - skip just this zone
                        log.warning(f"[{self.amp_name}] exception polling zone {zz(z)}: {e} - skipping")

                if success_count == 0:
                    # every zone failed but nothing bubbled up - treat the whole poll as failed
                    self._handle_poll_failure()
                    return False

                self._handle_poll_success()
                return True
            except Exception as e:
                log.warning(f"[{self.amp_name}] major poll error: {e}")
                self._handle_poll_failure()
                return False

    def _handle_poll_success(self):
        if self._consecutive_failures > 0:
            log.info(f"[{self.amp_name}] Amp communication restored.")
        self._consecutive_failures = 0
        if self._is_down_published:
            self._is_down_published = False

    def _handle_poll_failure(self):
        self._consecutive_failures += 1
        log.warning(f"[{self.amp_name}] Poll failed. Consecutive failures: {self._consecutive_failures}")
        self.connected = False
        if self._consecutive_failures >= 3 and not self._is_down_published:
            log.error(f"[{self.amp_name}] Exceeded poll failure threshold. Publishing 'down' message.")
            down_topic = f"{MQTT_BASE}/network_status/{self.amp_name}"
            self.mqttc.publish(down_topic, "down", retain=True)
            self._is_down_published = True

    def run(self):
        while not self.stop_flag.is_set():
            if self._poll_once():
                half_interval = POLL_INTERVAL_SEC / 2
                time.sleep(half_interval)
                self._keep_alive()
                time.sleep(half_interval)
            else:
                delay = min(10.0, 2.0 + self._consecutive_failures)
                log.warning(f"[{self.amp_name}] Poll failed, retrying in {delay:.1f}s...")
                time.sleep(delay)

    def stop(self):
        self.stop_flag.set()
        self._close()


class Bridge:
    def __init__(self):
        self.client = mqtt.Client(protocol=mqtt.MQTTv5, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.will_set(f"{MQTT_BASE}/bridge/status", "offline", retain=True)
        self.sessions = {}
        self._start_time = time.monotonic()
        self._pid = os.getpid()
        self._process = psutil.Process(self._pid)
        self._last_diag_pub_time = time.monotonic()
        self._published_disc: set = set()
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._scan_seen: set = set()

    def _pub_disc(self, component: str, object_id: str, cfg: dict):
        t = discovery_topic(component, object_id)
        self._published_disc.add(t)
        self.client.publish(t, json.dumps(cfg), retain=True)

    def publish_discovery(self):
        self._published_disc = set()
        for amp in SETTINGS.amps:
            amp_key = amp.id
            avail_t = self._topic(amp_key, "status")
            dev = device_block(amp_key)
            for z in range(1, ZONES_PER_AMP + 1):
                zname = amp.zones[z].name
                base = self._topic(amp_key, "zone", z)
                cmd_base = f"{base}/set"

                power_cfg = {
                    "name": f"{zname} Power",
                    "uniq_id": zone_object_id(amp_key, z, "power"),
                    "stat_t": f"{base}/power",
                    "cmd_t": f"{cmd_base}/power",
                    "pl_on": "on",
                    "pl_off": "off",
                    "stat_on": "on",
                    "stat_off": "off",
                    "avty_t": avail_t,
                    "device": dev,
                    "optimistic": True,
                }
                self._pub_disc("switch", zone_object_id(amp_key, z, "power"), power_cfg)

                mute_cfg = {
                    "name": f"{zname} Mute",
                    "uniq_id": zone_object_id(amp_key, z, "mute"),
                    "stat_t": f"{base}/mute",
                    "cmd_t": f"{cmd_base}/mute",
                    "pl_on": "on",
                    "pl_off": "off",
                    "stat_on": "on",
                    "stat_off": "off",
                    "avty_t": avail_t,
                    "device": dev,
                    "optimistic": True,
                }
                self._pub_disc("switch", zone_object_id(amp_key, z, "mute"), mute_cfg)

                vol_cfg = {
                    "name": f"{zname} Volume",
                    "uniq_id": zone_object_id(amp_key, z, "volume"),
                    "stat_t": f"{base}/volume",
                    "cmd_t": f"{cmd_base}/volume",
                    "min": 0,
                    "max": 75,
                    "mode": "slider",
                    "avty_t": avail_t,
                    "device": dev,
                    "val_tpl": "{{ 75 - (value | int) }}",
                    "cmd_tpl": "{{ 75 - (value | int) }}",
                    "optimistic": True,
                }
                self._pub_disc("number", zone_object_id(amp_key, z, "volume"), vol_cfg)

                source_cfg = {
                    "name": f"{zname} Source",
                    "uniq_id": zone_object_id(amp_key, z, "source"),
                    "stat_t": f"{base}/source_label",
                    "cmd_t": f"{cmd_base}/source",
                    "options": amp.source_options,
                    "avty_t": avail_t,
                    "device": dev,
                }
                self._pub_disc("select", zone_object_id(amp_key, z, "source"), source_cfg)

                bass_cfg = {
                    "name": f"{zname} Bass",
                    "uniq_id": zone_object_id(amp_key, z, "bass"),
                    "stat_t": f"{base}/bass",
                    "cmd_t": f"{cmd_base}/bass",
                    "min": -12,
                    "max": 12,
                    "step": 2,
                    "mode": "slider",
                    "avty_t": avail_t,
                    "device": dev,
                    "icon": "mdi:speaker",
                    "optimistic": True,
                }
                self._pub_disc("number", zone_object_id(amp_key, z, "bass"), bass_cfg)

                treble_cfg = {
                    "name": f"{zname} Treble",
                    "uniq_id": zone_object_id(amp_key, z, "treble"),
                    "stat_t": f"{base}/treble",
                    "cmd_t": f"{cmd_base}/treble",
                    "min": -12,
                    "max": 12,
                    "step": 2,
                    "mode": "slider",
                    "avty_t": avail_t,
                    "device": dev,
                    "icon": "mdi:surround-sound",
                    "optimistic": True,
                }
                self._pub_disc("number", zone_object_id(amp_key, z, "treble"), treble_cfg)

    # ---- orphaned discovery cleanup -------------------------------------
    def _start_orphan_scan(self):
        if not SETTINGS.discovery.get("cleanup_orphans"):
            return
        with self._scan_lock:
            if self._scanning:
                return
            self._scanning = True
            self._scan_seen = set()
        self.client.subscribe(f"{DISCOVERY_PREFIX}/+/+/config")
        log.info(f"[Bridge] Scanning retained discovery configs for orphans ({ORPHAN_SCAN_SEC:.0f}s)...")
        threading.Timer(ORPHAN_SCAN_SEC, self._finish_orphan_scan).start()

    def _note_discovery_config(self, topic: str, payload: str):
        if not payload:
            return
        try:
            cfg = json.loads(payload)
        except ValueError:
            return
        dev = cfg.get("device") or cfg.get("dev") or {}
        ids = dev.get("identifiers") or dev.get("ids") or []
        if isinstance(ids, str):
            ids = [ids]
        if any(str(i).startswith("ad8x_") for i in ids):
            with self._scan_lock:
                self._scan_seen.add(topic)

    def _finish_orphan_scan(self):
        self.client.unsubscribe(f"{DISCOVERY_PREFIX}/+/+/config")
        with self._scan_lock:
            self._scanning = False
            orphans = sorted(self._scan_seen - self._published_disc)
        for t in orphans:
            log.info(f"[Bridge] Removing orphaned discovery entry {t}")
            self.client.publish(t, b"", retain=True)
        log.info(f"[Bridge] Orphan scan done: removed {len(orphans)} entr{'y' if len(orphans) == 1 else 'ies'}")

    def publish_diagnostics(self):
        if not self.client.is_connected():
            return
        try:
            cpu_pct = self._process.cpu_percent(interval=None)
            mem_info = self._process.memory_info()
            mem_mb = round(mem_info.rss / (1024 * 1024), 2)
            self.client.publish(self._topic("diagnostics", "cpu_usage_pct"), str(cpu_pct), retain=False)
            self.client.publish(self._topic("diagnostics", "memory_usage_mb"), str(mem_mb), retain=False)
        except Exception as e:
            log.warning(f"[Bridge] Failed to gather process metrics: {e}")

        uptime_s = int(time.monotonic() - self._start_time)
        self.client.publish(self._topic("diagnostics", "uptime_s"), str(uptime_s), retain=True)

        amp_statuses = {name: "online" if s.connected else "offline" for name, s in self.sessions.items()}
        self.client.publish(self._topic("diagnostics", "amp_connection_status"), json.dumps(amp_statuses), retain=True)

        entity_count = len(self._published_disc)
        self.client.publish(self._topic("diagnostics", "entity_count"), str(entity_count), retain=True)
        self.client.publish(self._topic("bridge", "status"), "online", retain=True)

    def _topic(self, *parts) -> str:
        return "/".join([MQTT_BASE, *[str(p) for p in parts]])

    def start(self):
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()

        try:
            self._process.cpu_percent(interval=0.1)
        except Exception as e:
            log.warning(f"[Bridge] Initial psutil call failed: {e}")

        for amp in SETTINGS.amps:
            name, addr = amp.id, (amp.host, amp.port)
            s = AmpSession(name, addr, self.client)
            self.sessions[name] = s
            s.start()
            log.info(f"Started AmpSession {name} -> {addr[0]}:{addr[1]}")

    def stop(self):
        for s in self.sessions.values():
            s.stop()
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            client.subscribe(f"{self._topic('+', 'zone', '+', 'set', '+')}")
            client.subscribe(f"{self._topic('+', 'raw')}")
            client.subscribe(f"{self._topic('all', 'command')}")
            client.subscribe(f"{DISCOVERY_PREFIX}/status")
            client.publish(self._topic("bridge", "status"), "online", retain=True)
            self.publish_discovery()
            self._start_orphan_scan()
            log.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
        else:
            log.error(f"MQTT connect failed code: {rc}")

    def on_message(self, client, userdata, msg):
        try:
            payload = (msg.payload.decode() if msg.payload else "").strip()
            topic = msg.topic

            if topic.startswith(DISCOVERY_PREFIX + "/") and topic.endswith("/config"):
                self._note_discovery_config(topic, payload)
                return

            if topic == f"{DISCOVERY_PREFIX}/status":
                if payload == "online":
                    self.publish_discovery()
                return

            if not topic.startswith(MQTT_BASE + "/"):
                return
            # parts relative to the base topic: <amp>/zone/<n>/set/<cmd>, <amp>/raw, all/command
            parts = topic[len(MQTT_BASE) + 1:].split("/")

            if parts == ["all", "command"]:
                log.info(f"Received master command: {payload}")
                if payload.upper() == "OFF":
                    for s in self.sessions.values():
                        s.all_zones_off_optimistic()
                    for amp_key, s in self.sessions.items():
                        for z in range(1, ZONES_PER_AMP + 1):
                            base_t = s._topic("zone", z)
                            client.publish(f"{base_t}/power", "off", retain=True)
                            client.publish(f"{base_t}/mute", "off", retain=True)
                    log.info("Sent ALL OFF command and optimistically set all zones to OFF")
                return

            if len(parts) == 2 and parts[1] == "raw":
                sess = self.sessions.get(parts[0])
                if sess:
                    with sess.lock:
                        if not sess.connected and not sess._connect():
                            return
                        log.info(f"[{parts[0]}] raw <- '{payload}'")
                        sess._send_ascii(payload)
                        time.sleep(POST_SEND_SETTLE)
                        line = sess._readline(PER_CMD_TIMEOUT)
                        client.publish(self._topic(parts[0], "ack", "raw"), line or "", retain=False)
                return

            if len(parts) != 5 or parts[1] != "zone" or parts[3] != "set":
                return
            amp, zone_str, cmd = parts[0], parts[2], parts[4].lower()
            log.info(f"[{amp}] zone {zone_str} {cmd} <- '{payload}'")
            try:
                zone = int(zone_str)
            except ValueError:
                return
            sess = self.sessions.get(amp)
            if not sess:
                return

            ok = False
            if cmd == "power":
                is_on = payload.lower() in ("1", "on", "true")
                if is_on:
                    last_vol = sess._zone_states.get(zone, {}).get("vol_0_75", 65)
                    log.info(f"[{amp}] Power ON for zone {zone} received. Setting volume to {last_vol} to power on.")
                    ok = sess.set_volume(zone, last_vol)
                else:
                    ok = sess.set_power(zone, False)
            elif cmd == "mute":
                ok = sess.set_mute(zone, payload.lower() in ("1", "on", "true"))
            elif cmd == "toggle_mute":
                ok = sess.toggle_mute(zone)
            elif cmd == "source":
                src = SETTINGS.amp(amp).source_number(payload)
                if src is None:
                    log.warning(f"[{amp}] Unknown source '{payload}' for zone {zone}")
                else:
                    ok = sess.set_source(zone, src)
            elif cmd == "volume":
                ok = sess.set_volume(zone, int(payload))
            elif cmd == "bass":
                ok = sess.set_bass(zone, int(payload))
            elif cmd == "treble":
                ok = sess.set_treble(zone, int(payload))
            elif cmd == "volume_up":
                ok = sess.volume_up(zone)
            elif cmd == "volume_down":
                ok = sess.volume_down(zone)
            elif cmd == "bass_up":
                ok = sess.bass_up(zone)
            elif cmd == "bass_down":
                ok = sess.bass_down(zone)
            elif cmd == "treble_up":
                ok = sess.treble_up(zone)
            elif cmd == "treble_down":
                ok = sess.treble_down(zone)

            client.publish(self._topic(amp, "zone", "ack", cmd), "ok" if ok else "err", retain=False)

        except Exception:
            traceback.print_exc()


def main():
    try:
        cfg = settings_mod.load()
    except ConfigError as e:
        log.error(f"Config error: {e}")
        sys.exit(2)
    apply_settings(cfg)
    log.info(f"RTI bridge {__version__} starting - config {cfg.path}, "
             f"{len(cfg.amps)} amp(s): {', '.join(a.id + '@' + a.host for a in cfg.amps) or 'none'}")
    log.info(f"MQTT broker {MQTT_HOST}:{MQTT_PORT} user={MQTT_USER or '(none)'} "
             f"password={'set' if MQTT_PASS else 'not set'}")
    bridge = Bridge()

    def _graceful(sig, frame):
        log.info(f"Signal {sig} received; stopping...")
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    try:
        bridge.start()
        while True:
            now = time.monotonic()
            if (now - bridge._last_diag_pub_time) > HEALTH_CHECK_INTERVAL:
                bridge.publish_diagnostics()
                bridge._last_diag_pub_time = now
            time.sleep(1.0)
    finally:
        bridge.stop()


if __name__ == "__main__":
    main()