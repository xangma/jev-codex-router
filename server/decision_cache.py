"""Private, bounded exact-result reuse for deterministic local classification."""
from collections import OrderedDict
import configparser
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from weakref import WeakValueDictionary


def model_version(model, preset=None):
    """Invalidate managed Clef decisions when its preset or GGUF is replaced.

    Unknown model files disable reuse. No model contents or prompts are read.
    """
    path = Path(preset or Path.home() / ".local/share/clef/models.ini")
    try:
        config = configparser.ConfigParser(interpolation=None)
        # llama.cpp presets allow global fields before the first section.
        config.read_string("[__preset__]\n" + path.read_text())
        source = Path(config[model]["model"]).expanduser()
        if not source.is_file():
            return None
        return tuple((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                     for info in (path.stat(), source.stat()))
    except (OSError, UnicodeError, KeyError, configparser.Error):
        return None


def _cacheable(result, questions):
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        return False
    answers = result["answers"]
    if not questions or set(answers) != set(questions):
        return False
    for name, question in questions.items():
        answer = answers[name]
        criteria = question.get("criteria") if isinstance(question, dict) else None
        if (not isinstance(answer, dict) or not isinstance(answer.get("choice"), str)
                or not isinstance(criteria, dict) or answer["choice"] not in criteria):
            return False
    if not isinstance(result.get("usage", {}), dict):
        return False
    try:
        return len(json.dumps(result, allow_nan=False).encode()) <= 65536
    except (TypeError, ValueError):
        return False


class DecisionCache:
    def __init__(self, ttl=60, capacity=128, clock=time.monotonic):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self._entries = OrderedDict()
        self._guards = WeakValueDictionary()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def statistics(self):
        with self._lock:
            return {"kind": "exact_result", "ttl_seconds": self.ttl,
                    "max_entries": self.capacity, "entries": len(self._entries),
                    "hits": self.hits, "misses": self.misses}

    def call(self, endpoint, credential, payload, loader, timeout, version):
        if version is None:
            return loader(timeout)
        # Native Clef preserves object order while constructing its prompt.
        data = json.dumps([endpoint, credential, version, payload],
                          separators=(",", ":"), allow_nan=False).encode()
        key = hashlib.sha256(data).digest()
        start = self.clock()
        with self._lock:
            guard = self._guards.get(key)
            if guard is None:
                guard = threading.Lock()
                self._guards[key] = guard
        if not guard.acquire(timeout=max(0, timeout)):
            raise TimeoutError("local decision cache wait timed out")
        try:
            now = self.clock()
            remaining = timeout - (now - start)
            if remaining <= 0:
                raise TimeoutError("local decision cache deadline expired")
            with self._lock:
                entry = self._entries.pop(key, None)
                if entry is not None and now - entry[0] < self.ttl:
                    self._entries[key] = entry
                    self.hits += 1
                    answer = copy.deepcopy(entry[1])
                    usage = answer.get("usage") or {}
                    saved = usage.get("input_tokens")
                    saved = saved if type(saved) is int and saved >= 0 else 0
                    answer["usage"] = {"input_tokens": 0, "output_tokens": 0}
                    answer["decision_cache"] = {"kind": "exact_result", "hit": True,
                                                "saved_input_tokens": saved}
                    return answer
                self.misses += 1
            result = loader(remaining)
            if _cacheable(result, payload.get("questions")):
                stored = copy.deepcopy(result)
                with self._lock:
                    self._entries[key] = (self.clock(), stored)
                    while len(self._entries) > self.capacity:
                        self._entries.popitem(last=False)
                result = copy.deepcopy(result)
                result["decision_cache"] = {"kind": "exact_result", "hit": False,
                                            "saved_input_tokens": 0}
            return result
        finally:
            guard.release()
