"""
Configuration for rti-bridge.

Everything lives in config.yaml. The MQTT_* and LOG_LEVEL environment variables
override the file, so secrets can stay in the container environment if you prefer.

Search order for the file: $CONFIG_PATH, /config/config.yaml, ./config/config.yaml
"""
import copy
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

ZONES_PER_AMP = 8
SOURCES_PER_AMP = 8

DEFAULTS = {
    "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "user": "",
        "password": "",
        "base_topic": "rti/ad8x",
        "discovery_prefix": "homeassistant",
    },
    "discovery": {
        # Remove retained HA discovery entries that belong to this bridge's amps
        # but are no longer in this config (renamed/removed zones or amps).
        "cleanup_orphans": False,
    },
    "logging": {"level": "INFO"},
    "timing": {
        "poll_interval_sec": 20.0,
        "connect_timeout_sec": 6.0,
        "per_cmd_timeout_sec": 5.0,
        "post_send_settle_sec": 0.1,
        "inter_cmd_sleep_sec": 0.08,
        "set_retries": 2,
        "retry_sleep_sec": 0.2,
        "vol_coalesce_sec": 1.2,
        "vol_echo_suppress_sec": 1.0,
        "health_check_interval_sec": 30.0,
        "dump_raw_chunks": True,
    },
    "amps": [],
}

ENV_OVERRIDES = {
    "MQTT_HOST": ("mqtt", "host", str),
    "MQTT_PORT": ("mqtt", "port", int),
    "MQTT_USER": ("mqtt", "user", str),
    "MQTT_PASS": ("mqtt", "password", str),
    "MQTT_BASE": ("mqtt", "base_topic", str),
    "DISCOVERY_PREFIX": ("mqtt", "discovery_prefix", str),
    "LOG_LEVEL": ("logging", "level", str),
}

_ID_RE = re.compile(r"^[a-z0-9_]+$")


class ConfigError(Exception):
    pass


def legacy_slug(s: str) -> str:
    """Same transform the original bridge used for unique IDs (no strip),
    so zones without an explicit id keep their existing HA entities."""
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in s)


def slugify(s: str) -> str:
    return legacy_slug(s).strip("_")


@dataclass
class Zone:
    number: int
    name: str
    id: str


@dataclass
class Amp:
    id: str
    host: str
    port: int
    zones: Dict[int, Zone]
    sources: Dict[int, str]  # number -> label ("1".."8" when unnamed)
    name: str = ""           # optional HA device name

    @property
    def device_name(self) -> str:
        return self.name or f"RTI AD-8x ({self.id})"

    def source_label(self, n: int) -> str:
        return self.sources.get(n, str(n))

    def source_number(self, label: str) -> Optional[int]:
        label = str(label).strip()
        for n, lbl in self.sources.items():
            if lbl == label:
                return n
        if label.isdigit() and 1 <= int(label) <= SOURCES_PER_AMP:
            return int(label)
        return None

    @property
    def source_options(self) -> List[str]:
        return [self.source_label(n) for n in range(1, SOURCES_PER_AMP + 1)]


@dataclass
class Settings:
    path: str
    raw: dict
    mqtt: dict
    discovery: dict
    logging: dict
    timing: dict
    amps: List[Amp] = field(default_factory=list)

    def amp(self, amp_id: str) -> Optional[Amp]:
        return next((a for a in self.amps if a.id == amp_id), None)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config_path() -> str:
    candidates = [
        os.getenv("CONFIG_PATH"),
        "/config/config.yaml",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "config.yaml"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    raise ConfigError(
        "config.yaml not found. Copy config.example.yaml to config/config.yaml "
        "(or mount it at /config/config.yaml, or set CONFIG_PATH)."
    )


def _parse_zones(amp_id: str, raw) -> Dict[int, Zone]:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"amps.{amp_id}.zones must be a mapping of zone number -> name")
    zones: Dict[int, Zone] = {}
    seen_ids = {}
    for n in range(1, ZONES_PER_AMP + 1):
        entry = raw.get(n, raw.get(str(n)))
        if entry is None:
            name, zid = f"Zone {n}", None
        elif isinstance(entry, str):
            name, zid = entry, None
        elif isinstance(entry, dict):
            name, zid = str(entry.get("name") or f"Zone {n}"), entry.get("id")
        else:
            raise ConfigError(f"amps.{amp_id}.zones.{n}: expected a name or {{name, id}}")
        zid = str(zid) if zid else legacy_slug(name)
        if not _ID_RE.match(zid):
            raise ConfigError(f"amps.{amp_id}.zones.{n}.id '{zid}' may only use a-z, 0-9 and _")
        if zid in seen_ids:
            raise ConfigError(f"amps.{amp_id}: zones {seen_ids[zid]} and {n} share id '{zid}'")
        seen_ids[zid] = n
        zones[n] = Zone(n, name, zid)
    extra = [k for k in raw if str(k) not in {str(i) for i in range(1, ZONES_PER_AMP + 1)}]
    if extra:
        raise ConfigError(f"amps.{amp_id}.zones: zone numbers must be 1-{ZONES_PER_AMP}, got {extra}")
    return zones


