"""
RTI VHD-8x (Pulse-Eight OEM) 8x10 HDBaseT matrix.

Transport is kept separate from the session so a serial (RS-232) transport can be
dropped in later with the same two methods: list_ports() and route().

HTTP API (unauthenticated, all GET, bays are 0-based):
  /Port/List                  -> {"Result": true, "Ports": [{Bay, Mode, Type, Status, Name, ReceiveFrom?}]}
  /Port/Set/{inBay}/{outBay}  -> {"Result": true}
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
    def __init__(self, host: str, port: int = 80, timeout: float = 3.0):
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

    def run(self):
        while not self.stop_flag.is_set():
            self.poll_once()
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
            } for n in range(1, MATRIX_OUTPUTS + 1)],
        }
