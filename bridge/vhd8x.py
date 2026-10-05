"""
RTI VHD-8x (Pulse-Eight OEM) 8x10 HDBaseT matrix.

Transport is kept separate from the session so a serial (RS-232) transport can be
dropped in later with the same two methods: list_ports() and route().

HTTP API (unauthenticated, all GET, bays are 0-based):
  /Port/List                  -> {"Result": true, "Ports": [{Bay, Mode, Type, Status, Name, ReceiveFrom?}]}
  /Port/Set/{inBay}/{outBay}  -> {"Result": true}
  /Port/Details/Output/{bay}  -> {..., "DPS": n, ...}  display power (only with CEC enabled):
                                 0 on, 1 standby, 2 turning on, 3 turning off
  /CEC/{on|off}/Output/{bay}  -> {"Result": true}   (what the matrix's own power button calls)
  /key/sendcommand/{3|4}/output/{bay}               CEC volume up / down
  /System/Details             -> {Model, Version, Serial, StatusMessage, Status, ...}
Port Status: 0 ok, 1 fault, 2 warning, 3 no signal, 4 module not present.
Everything outside this module uses 1-based numbers, like the matrix's web UI.
"""
import json
import logging
import threading
import time
import urllib.request
from typing import Callable, Dict, Optional

from settings import Matrix, MATRIX_INPUTS, MATRIX_OUTPUTS

log = logging.getLogger("rti_vhd8x")

STATUS_TEXT = {0: "ok", 1: "fault", 2: "warning", 3: "no signal", 4: "not present"}