def _parse_sources(amp_id: str, raw) -> Dict[int, str]:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"amps.{amp_id}.sources must be a mapping of input number -> label")
    out: Dict[int, str] = {}
    for k, v in raw.items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            raise ConfigError(f"amps.{amp_id}.sources: '{k}' is not an input number")
        if not 1 <= n <= SOURCES_PER_AMP:
            raise ConfigError(f"amps.{amp_id}.sources: input numbers must be 1-{SOURCES_PER_AMP}, got {n}")
        if v is None or str(v).strip() == "":
            continue
        out[n] = str(v).strip()
    labels = [out.get(n, str(n)) for n in range(1, SOURCES_PER_AMP + 1)]
    dupes = {l for l in labels if labels.count(l) > 1}
    if dupes:
        raise ConfigError(f"amps.{amp_id}.sources: labels must be unique, duplicated: {sorted(dupes)}")
    return out


def parse(raw: dict, path: str = "<memory>", apply_env: bool = True) -> Settings:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping at the top level")
    cfg = _merge(DEFAULTS, raw)

    if apply_env:
        for env, (sect, key, typ) in ENV_OVERRIDES.items():
            val = os.getenv(env)
            if val not in (None, ""):
                try:
                    cfg[sect][key] = typ(val)
                except ValueError:
                    raise ConfigError(f"env {env}={val!r} is not a valid {typ.__name__}")

    for k, v in DEFAULTS["timing"].items():
        try:
            cfg["timing"][k] = type(v)(cfg["timing"][k])
        except (TypeError, ValueError):
            raise ConfigError(f"timing.{k} must be a {type(v).__name__}")
    try:
        cfg["mqtt"]["port"] = int(cfg["mqtt"]["port"])
    except (TypeError, ValueError):
        raise ConfigError("mqtt.port must be a number")
    for k in ("base_topic", "discovery_prefix"):
        t = str(cfg["mqtt"][k]).strip("/")
        if not t or any(c in t for c in "+#"):
            raise ConfigError(f"mqtt.{k} '{t}' is not a valid topic prefix")
        cfg["mqtt"][k] = t

    amps_raw = cfg.get("amps") or []
    if not isinstance(amps_raw, list):
        raise ConfigError("amps must be a list")
    amps: List[Amp] = []
    for i, a in enumerate(amps_raw):
        if not isinstance(a, dict):
            raise ConfigError(f"amps[{i}] must be a mapping")
        amp_id = str(a.get("id") or f"amp{i + 1}")
        if not _ID_RE.match(amp_id) or amp_id == "all":
            raise ConfigError(f"amps[{i}].id '{amp_id}' may only use a-z, 0-9 and _ (and not 'all')")
        if any(x.id == amp_id for x in amps):
            raise ConfigError(f"amps: duplicate id '{amp_id}'")
        host = str(a.get("host") or "").strip()
        if not host:
            raise ConfigError(f"amps.{amp_id}.host is required")
        try:
            port = int(a.get("port", 23))
        except (TypeError, ValueError):
            raise ConfigError(f"amps.{amp_id}.port must be a number")
        amps.append(Amp(amp_id, host, port, _parse_zones(amp_id, a.get("zones")),
                        _parse_sources(amp_id, a.get("sources")), str(a.get("name") or "").strip()))

    return Settings(
        path=path, raw=raw, mqtt=cfg["mqtt"], discovery=cfg["discovery"],
        logging=cfg["logging"], timing=cfg["timing"], amps=amps,
    )


def load(path: Optional[str] = None) -> Settings:
    path = path or find_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}")
    return parse(raw, path)
