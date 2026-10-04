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
  2.0.0: web UI (status, zone control, config editor, logs) on port 8088
  2.1.0: RTI VHD-8x matrix module (HTTP): source select per output, signal/link sensors
  2.1.1: from upstream 2.0.x: paced bass/treble (one send, settle, one verify),
         exponential reconnect backoff, staggered amp start, 0.2s command pacing
  2.1.2: advanced timing section in the web config form
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
import vhd8x
from settings import ConfigError, Settings, slugify, ZONES_PER_AMP


# LOGGING
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rti_ad8x_bridge")


class _RingHandler(logging.Handler):
    """Keeps the last N formatted log lines for the web UI's Logs tab."""
    def __init__(self, size: int = 1000):
        super().__init__()
        import collections
        self.lines = collections.deque(maxlen=size)
        self.seq = 0

    def emit(self, record):
        try:
            self.seq += 1
            self.lines.append((self.seq, record.levelname, self.format(record)))
        except Exception:
            pass


LOG_BUFFER = _RingHandler()
LOG_BUFFER.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(LOG_BUFFER)
__version__ = "2.1.2"


# CONFIG - populated from config.yaml by apply_settings() at startup
SETTINGS: Optional[Settings] = None
MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS = "127.0.0.1", 1883, "", ""
MQTT_BASE, DISCOVERY_PREFIX = "rti/ad8x", "homeassistant"
MATRIX_BASE = "rti/vhd8x"
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
TONE_SETTLE_SEC = 6.0
RECONNECT_BACKOFF_INITIAL = 5.0
RECONNECT_BACKOFF_MAX = 30.0
AMP_START_STAGGER = 1.5
ORPHAN_SCAN_SEC = 5.0  # how long to collect retained discovery configs before cleanup