class VHD8xHTTP:
    def __init__(self, host: str, port: int = 80, timeout: float = 5.0):
        self.base = f"http://{host}" + ("" if port == 80 else f":{port}")
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def list_ports(self) -> dict:
        data = self._get("/Port/List")
        if not data.get("Result") or not isinstance(data.get("Ports"), list):
            raise RuntimeError(f"unexpected /Port/List response: {str(data)[:200]}")
        inputs, outputs = {}, {}
        for p in data["Ports"]:
            bay, mode = int(p.get("Bay", -1)), str(p.get("Mode", ""))
            entry = {"name": str(p.get("Name") or ""), "status": int(p.get("Status", 4)),
                     "type": str(p.get("Type") or "")}
            if mode == "Input" and 0 <= bay < MATRIX_INPUTS:
                inputs[bay + 1] = entry
            elif mode == "Output" and 0 <= bay < MATRIX_OUTPUTS:
                rf = p.get("ReceiveFrom")
                entry["source"] = int(rf) + 1 if isinstance(rf, int) and 0 <= rf < MATRIX_INPUTS else None
                outputs[bay + 1] = entry
        return {"inputs": inputs, "outputs": outputs}

    def route(self, input_n: int, output_n: int) -> bool:
        data = self._get(f"/Port/Set/{input_n - 1}/{output_n - 1}")
        return data.get("Result") is True

    def output_details(self, output_n: int) -> dict:
        return self._get(f"/Port/Details/Output/{output_n - 1}")

    def cec_power(self, output_n: int, on: bool) -> bool:
        data = self._get(f"/CEC/{'on' if on else 'off'}/Output/{output_n - 1}")
        return data.get("Result") is True

    def cec_volume(self, output_n: int, up: bool) -> bool:
        with urllib.request.urlopen(f"{self.base}/key/sendcommand/{3 if up else 4}/output/{output_n - 1}",
                                    timeout=self.timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return json.loads(body).get("Result", True) is not False
        except ValueError:
            return True  # some firmware answers with an empty/plain body

    def system_details(self) -> dict:
        return self._get("/System/Details")


class MatrixSession(threading.Thread):
    """Polls the matrix, publishes retained state on change, and routes on request."""

    def __init__(self, cfg: Matrix, mqttc, base_topic: str,
                 on_labels_changed: Callable[["MatrixSession"], None]):
        super().__init__(daemon=True, name=f"vhd8x-{cfg.id}")
        self.cfg = cfg
        self.mqttc = mqttc
        self.base = f"{base_topic}/{cfg.id}"
        self.transport = VHD8xHTTP(cfg.host, cfg.port)
        self.on_labels_changed = on_labels_changed
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.wake = threading.Event()
        self.connected = False
        self.failures = 0
        self.last_poll_ok: Optional[float] = None
        self.inputs: Dict[int, dict] = {}
        self.outputs: Dict[int, dict] = {}
        self.device_names: Dict[int, str] = {}
        self._sent: Dict[str, str] = {}
        self.power: Dict[int, dict] = {}   # output -> {dps, target, hold_until, seq}
        self.health: dict = {}

    # -- labels --
    @property
    def labels(self) -> Dict[int, str]:
        return self.cfg.input_labels(self.device_names)

    def input_number(self, value: str) -> Optional[int]:
        value = str(value).strip()
        for n, lbl in self.labels.items():
            if lbl == value:
                return n
        if value.isdigit() and 1 <= int(value) <= MATRIX_INPUTS:
            return int(value)
        return None

    # -- mqtt --
    def topic(self, *parts) -> str:
        return "/".join([self.base, *[str(p) for p in parts]])

    def _pub(self, topic: str, payload: str):
        if self._sent.get(topic) != payload:
            self.mqttc.publish(topic, payload, retain=True)
            self._sent[topic] = payload

    def republish(self):
        """Forget what was sent (e.g. after an MQTT reconnect) and send everything again."""
        self._sent = {}
        self._pub(self.topic("status"), "online" if self.connected else "offline")
        if self.connected:
            self._publish_state()

    def _publish_state(self):
        labels = self.labels
        if self.cfg.cec_power:
            for n in self.cfg.outputs:
                st = self.power_state(n)
                if st is not None:
                    self._pub(self.topic("output", n, "power"), "on" if st else "off")
        if self.health:
            self._pub(self.topic("health"), str(self.health.get("message") or "unknown"))
            self._pub(self.topic("health", "problem"), "off" if self.health.get("status") == 0 else "on")
            if self.health.get("version"):
                self._pub(self.topic("health", "version"), str(self.health["version"]))
        for n, i in self.inputs.items():
            self._pub(self.topic("input", n, "signal"), "on" if i["status"] in (0, 2) else "off")
        for n, o in self.outputs.items():
            src = o.get("source")
            self._pub(self.topic("output", n, "source_number"), str(src or ""))
            self._pub(self.topic("output", n, "source"), labels.get(src, "") if src else "")
            self._pub(self.topic("output", n, "link"), "on" if o["status"] in (0, 2) else "off")

    # -- polling --
    def poll_once(self) -> bool:
        with self.lock:
            try:
                ports = self.transport.list_ports()
            except Exception as e:
                self.failures += 1
                if self.failures == 1 or self.failures == 3:
                    log.warning(f"[{self.cfg.id}] poll failed ({self.failures}): {e}")
                if self.failures >= 3 and self.connected:
                    self.connected = False
                    self._pub(self.topic("status"), "offline")
                    log.error(f"[{self.cfg.id}] matrix offline after {self.failures} failed polls")
                return False

            was_connected = self.connected
            self.failures = 0
            self.connected = True
            self.last_poll_ok = time.time()
            self.inputs, self.outputs = ports["inputs"], ports["outputs"]
            names = {n: i["name"] for n, i in self.inputs.items()}
            names_changed = names != self.device_names
            self.device_names = names
            if not was_connected:
                log.info(f"[{self.cfg.id}] matrix online ({self.cfg.host})")
                self._pub(self.topic("status"), "online")
            self._publish_state()
        if names_changed:
            self.on_labels_changed(self)
        return True

    def route(self, output_n: int, input_value: str) -> bool:
        input_n = self.input_number(input_value)
        if input_n is None:
            log.warning(f"[{self.cfg.id}] unknown input '{input_value}' for output {output_n}")
            return False
        if not 1 <= output_n <= MATRIX_OUTPUTS:
            return False
        with self.lock:
            try:
                ok = self.transport.route(input_n, output_n)
            except Exception as e:
                log.error(f"[{self.cfg.id}] route input {input_n} -> output {output_n} failed: {e}")
                return False
        log.info(f"[{self.cfg.id}] route input {input_n} ({self.labels[input_n]}) -> output {output_n}: "
                 f"{'ok' if ok else 'rejected'}")
        time.sleep(0.2)
        self.poll_once()  # confirm and publish the new state right away
        return ok

    # -- display power (HDMI-CEC) --
    def power_state(self, n: int) -> Optional[bool]:
        p = self.power.get(n) or {}
        if time.time() < p.get("hold_until", 0.0) and p.get("target") is not None:
            return p["target"]
        dps = p.get("dps")
        if dps in (0, 2):
            return True
        if dps in (1, 3):
            return False
        return None

    def poll_power(self):
        for n in self.cfg.outputs:
            if self.stop_flag.is_set():
                return
            try:
                d = self.transport.output_details(n)
            except Exception as e:
                log.debug(f"[{self.cfg.id}] output {n} details failed: {e}")
                continue
            p = self.power.setdefault(n, {})
            p["dps"] = d.get("DPS")
            p["hpd"] = d.get("HPD")
        self._publish_state()

    def set_power(self, output_n: int, on: bool, attempt: int = 1) -> bool:
        if not 1 <= output_n <= MATRIX_OUTPUTS:
            return False
        try:
            ok = self.transport.cec_power(output_n, on)
        except Exception as e:
            log.error(f"[{self.cfg.id}] CEC power {'on' if on else 'off'} output {output_n} failed: {e}")
            return False
        p = self.power.setdefault(output_n, {})
        p["seq"] = seq = p.get("seq", 0) + 1
        p["target"], p["hold_until"] = on, time.time() + 20.0
        log.info(f"[{self.cfg.id}] CEC power {'on' if on else 'off'} -> output {output_n}"
                 f"{' (retry)' if attempt > 1 else ''}: {'ok' if ok else 'rejected'}")
        self._publish_state()
        threading.Timer(15.0, self._verify_power, args=(output_n, on, seq, attempt)).start()
        return ok

    def _verify_power(self, n: int, on: bool, seq: int, attempt: int):
        p = self.power.get(n) or {}
        if p.get("seq") != seq or self.stop_flag.is_set():
            return
        try:
            p["dps"] = self.transport.output_details(n).get("DPS")
        except Exception:
            pass
        reached = p.get("dps") in ((0, 2) if on else (1, 3))
        if not reached and attempt < 2:
            log.info(f"[{self.cfg.id}] output {n} still reports DPS={p.get('dps')} - sending power "
                     f"{'on' if on else 'off'} once more")
            self.set_power(n, on, attempt + 1)
            return
        p["hold_until"] = 0.0
        if not reached:
            log.warning(f"[{self.cfg.id}] display on output {n} didn't confirm power "
                        f"{'on' if on else 'off'} (DPS={p.get('dps')}) - is CEC/Anynet+ on for it?")
        self._publish_state()

    def volume(self, output_n: int, up: bool) -> bool:
        try:
            ok = self.transport.cec_volume(output_n, up)
        except Exception as e:
            log.error(f"[{self.cfg.id}] CEC volume {'up' if up else 'down'} output {output_n} failed: {e}")
            return False
        log.info(f"[{self.cfg.id}] CEC volume {'up' if up else 'down'} -> output {output_n}")
        return ok

    # -- health --
    def poll_health(self):
        try:
            d = self.transport.system_details()
        except Exception as e:
            log.debug(f"[{self.cfg.id}] health read failed: {e}")
            return
        prev = self.health.get("status")
        self.health = {"status": d.get("Status"), "message": d.get("StatusMessage"),
                       "version": d.get("Version"), "model": d.get("Model"), "serial": d.get("Serial")}
        if prev is not None and prev != self.health["status"]:
            log.warning(f"[{self.cfg.id}] matrix health changed: {self.health['message']}")
        self._publish_state()

    def run(self):
        next_power = next_health = 0.0
        while not self.stop_flag.is_set():
            self.poll_once()
            now = time.time()
            if self.connected and self.cfg.cec_power and now >= next_power:
                self.poll_power()
                next_power = now + self.cfg.power_poll_sec
            if self.connected and now >= next_health:
                self.poll_health()
                next_health = now + 60.0
            self.wake.wait(self.cfg.poll_interval_sec)
            self.wake.clear()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()

    # -- for the web UI --
    def snapshot(self) -> dict:
        labels = self.labels
        return {
            "id": self.cfg.id, "name": self.cfg.device_name, "host": self.cfg.host,
            "connected": self.connected, "failures": self.failures, "last_poll_ok": self.last_poll_ok,
            "poll_interval_sec": self.cfg.poll_interval_sec,
            "cec_power": self.cfg.cec_power, "cec_volume": self.cfg.cec_volume,
            "health": self.health,
            "inputs": [{
                "n": n, "label": labels[n], "device_name": self.device_names.get(n, ""),
                "status": self.inputs.get(n, {}).get("status"),
                "status_text": STATUS_TEXT.get(self.inputs.get(n, {}).get("status"), "unknown"),
            } for n in range(1, MATRIX_INPUTS + 1)],
            "outputs": [{
                "n": n, "exposed": n in self.cfg.outputs,
                "name": self.cfg.outputs[n].name if n in self.cfg.outputs else f"Output {n}",
                "id": self.cfg.outputs[n].id if n in self.cfg.outputs else None,
                "device_name": self.outputs.get(n, {}).get("name", ""),
                "status": self.outputs.get(n, {}).get("status"),
                "source": self.outputs.get(n, {}).get("source"),
                "power": self.power_state(n) if self.cfg.cec_power and n in self.cfg.outputs else None,
                "dps": (self.power.get(n) or {}).get("dps"),
            } for n in range(1, MATRIX_OUTPUTS + 1)],
        }
