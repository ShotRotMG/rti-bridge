"""
Factory-reset detection for RTI AD-8x amps (warn only - never sends commands).

Upstream (srhunt-cyber 2.0.x) saw AD-8x amps come back from power events with every
zone at factory defaults. A fixed "defaults" signature alone would false-alarm whenever
zones happen to be flat, so a reset is only suspected when, on a full poll:

  * at least `min_zones` zones (default all 8) have bass 0 and treble 0,
  * and those zones all share the SAME source and volume,
  * and at least `min_changed` of them differ from the last known settings,
  * and the last known settings were not already uniform,

for `confirmation_polls` polls in a row. Last known settings are saved next to
config.yaml (amp_state.json) so they survive restarts - and so you can see what
each zone was set to before the reset.
"""
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, Optional

log = logging.getLogger("rti_reset")

SAVE_EVERY_SEC = 30.0


def _key(st: dict) -> Optional[list]:
    if any(st.get(k) is None for k in ("source", "vol_0_75", "bass", "treble")):
        return None
    return [int(st["source"]), int(st["vol_0_75"]), int(st["bass"]), int(st["treble"])]


class ResetDetector:
    def __init__(self, path: str, cfg: dict, publish: Callable[[str, dict], None]):
        self.path = path
        self.enabled = bool(cfg.get("enabled", True))
        self.min_zones = int(cfg.get("min_zones", 8))
        self.min_changed = int(cfg.get("min_changed", 3))
        self.confirm = int(cfg.get("confirmation_polls", 2))
        self.publish = publish            # (amp_id, status dict) -> None
        self.lock = threading.Lock()
        self.saved: Dict[str, Dict[str, list]] = {}   # amp -> {"1": [src, att, bass, treble]}
        self.status: Dict[str, dict] = {}
        self._polls: Dict[str, int] = {}
        self._dirty = False
        self._last_write = 0.0
        self._load()

    # -- persistence --
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.saved = data.get("zones", {})
            self.status = data.get("status", {})
            log.info(f"[reset] loaded last known settings for {', '.join(self.saved) or 'no amps'} from {self.path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"[reset] could not read {self.path}: {e}")

    def _write(self, force: bool = False):
        if not self._dirty or (not force and time.time() - self._last_write < SAVE_EVERY_SEC):
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"zones": self.saved, "status": self.status}, f, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_write = time.time()
        except Exception as e:
            log.warning(f"[reset] could not save {self.path}: {e}")

    # -- evaluation (called after every complete 8-zone poll) --
    def evaluate(self, amp: str, zone_states: Dict[int, dict]):
        if not self.enabled:
            return
        cur = {str(z): _key(st) for z, st in zone_states.items()}
        cur = {z: v for z, v in cur.items() if v}
        if len(cur) < self.min_zones:
            return
        with self.lock:
            prev = self.saved.get(amp)
            st = self.status.get(amp) or {}
            flat = [z for z, v in cur.items() if v[2] == 0 and v[3] == 0]
            uniform = len(flat) >= self.min_zones and len({(cur[z][0], cur[z][1]) for z in flat}) == 1
            changed = sum(1 for z in flat if prev and z in prev and prev[z] != cur[z])
            prev_varied = bool(prev) and len({tuple(v) for v in prev.values()}) > 1
            candidate = uniform and prev_varied and changed >= self.min_changed

            if st.get("suspected"):
                if not uniform:  # zones were set up again - clear and resume tracking
                    log.info(f"[reset] {amp}: zones no longer match the reset pattern - clearing warning")
                    self._set(amp, {"suspected": False, "cleared": time.time(), "reason": "settings changed"})
                    self.saved[amp] = cur
                    self._dirty = True
                self._write()
                return

            if candidate:
                self._polls[amp] = self._polls.get(amp, 0) + 1
                log.warning(f"[reset] {amp}: possible factory reset - {len(flat)} zones flat and identical, "
                            f"{changed} changed ({self._polls[amp]}/{self.confirm} polls)")
                if self._polls[amp] >= self.confirm:
                    log.error(f"[reset] {amp}: FACTORY RESET SUSPECTED - nothing was sent to the amp. "
                              f"Previous settings are kept in {self.path}")
                    self._set(amp, {"suspected": True, "since": time.time(),
                                    "now": cur[flat[0]], "before": prev})
                    self._write(force=True)
                return

            self._polls[amp] = 0
            if prev != cur:
                self.saved[amp] = cur
                self._dirty = True
            self._write()

    def acknowledge(self, amp: str, zone_states: Dict[int, dict]):
        """User says it's fine: accept current settings as the new baseline."""
        with self.lock:
            cur = {str(z): _key(st) for z, st in zone_states.items()}
            cur = {z: v for z, v in cur.items() if v}
            if cur:
                self.saved[amp] = cur
            self._polls[amp] = 0
            self._set(amp, {"suspected": False, "cleared": time.time(), "reason": "dismissed"})
            self._dirty = True
            self._write(force=True)
        log.info(f"[reset] {amp}: warning dismissed; current settings saved as the new baseline")

    def _set(self, amp: str, status: dict):
        self.status[amp] = status
        self._dirty = True
        try:
            self.publish(amp, status)
        except Exception as e:
            log.warning(f"[reset] publish failed: {e}")

    def get(self, amp: str) -> dict:
        return dict(self.status.get(amp) or {"suspected": False})

    def flush(self):
        with self.lock:
            self._write(force=True)
