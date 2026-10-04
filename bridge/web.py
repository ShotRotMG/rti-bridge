"""
Web UI for rti-bridge: live status, zone control, config editor and logs.

Plain stdlib HTTP server (no extra framework). Serves static/index.html plus a
small JSON API. Config edits go through ruamel.yaml so comments in config.yaml
survive, are validated with settings.parse() before anything is written, and
the previous file is kept as config.yaml.bak.
"""
import base64
import hmac
import io
import json
import logging
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

import settings as settings_mod
from settings import (ConfigError, ZONES_PER_AMP, SOURCES_PER_AMP, MATRIX_INPUTS, MATRIX_OUTPUTS,
                      legacy_slug)

log = logging.getLogger("rti_web")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
EXAMPLE_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.yaml"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.example.yaml"),
]
MASK = "********"


# ---------------------------------------------------------------------------
# config.yaml read/write (comment preserving)
# ---------------------------------------------------------------------------
def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _read_text(path: str) -> str:
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    for p in EXAMPLE_PATHS:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    return "amps: []\n"


def _load_doc(text: str) -> CommentedMap:
    doc = _yaml().load(text)
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, dict):
        raise ConfigError("config.yaml must be a mapping at the top level")
    return doc


def _dump(doc) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _secret(doc, section: str) -> str:
    sect = doc.get(section)
    return str(sect.get("password") or "") if isinstance(sect, dict) else ""


def _write_validated(path: str, doc: CommentedMap) -> None:
    """Validate (file only, no env overrides), back up, write atomically."""
    settings_mod.parse(json.loads(json.dumps(doc, default=str)), path, apply_env=False)
    text = _dump(doc)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.isfile(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    log.info(f"[web] config saved to {path} (previous kept as config.yaml.bak)")


def raw_get(path: str) -> str:
    text = _read_text(path)
    try:
        doc = _load_doc(text)
    except Exception:
        return text  # broken YAML: show as-is so it can be fixed
    changed = False
    for section in ("mqtt", "web"):
        if _secret(doc, section):
            doc[section]["password"] = MASK
            changed = True
    return _dump(doc) if changed else text


def raw_parse(path: str, text: str) -> CommentedMap:
    try:
        doc = _load_doc(text)
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"invalid YAML: {e}")
    try:
        current = _load_doc(_read_text(path)) if os.path.isfile(path) else CommentedMap()
    except Exception:
        current = CommentedMap()
    for section in ("mqtt", "web"):
        sect = doc.get(section)
        if isinstance(sect, dict) and sect.get("password") == MASK:
            sect["password"] = _secret(current, section)
    return doc


def raw_validate(path: str, text: str) -> None:
    doc = raw_parse(path, text)
    settings_mod.parse(json.loads(json.dumps(doc, default=str)), path, apply_env=False)


def raw_save(path: str, text: str) -> None:
    _write_validated(path, raw_parse(path, text))


def form_get(path: str) -> dict:
    """Structured view of the file (not merged with env overrides)."""
    doc = _load_doc(_read_text(path))
    plain = json.loads(json.dumps(doc, default=str))
    cfg = settings_mod.parse(plain, path, apply_env=False)
    return {
        "mqtt": {k: cfg.mqtt[k] for k in ("host", "port", "user", "base_topic", "matrix_base_topic",
                                          "discovery_prefix")},
        "mqtt_has_password": bool(cfg.mqtt["password"]),
        "cleanup_orphans": bool(cfg.discovery.get("cleanup_orphans")),
        "log_level": str(cfg.logging.get("level", "INFO")).upper(),
        "poll_interval_sec": cfg.timing["poll_interval_sec"],
        "web_port": cfg.web["port"],
        "web_has_password": bool(cfg.web["password"]),
        "env_overrides": sorted(k for k in settings_mod.ENV_OVERRIDES if os.getenv(k)),
        "amps": [{
            "id": a.id, "name": a.name, "host": a.host, "port": a.port,
            "zones": {str(n): {"name": z.name, "id": z.id} for n, z in a.zones.items()},
            "sources": {str(n): a.sources.get(n, "") for n in range(1, SOURCES_PER_AMP + 1)},
        } for a in cfg.amps],
        "matrices": [{
            "id": m.id, "name": m.name, "host": m.host, "port": m.port,
            "poll_interval_sec": m.poll_interval_sec,
            "inputs": {str(n): m.inputs.get(n, "") for n in range(1, MATRIX_INPUTS + 1)},
            "outputs": {str(n): {"exposed": n in m.outputs,
                                 "name": m.outputs[n].name if n in m.outputs else f"Output {n}",
                                 "id": m.outputs[n].id if n in m.outputs else ""}
                        for n in range(1, MATRIX_OUTPUTS + 1)},
        } for m in cfg.matrices],
    }