def apply_settings(cfg: Settings):
    global SETTINGS, MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, MQTT_BASE, DISCOVERY_PREFIX, MATRIX_BASE
    global POLL_INTERVAL_SEC, CONNECT_TIMEOUT, PER_CMD_TIMEOUT, POST_SEND_SETTLE, INTER_CMD_SLEEP
    global SET_RETRIES, RETRY_SLEEP, DUMP_RAW_CHUNKS, VOL_COALESCE_SEC, VOL_ECHO_SUPPRESS_SEC
    global HEALTH_CHECK_INTERVAL, TONE_SETTLE_SEC, RECONNECT_BACKOFF_INITIAL, RECONNECT_BACKOFF_MAX
    global AMP_START_STAGGER
    SETTINGS = cfg
    m, t = cfg.mqtt, cfg.timing
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS = m["host"], m["port"], m["user"], m["password"]
    MQTT_BASE, DISCOVERY_PREFIX = m["base_topic"], m["discovery_prefix"]
    MATRIX_BASE = m["matrix_base_topic"]
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
    TONE_SETTLE_SEC = t["tone_settle_sec"]
    RECONNECT_BACKOFF_INITIAL = t["reconnect_backoff_initial_sec"]
    RECONNECT_BACKOFF_MAX = t["reconnect_backoff_max_sec"]
    AMP_START_STAGGER = t["amp_start_stagger_sec"]
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
    def __init__(self, amp_name: str, addr: Tuple[str, int], mqttc: mqtt.Client, start_delay: float = 0.0):
        super().__init__(daemon=True)
        self.start_delay = start_delay
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
        self.last_poll_ok: Optional[float] = None

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
        buf = self._zone_states.setdefault(z, {})
        # while a bass/treble change is settling, keep showing the target, not the amp's stale value
        tone_data = dict(tone_data)
        for f in ("bass", "treble"):
            if time.time() < buf.get(f"{f}_hold_until", 0.0) and buf.get(f"target_{f}") is not None:
                tone_data[f] = buf[f"target_{f}"]
        self.mqttc.publish(f"{base}/bass", str(tone_data["bass"]), retain=True)
        self.mqttc.publish(f"{base}/treble", str(tone_data["treble"]), retain=True)

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

    def _tone_now(self, zone: int, field: str) -> int:
        """Current bass/treble, counting a change that is still pending or settling."""
        buf = self._zone_states.get(zone, {})
        pending = (buf.get(f"{field}_timer") and buf[f"{field}_timer"].is_alive()) or \
            time.time() < buf.get(f"{field}_hold_until", 0.0)
        if pending and buf.get(f"target_{field}") is not None:
            return buf[f"target_{field}"]
        return buf.get(field, 0)

    def bass_up(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        return self.set_bass(zone, min(12, self._tone_now(zone, "bass") + 2))

    def bass_down(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        return self.set_bass(zone, max(-12, self._tone_now(zone, "bass") - 2))

    def treble_up(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        return self.set_treble(zone, min(12, self._tone_now(zone, "treble") + 2))

    def treble_down(self, zone: int) -> bool:
        if not self._is_zone_on(zone):
            return False
        return self.set_treble(zone, max(-12, self._tone_now(zone, "treble") - 2))

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
        self._flush_tone(zone, "bass")

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
        self._flush_tone(zone, "treble")

    def _flush_tone(self, zone: int, field: str):
        """The AD-8x applies tone changes slowly. Send the absolute target once, show it
        right away, let the amp settle, then check once - instead of resending."""
        if not self._is_zone_on(zone):
            return
        buf = self._zone_states.setdefault(zone, {})
        target = buf.get(f"target_{field}")
        if target is None:
            return
        opcode = "BAS" if field == "bass" else "TRB"
        log.info(f"[{self.amp_name}] {field.upper()} zone {zz(zone)} -> {target} (verify in {TONE_SETTLE_SEC:g}s)")
        buf[f"{field}_hold_until"] = time.time() + TONE_SETTLE_SEC + 2.0
        buf[f"{field}_seq"] = seq = buf.get(f"{field}_seq", 0) + 1
        self.mqttc.publish(self._topic("zone", zone, field), str(target), retain=True)
        if not self._send_only(f"*ZN{zz(zone)}{opcode}{_encode_tone(target)}"):
            buf[f"{field}_hold_until"] = 0.0
            log.warning(f"[{self.amp_name}] {field} send failed for zone {zone}")
            return
        threading.Timer(TONE_SETTLE_SEC, self._verify_tone, args=(zone, field, target, seq)).start()

    def _verify_tone(self, zone: int, field: str, target: int, seq: int):
        buf = self._zone_states.setdefault(zone, {})
        if buf.get(f"{field}_seq") != seq:
            return  # a newer change superseded this one; it will verify itself
        tone = None
        with self.lock:
            if self.connected or self._connect():
                try:
                    self._send_ascii(f"*ZN{zz(zone)}SET00")
                    time.sleep(POST_SEND_SETTLE)
                    tone = parse_tone(self._read_reply(f"${zz(zone)},", PER_CMD_TIMEOUT))
                except Exception as e:
                    log.warning(f"[{self.amp_name}] {field} verify failed for zone {zone}: {e}")
        if buf.get(f"{field}_seq") != seq:
            return
        buf[f"{field}_hold_until"] = 0.0
        if not tone:
            return  # next poll will publish the real value
        buf.update(tone)
        for f in ("bass", "treble"):
            self.mqttc.publish(self._topic("zone", zone, f), str(tone[f]), retain=True)
        if tone[field] != target:
            log.warning(f"[{self.amp_name}] zone {zone} {field}: amp reports {tone[field]}, wanted {target}")

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
        self.last_poll_ok = time.time()
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
        if self.start_delay and self.stop_flag.wait(self.start_delay):
            return
        while not self.stop_flag.is_set():
            if self._poll_once():
                half_interval = POLL_INTERVAL_SEC / 2
                if self.stop_flag.wait(half_interval):
                    break
                self._keep_alive()
                self.stop_flag.wait(half_interval)
            else:
                n = max(1, self._consecutive_failures)
                delay = min(RECONNECT_BACKOFF_MAX, RECONNECT_BACKOFF_INITIAL * (2 ** (n - 1)))
                log.warning(f"[{self.amp_name}] Poll failed, retrying in {delay:.1f}s...")
                self.stop_flag.wait(delay)

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
        self.matrices: dict = {}
        self._disc_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._scan_seen: set = set()

    def _pub_disc(self, component: str, object_id: str, cfg: dict):
        t = discovery_topic(component, object_id)
        self._published_disc.add(t)
        self.client.publish(t, json.dumps(cfg), retain=True)

    def _publish_matrix_discovery(self, sess):
        m = sess.cfg
        dev = {"identifiers": [f"vhd8x_{m.id}"], "manufacturer": "RTI", "model": "VHD-8x",
               "name": m.device_name, "configuration_url": f"http://{m.host}/index.htm"}
        avail = sess.topic("status")
        labels = sess.labels
        options = [labels[n] for n in sorted(labels)]
        with self._disc_lock:
            for n, out in m.outputs.items():
                oid = slugify(f"vhd8x_{m.id}_{out.id}")
                self._pub_disc("select", f"{oid}_source", {
                    "name": f"{out.name} Source", "uniq_id": f"{oid}_source",
                    "stat_t": sess.topic("output", n, "source"),
                    "cmd_t": sess.topic("output", n, "set", "source"),
                    "options": options, "avty_t": avail, "device": dev, "icon": "mdi:video-input-hdmi",
                })
                self._pub_disc("binary_sensor", f"{oid}_link", {
                    "name": f"{out.name} Link", "uniq_id": f"{oid}_link",
                    "stat_t": sess.topic("output", n, "link"), "pl_on": "on", "pl_off": "off",
                    "dev_cla": "connectivity", "ent_cat": "diagnostic", "avty_t": avail, "device": dev,
                })
            for n in range(1, settings_mod.MATRIX_INPUTS + 1):
                iid = slugify(f"vhd8x_{m.id}_input_{n}")
                self._pub_disc("binary_sensor", f"{iid}_signal", {
                    "name": f"{labels[n]} Signal", "uniq_id": f"{iid}_signal",
                    "stat_t": sess.topic("input", n, "signal"), "pl_on": "on", "pl_off": "off",
                    "icon": "mdi:video-input-hdmi", "avty_t": avail, "device": dev,
                })

    def _matrix_labels_changed(self, sess):
        log.info(f"[{sess.cfg.id}] input names from matrix: "
                 + ", ".join(f"{n}={l}" for n, l in sorted(sess.labels.items())))
        if self.client.is_connected():
            self._publish_matrix_discovery(sess)
            sess.republish()

    def publish_discovery(self):
        self._published_disc = set()
        for sess in self.matrices.values():
            self._publish_matrix_discovery(sess)
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
        if any(str(i).startswith(("ad8x_", "vhd8x_")) for i in ids):
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

        for i, amp in enumerate(SETTINGS.amps):
            name, addr = amp.id, (amp.host, amp.port)
            s = AmpSession(name, addr, self.client, start_delay=i * AMP_START_STAGGER)
            self.sessions[name] = s
            s.start()
            log.info(f"Started AmpSession {name} -> {addr[0]}:{addr[1]}")

        for m in SETTINGS.matrices:
            sess = vhd8x.MatrixSession(m, self.client, MATRIX_BASE, self._matrix_labels_changed)
            self.matrices[m.id] = sess
            sess.start()
            log.info(f"Started MatrixSession {m.id} -> http://{m.host}:{m.port} (every {m.poll_interval_sec:g}s)")

    def stop(self):
        for s in self.sessions.values():
            s.stop()
        for m in self.matrices.values():
            m.stop()
        self.client.loop_stop()
        self.client.disconnect()

    def on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            client.subscribe(f"{self._topic('+', 'zone', '+', 'set', '+')}")
            client.subscribe(f"{self._topic('+', 'raw')}")
            client.subscribe(f"{self._topic('all', 'command')}")
            client.subscribe(f"{DISCOVERY_PREFIX}/status")
            if SETTINGS.matrices:
                client.subscribe(f"{MATRIX_BASE}/+/output/+/set/source")
            client.publish(self._topic("bridge", "status"), "online", retain=True)
            self.publish_discovery()
            for sess in self.matrices.values():
                sess.republish()
            self._start_orphan_scan()
            log.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
        else:
            log.error(f"MQTT connect failed code: {rc}")

    # ---- commands (shared by MQTT and the web UI) ---------------------
    def all_off(self):
        for s in self.sessions.values():
            s.all_zones_off_optimistic()
        for amp_key, s in self.sessions.items():
            for z in range(1, ZONES_PER_AMP + 1):
                base_t = s._topic("zone", z)
                self.client.publish(f"{base_t}/power", "off", retain=True)
                self.client.publish(f"{base_t}/mute", "off", retain=True)
                s._zone_states.setdefault(z, {})["power"] = False
        log.info("Sent ALL OFF command and optimistically set all zones to OFF")

    def send_raw(self, amp: str, payload: str) -> Optional[str]:
        sess = self.sessions.get(amp)
        if not sess:
            return None
        with sess.lock:
            if not sess.connected and not sess._connect():
                return None
            log.info(f"[{amp}] raw <- '{payload}'")
            sess._send_ascii(payload)
            time.sleep(POST_SEND_SETTLE)
            return sess._readline(PER_CMD_TIMEOUT) or ""

    def zone_command(self, amp: str, zone: int, cmd: str, payload: str) -> bool:
        sess = self.sessions.get(amp)
        if not sess or not 1 <= zone <= ZONES_PER_AMP:
            return False
        payload = str(payload).strip()
        try:
            if cmd == "power":
                if payload.lower() in ("1", "on", "true"):
                    last_vol = sess._zone_states.get(zone, {}).get("vol_0_75", 65)
                    log.info(f"[{amp}] Power ON for zone {zone} received. Setting volume to {last_vol} to power on.")
                    return sess.set_volume(zone, last_vol)
                return sess.set_power(zone, False)
            if cmd == "mute":
                return sess.set_mute(zone, payload.lower() in ("1", "on", "true"))
            if cmd == "toggle_mute":
                return sess.toggle_mute(zone)
            if cmd == "source":
                src = SETTINGS.amp(amp).source_number(payload)
                if src is None:
                    log.warning(f"[{amp}] Unknown source '{payload}' for zone {zone}")
                    return False
                return sess.set_source(zone, src)
            if cmd == "volume":
                return sess.set_volume(zone, int(payload))
            if cmd == "bass":
                return sess.set_bass(zone, int(payload))
            if cmd == "treble":
                return sess.set_treble(zone, int(payload))
            simple = {
                "volume_up": sess.volume_up, "volume_down": sess.volume_down,
                "bass_up": sess.bass_up, "bass_down": sess.bass_down,
                "treble_up": sess.treble_up, "treble_down": sess.treble_down,
            }
            if cmd in simple:
                return simple[cmd](zone)
        except ValueError:
            log.warning(f"[{amp}] Bad value '{payload}' for {cmd} on zone {zone}")
        return False

    def matrix_route(self, matrix_id: str, output_n: int, input_value: str) -> bool:
        sess = self.matrices.get(matrix_id)
        return bool(sess and sess.route(int(output_n), str(input_value)))

    def snapshot(self) -> dict:
        """Current state for the web UI."""
        amps = []
        for amp in SETTINGS.amps:
            sess = self.sessions.get(amp.id)
            zones = []
            for z in range(1, ZONES_PER_AMP + 1):
                st = dict(sess._zone_states.get(z, {})) if sess else {}
                zones.append({
                    "n": z, "name": amp.zones[z].name, "id": amp.zones[z].id,
                    "power": st.get("power"), "mute": st.get("mute"),
                    "source": st.get("source"),
                    "source_label": amp.source_label(st["source"]) if st.get("source") else None,
                    "vol_0_75": st.get("vol_0_75"), "bass": st.get("bass"), "treble": st.get("treble"),
                })
            amps.append({
                "id": amp.id, "name": amp.device_name, "host": amp.host, "port": amp.port,
                "connected": bool(sess and sess.connected),
                "failures": sess._consecutive_failures if sess else 0,
                "last_poll_ok": sess.last_poll_ok if sess else None,
                "source_options": amp.source_options,
                "zones": zones,
            })
        return {
            "version": __version__,
            "uptime_s": int(time.monotonic() - self._start_time),
            "config_path": SETTINGS.path,
            "mqtt": {"host": MQTT_HOST, "port": MQTT_PORT, "connected": self.client.is_connected()},
            "poll_interval_sec": POLL_INTERVAL_SEC,
            "amps": amps,
            "matrices": [self.matrices[m.id].snapshot() for m in SETTINGS.matrices if m.id in self.matrices],
        }

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

            if topic.startswith(MATRIX_BASE + "/"):
                parts = topic[len(MATRIX_BASE) + 1:].split("/")
                # <matrix>/output/<n>/set/source
                if len(parts) == 5 and parts[1] == "output" and parts[3:] == ["set", "source"]:
                    log.info(f"[{parts[0]}] output {parts[2]} source <- '{payload}'")
                    threading.Thread(target=self.matrix_route, args=(parts[0], int(parts[2]), payload),
                                     daemon=True).start()
                return

            if not topic.startswith(MQTT_BASE + "/"):
                return
            # parts relative to the base topic: <amp>/zone/<n>/set/<cmd>, <amp>/raw, all/command
            parts = topic[len(MQTT_BASE) + 1:].split("/")

            if parts == ["all", "command"]:
                log.info(f"Received master command: {payload}")
                if payload.upper() == "OFF":
                    self.all_off()
                return

            if len(parts) == 2 and parts[1] == "raw":
                line = self.send_raw(parts[0], payload)
                if line is not None:
                    client.publish(self._topic(parts[0], "ack", "raw"), line, retain=False)
                return

            if len(parts) != 5 or parts[1] != "zone" or parts[3] != "set":
                return
            amp, zone_str, cmd = parts[0], parts[2], parts[4].lower()
            log.info(f"[{amp}] zone {zone_str} {cmd} <- '{payload}'")
            try:
                zone = int(zone_str)
            except ValueError:
                return
            ok = self.zone_command(amp, zone, cmd, payload)
            client.publish(self._topic(amp, "zone", "ack", cmd), "ok" if ok else "err", retain=False)

        except Exception:
            traceback.print_exc()


def _restart_process():
    log.info("Restarting bridge process...")
    logging.shutdown()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    import web  # local module

    restart = threading.Event()
    try:
        cfg = settings_mod.load()
    except ConfigError as e:
        # Keep the web UI up so the config can be fixed from the browser.
        log.error(f"Config error: {e}")
        server = web.start_server(None, restart, LOG_BUFFER, config_error=str(e))
        if not server:
            sys.exit(2)
        log.error("Bridge NOT running - fix config.yaml in the web UI (Config tab) or on disk.")
        restart.wait()
        _restart_process()
        return

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

    bridge.start()
    web.start_server(bridge, restart, LOG_BUFFER, cfg.web)
    try:
        while not restart.is_set():
            now = time.monotonic()
            if (now - bridge._last_diag_pub_time) > HEALTH_CHECK_INTERVAL:
                bridge.publish_diagnostics()
                bridge._last_diag_pub_time = now
            restart.wait(1.0)
    finally:
        bridge.stop()
    _restart_process()


if __name__ == "__main__":
    main()