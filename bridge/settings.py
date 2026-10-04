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
MATRIX_INPUTS = 8      # VHD-8x: 8 inputs, 10 outputs (numbered 1-based like its web UI)
MATRIX_OUTPUTS = 10

DEFAULTS = {
    "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "user": "",
        "password": "",
        "base_topic": "rti/ad8x",
        "matrix_base_topic": "rti/vhd8x",
        "discovery_prefix": "homeassistant",
    },
    "discovery": {
        # Remove retained HA discovery entries that belong to this bridge's amps
        # but are no longer in this config (renamed/removed zones or amps).
        "cleanup_orphans": False,
    },
    "logging": {"level": "INFO"},
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8088,
        "password": "",   # optional HTTP Basic auth (any username)
    },
    "timing": {
        "poll_interval_sec": 20.0,
        "connect_timeout_sec": 6.0,
        "per_cmd_timeout_sec": 5.0,
        "post_send_settle_sec": 0.1,
        "inter_cmd_sleep_sec": 0.2,
        "set_retries": 2,
        "retry_sleep_sec": 0.2,
        "vol_coalesce_sec": 1.2,
        "vol_echo_suppress_sec": 1.0,
        "health_check_interval_sec": 30.0,
        "tone_settle_sec": 6.0,              # AD-8x applies bass/treble slowly
        "reconnect_backoff_initial_sec": 5.0,
        "reconnect_backoff_max_sec": 30.0,
        "amp_start_stagger_sec": 1.5,        # don't poll every amp in lockstep
        "dump_raw_chunks": True,
    },
    "amps": [],
    "matrices": [],
}

ENV_OVERRIDES = {
    "MQTT_HOST": ("mqtt", "host", str),
    "MQTT_PORT": ("mqtt", "port", int),
    "MQTT_USER": ("mqtt", "user", str),
    "MQTT_PASS": ("mqtt", "password", str),
    "MQTT_BASE": ("mqtt", "base_topic", str),
    "MQTT_MATRIX_BASE": ("mqtt", "matrix_base_topic", str),
    "DISCOVERY_PREFIX": ("mqtt", "discovery_prefix", str),
    "LOG_LEVEL": ("logging", "level", str),
    "WEB_PORT": ("web", "port", int),
    "WEB_PASSWORD": ("web", "password", str),
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
class Output:
    number: int
    name: str
    id: str


@dataclass
class Matrix:
    id: str
    host: str
    port: int
    poll_interval_sec: float
    inputs: Dict[int, str]         # configured labels only; see input_labels()
    outputs: Dict[int, Output]     # outputs exposed to HA (all 10 if none configured)
    name: str = ""

    @property
    def device_name(self) -> str:
        return self.name or f"RTI VHD-8x ({self.id})"

    def input_labels(self, device_names: Optional[Dict[int, str]] = None) -> Dict[int, str]:
        """Label per input: config name, else the matrix's own name, else "Input N".
        Made unique so they work as select options."""
        device_names = device_names or {}
        labels, seen = {}, set()
        for n in range(1, MATRIX_INPUTS + 1):
            lbl = self.inputs.get(n) or (device_names.get(n) or "").strip() or f"Input {n}"
            if lbl in seen:
                lbl = f"{lbl} ({n})"
            seen.add(lbl)
            labels[n] = lbl
        return labels


@dataclass
class Settings:
    path: str
    raw: dict
    mqtt: dict
    discovery: dict
    logging: dict
    timing: dict
    web: dict
    amps: List[Amp] = field(default_factory=list)
    matrices: List[Matrix] = field(default_factory=list)

    def amp(self, amp_id: str) -> Optional[Amp]:
        return next((a for a in self.amps if a.id == amp_id), None)

    def matrix(self, matrix_id: str) -> Optional[Matrix]:
        return next((m for m in self.matrices if m.id == matrix_id), None)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def target_config_path() -> str:
    """Where to write config.yaml: the existing file, else the first sensible location."""
    try:
        return find_config_path()
    except ConfigError:
        if os.getenv("CONFIG_PATH"):
            return os.getenv("CONFIG_PATH")
        if os.path.isdir("/config"):
            return "/config/config.yaml"
        return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "config.yaml"))


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