def _section(doc, key) -> CommentedMap:
    if not isinstance(doc.get(key), dict):
        doc[key] = CommentedMap()
    return doc[key]


def _set_or_insert(m: CommentedMap, pos: int, key, value):
    if key in m:
        m[key] = value
    else:
        m.insert(min(pos, len(m)), key, value)


def form_save(path: str, data: dict) -> None:
    doc = _load_doc(_read_text(path))

    mq = _section(doc, "mqtt")
    for k in ("host", "user", "base_topic", "matrix_base_topic", "discovery_prefix"):
        if k in data.get("mqtt", {}):
            mq[k] = str(data["mqtt"][k]).strip()
    if "port" in data.get("mqtt", {}):
        mq["port"] = int(data["mqtt"]["port"])
    if data.get("mqtt_password"):            # blank = keep current
        mq["password"] = str(data["mqtt_password"])
    if data.get("mqtt_password_clear"):
        mq["password"] = ""

    if "cleanup_orphans" in data:
        _section(doc, "discovery")["cleanup_orphans"] = bool(data["cleanup_orphans"])
    if "log_level" in data:
        _section(doc, "logging")["level"] = str(data["log_level"]).upper()
    if "poll_interval_sec" in data:
        v = float(data["poll_interval_sec"])
        _section(doc, "timing")["poll_interval_sec"] = int(v) if v.is_integer() else v
    if "web_port" in data:
        _section(doc, "web")["port"] = int(data["web_port"])
    if data.get("web_password"):
        _section(doc, "web")["password"] = str(data["web_password"])
    if data.get("web_password_clear"):
        _section(doc, "web")["password"] = ""

    if "amps" in data:
        old = {str(a.get("id")): a for a in (doc.get("amps") or []) if isinstance(a, dict)}
        seq = CommentedSeq()
        for a in data["amps"]:
            amp_id = str(a.get("id") or "").strip()
            m = old.get(amp_id)
            if m is None:
                m = CommentedMap()
            _set_or_insert(m, 0, "id", amp_id)
            name = str(a.get("name") or "").strip()
            if name:
                _set_or_insert(m, 1, "name", name)
            elif "name" in m:
                del m["name"]
            _set_or_insert(m, 2, "host", str(a.get("host") or "").strip())
            _set_or_insert(m, 3, "port", int(a.get("port") or 23))

            zones = CommentedMap()
            for n in range(1, ZONES_PER_AMP + 1):
                z = (a.get("zones") or {}).get(str(n)) or {}
                zname = str(z.get("name") or "").strip() or f"Zone {n}"
                zid = str(z.get("id") or "").strip() or legacy_slug(zname)
                entry = CommentedMap([("name", zname), ("id", zid)])
                entry.fa.set_flow_style()
                zones[n] = entry
            _set_or_insert(m, 4, "zones", zones)

            sources = CommentedMap()
            for n in range(1, SOURCES_PER_AMP + 1):
                label = str((a.get("sources") or {}).get(str(n)) or "").strip()
                if label:
                    sources[n] = label
            if not sources:
                sources.fa.set_flow_style()
            _set_or_insert(m, 5, "sources", sources)
            seq.append(m)
        doc["amps"] = seq

    if "matrices" in data and (data["matrices"] or "matrices" in doc):
        old = {str(m.get("id")): m for m in (doc.get("matrices") or []) if isinstance(m, dict)}
        seq = CommentedSeq()
        for d in data["matrices"]:
            mid = str(d.get("id") or "").strip()
            m = old.get(mid)
            if m is None:
                m = CommentedMap()
            _set_or_insert(m, 0, "id", mid)
            name = str(d.get("name") or "").strip()
            if name:
                _set_or_insert(m, 1, "name", name)
            elif "name" in m:
                del m["name"]
            _set_or_insert(m, 2, "host", str(d.get("host") or "").strip())
            port = int(d.get("port") or 80)
            if port != 80 or "port" in m:
                _set_or_insert(m, 3, "port", port)
            v = float(d.get("poll_interval_sec") or 5)
            _set_or_insert(m, 4, "poll_interval_sec", int(v) if v.is_integer() else v)

            inputs = CommentedMap()
            for n in range(1, MATRIX_INPUTS + 1):
                label = str((d.get("inputs") or {}).get(str(n)) or "").strip()
                if label:
                    inputs[n] = label
            if not inputs:
                inputs.fa.set_flow_style()
            _set_or_insert(m, 5, "inputs", inputs)

            outputs = CommentedMap()
            for n in range(1, MATRIX_OUTPUTS + 1):
                o = (d.get("outputs") or {}).get(str(n)) or {}
                if not o.get("exposed"):
                    continue
                oname = str(o.get("name") or "").strip() or f"Output {n}"
                oid = str(o.get("id") or "").strip() or legacy_slug(oname)
                entry = CommentedMap([("name", oname), ("id", oid)])
                entry.fa.set_flow_style()
                outputs[n] = entry
            if not outputs:
                outputs.fa.set_flow_style()
            _set_or_insert(m, 6, "outputs", outputs)
            seq.append(m)
        doc["matrices"] = seq

    _write_validated(path, doc)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "rti-bridge"
    bridge = None                  # Bridge or None (config-error mode)
    restart: threading.Event = None
    config_error: Optional[str] = None
    password: str = ""
    log_buffer = None

    def log_message(self, fmt, *args):  # keep HTTP noise out of the bridge log
        log.debug("[web] " + fmt % args)

    # -- helpers --
    def _send(self, code: int, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode() if ctype == "application/json" else str(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("json") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not self.password:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                if hmac.compare_digest(pw, self.password):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="rti-bridge"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json_body(self) -> Optional[dict]:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._send(415, {"ok": False, "error": "Content-Type must be application/json"})
            return None
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid JSON"})
            return None

    def _cfg_path(self) -> str:
        return settings_mod.target_config_path()

    def _schedule_restart(self):
        threading.Timer(0.5, self.restart.set).start()

    # -- routes --
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":           # unauthenticated, for the Docker healthcheck
            return self._send(200, "ok", "text/plain")
        if not self._authed():
            return
        if url.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html")
        if url.path == "/api/status":
            if self.bridge is None:
                return self._send(200, {"bridge_running": False, "config_error": self.config_error,
                                        "config_path": self._cfg_path()})
            snap = self.bridge.snapshot()
            snap["bridge_running"] = True
            return self._send(200, snap)
        if url.path == "/api/logs":
            after = int((parse_qs(url.query).get("after") or ["0"])[0] or 0)
            buf = list(self.log_buffer.lines) if self.log_buffer else []
            lines = [{"seq": s, "level": lvl, "text": t} for s, lvl, t in buf if s > after]
            return self._send(200, {"lines": lines})
        if url.path == "/api/config/raw":
            return self._send(200, {"path": self._cfg_path(), "text": raw_get(self._cfg_path())})
        if url.path == "/api/config/form":
            try:
                return self._send(200, {"ok": True, "form": form_get(self._cfg_path())})
            except ConfigError as e:
                return self._send(200, {"ok": False, "error": str(e)})
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        body = self._json_body()
        if body is None:
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/zone":
                if self.bridge is None:
                    return self._send(503, {"ok": False, "error": "bridge not running"})
                ok = self.bridge.zone_command(str(body.get("amp")), int(body.get("zone")),
                                              str(body.get("cmd")).lower(), str(body.get("value", "")))
                log.info(f"[web] {body.get('amp')} zone {body.get('zone')} {body.get('cmd')} <- '{body.get('value', '')}' -> {'ok' if ok else 'rejected'}")
                return self._send(200, {"ok": ok})
            if path == "/api/route":
                if self.bridge is None:
                    return self._send(503, {"ok": False, "error": "bridge not running"})
                log.info(f"[web] {body.get('matrix')} output {body.get('output')} <- input {body.get('input')}")
                ok = self.bridge.matrix_route(str(body.get("matrix")), int(body.get("output")), str(body.get("input")))
                return self._send(200, {"ok": ok})
            if path == "/api/all_off":
                if self.bridge is None:
                    return self._send(503, {"ok": False, "error": "bridge not running"})
                log.info("[web] ALL OFF")
                self.bridge.all_off()
                return self._send(200, {"ok": True})
            if path == "/api/config/validate":
                raw_validate(self._cfg_path(), str(body.get("text", "")))
                return self._send(200, {"ok": True})
            if path == "/api/config/raw":
                raw_save(self._cfg_path(), str(body.get("text", "")))
                self._schedule_restart()
                return self._send(200, {"ok": True, "restarting": True})
            if path == "/api/config/form":
                form_save(self._cfg_path(), body)
                self._schedule_restart()
                return self._send(200, {"ok": True, "restarting": True})
            if path == "/api/restart":
                log.info("[web] restart requested")
                self._schedule_restart()
                return self._send(200, {"ok": True, "restarting": True})
        except ConfigError as e:
            return self._send(200, {"ok": False, "error": str(e)})
        except (TypeError, ValueError) as e:
            return self._send(200, {"ok": False, "error": f"bad value: {e}"})
        self._send(404, {"ok": False, "error": "not found"})


def _web_settings(loaded: Optional[dict]) -> dict:
    """Web settings from the loaded config, or best-effort from the raw file in config-error mode."""
    if loaded is not None:
        return dict(loaded)
    web = dict(settings_mod.DEFAULTS["web"])
    try:
        doc = _load_doc(_read_text(settings_mod.target_config_path()))
        if isinstance(doc.get("web"), dict):
            web.update(doc["web"])
    except Exception:
        pass
    if os.getenv("WEB_PORT"):
        web["port"] = os.getenv("WEB_PORT")
    if os.getenv("WEB_PASSWORD"):
        web["password"] = os.getenv("WEB_PASSWORD")
    try:
        web["port"] = int(web["port"])
    except (TypeError, ValueError):
        web["port"] = 8088
    web["enabled"] = True  # always serve the UI when the config needs fixing
    return web


def start_server(bridge, restart: threading.Event, log_buffer, web_cfg: Optional[dict] = None,
                 config_error: Optional[str] = None):
    web = _web_settings(web_cfg)
    if not web.get("enabled", True):
        log.info("[web] disabled in config")
        return None
    handler = type("Handler", (_Handler,), {
        "bridge": bridge, "restart": restart, "config_error": config_error,
        "password": str(web.get("password") or ""), "log_buffer": log_buffer,
    })
    try:
        srv = ThreadingHTTPServer((str(web.get("host") or "0.0.0.0"), int(web["port"])), handler)
    except OSError as e:
        log.error(f"[web] could not listen on port {web['port']}: {e}")
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info(f"[web] UI on http://{web.get('host') or '0.0.0.0'}:{web['port']}/"
             f"{' (password protected)' if handler.password else ''}")
    return srv