def _parse_matrices(raw, amp_ids) -> List[Matrix]:
    if not isinstance(raw, list):
        raise ConfigError("matrices must be a list")
    out: List[Matrix] = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ConfigError(f"matrices[{i}] must be a mapping")
        mid = str(m.get("id") or ("matrix" if i == 0 else f"matrix{i + 1}"))
        if not _ID_RE.match(mid):
            raise ConfigError(f"matrices[{i}].id '{mid}' may only use a-z, 0-9 and _")
        if any(x.id == mid for x in out):
            raise ConfigError(f"matrices: duplicate id '{mid}'")
        host = str(m.get("host") or "").strip()
        if not host:
            raise ConfigError(f"matrices.{mid}.host is required")
        try:
            port = int(m.get("port", 80))
            poll = float(m.get("poll_interval_sec", 5))
        except (TypeError, ValueError):
            raise ConfigError(f"matrices.{mid}: port and poll_interval_sec must be numbers")
        if poll < 1:
            raise ConfigError(f"matrices.{mid}.poll_interval_sec must be at least 1")

        inputs: Dict[int, str] = {}
        rin = m.get("inputs") or {}
        if not isinstance(rin, dict):
            raise ConfigError(f"matrices.{mid}.inputs must be a mapping of input number -> name")
        for k, v in rin.items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                raise ConfigError(f"matrices.{mid}.inputs: '{k}' is not an input number")
            if not 1 <= n <= MATRIX_INPUTS:
                raise ConfigError(f"matrices.{mid}.inputs: input numbers must be 1-{MATRIX_INPUTS}, got {n}")
            if v is not None and str(v).strip():
                inputs[n] = str(v).strip()

        rout = m.get("outputs") or {}
        if not isinstance(rout, dict):
            raise ConfigError(f"matrices.{mid}.outputs must be a mapping of output number -> name")
        outputs: Dict[int, Output] = {}
        try:
            numbers = sorted(int(k) for k in rout) if rout else list(range(1, MATRIX_OUTPUTS + 1))
        except (TypeError, ValueError):
            raise ConfigError(f"matrices.{mid}.outputs: keys must be output numbers 1-{MATRIX_OUTPUTS}")
        seen_ids = {}
        for n in numbers:
            if not 1 <= n <= MATRIX_OUTPUTS:
                raise ConfigError(f"matrices.{mid}.outputs: output numbers must be 1-{MATRIX_OUTPUTS}, got {n}")
            entry = rout.get(n, rout.get(str(n))) if rout else None
            if entry is None or isinstance(entry, str):
                name, oid = (entry or f"Output {n}"), None
            elif isinstance(entry, dict):
                name, oid = str(entry.get("name") or f"Output {n}"), entry.get("id")
            else:
                raise ConfigError(f"matrices.{mid}.outputs.{n}: expected a name or {{name, id}}")
            oid = str(oid) if oid else legacy_slug(name)
            if not _ID_RE.match(oid):
                raise ConfigError(f"matrices.{mid}.outputs.{n}.id '{oid}' may only use a-z, 0-9 and _")
            if oid in seen_ids:
                raise ConfigError(f"matrices.{mid}: outputs {seen_ids[oid]} and {n} share id '{oid}'")
            seen_ids[oid] = n
            outputs[n] = Output(n, name, oid)
        out.append(Matrix(mid, host, port, poll, inputs, outputs, str(m.get("name") or "").strip()))
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
    try:
        cfg["web"]["port"] = int(cfg["web"]["port"])
    except (TypeError, ValueError):
        raise ConfigError("web.port must be a number")
    if not 1 <= cfg["web"]["port"] <= 65535:
        raise ConfigError("web.port must be 1-65535")
    cfg["web"]["enabled"] = bool(cfg["web"]["enabled"])
    for k in ("base_topic", "matrix_base_topic", "discovery_prefix"):
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

    matrices = _parse_matrices(cfg.get("matrices") or [], {a.id for a in amps})
    if matrices and cfg["mqtt"]["matrix_base_topic"] == cfg["mqtt"]["base_topic"]:
        raise ConfigError("mqtt.matrix_base_topic must differ from mqtt.base_topic")

    return Settings(
        path=path, raw=raw, mqtt=cfg["mqtt"], discovery=cfg["discovery"],
        logging=cfg["logging"], timing=cfg["timing"], web=cfg["web"], amps=amps, matrices=matrices,
    )


def load(path: Optional[str] = None) -> Settings:
    path = path or find_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}")
    return parse(raw, path)
