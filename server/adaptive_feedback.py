"""Bounded local route measurements and conservative tool-check attribution.

Only aggregate counters survive restart. Outcomes are observations of explicit
checks, not calibrated model success rates or instructions to change effort.
"""
from collections import OrderedDict
import ast
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import threading
from weakref import WeakValueDictionary

MODELS = ("gpt-6-luna", "gpt-6.1-sol")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_SCOPES, MAX_SEEN, MAX_BATCH, MAX_TEXT = 256, 64, 8, 8192
TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "reasoning_tokens")
TOOL_OUTPUTS = ("function_call_output", "custom_tool_call_output")
TOOL_CALLS = ("function_call", "custom_tool_call")
INFRA = re.compile(r"timed?\s*out|timeout(?:error)?|command not found|permission denied|"
                   r"connection refused|network is unreachable|no such file or directory|"
                   r"no module named ['\"]?(?:pytest|unittest|ruff|mypy)|"
                   r"npm\s+(?:err!|error)\s+code\s+(?:enoent|enotfound|econnrefused)", re.I)
EXIT = re.compile(r"Process exited with code\s+(-?\d+)\b", re.I)
FAIL = re.compile(r"^\s*=*\s*(?:\d+\s+\w+,\s*)*[1-9]\d*\s+(?:failed|errors?)\b|^FAILED(?:\s*\(|\s+\S+::|\s*$)|"
                  r"^#\s*fail\s+[1-9]\d*\b|\b(?:failures|errors)=[1-9]\d*|"
                  r"\berror TS\d+\b|^\s*(?:tests?|lint|typecheck|smoke)\s+failed\b", re.I | re.M)
PASS = re.compile(r"^\s*=*\s*(?:\d+\s+\w+,\s*)*[1-9]\d*\s+passed\b|^#\s*pass\s+[1-9]\d*\b|"
                  r"Ran\s+[1-9]\d*\s+tests?\b[\s\S]*?^OK\s*$|"
                  r"^\s*All checks passed\b|^\s*(?:tests?|lint|typecheck|smoke)\s+passed\b", re.I | re.M)
_stores, _stores_lock = OrderedDict(), threading.Lock()


def _text(value):
    if not isinstance(value, str):
        return ""
    return value[:MAX_TEXT].encode("utf-8", "replace")[:MAX_TEXT].decode("utf-8", "ignore")


def _output(value, depth=0, nested=False):
    """Read supported exec envelopes without stringifying unrelated fields."""
    if depth > 3:
        return "", None, False
    if isinstance(value, str):
        text = _text(value)
        if text.lstrip().startswith("{"):
            try:
                return _output(json.loads(text), depth + 1, nested)
            except (ValueError, TypeError):
                pass
        envelope = re.search(r"(?:^|\n)(?:Output|Final output):\s*\n?(\{[\s\S]+\})\s*$", text)
        if envelope:
            prefix = text[:envelope.start()].strip().splitlines()
            if all(re.match(r"^(?:Script completed|Wall time|Process exited|Chunk ID|Exit code|Session ID)\b", line) for line in prefix):
                try:
                    return _output(json.loads(envelope[1]), depth + 1, nested)
                except (ValueError, TypeError):
                    pass
        match = EXIT.search(text)
        return text, int(match[1]) if match else None, False
    if isinstance(value, dict):
        text, code, structured = _output(value.get("output", value.get("text")), depth + 1, nested)
        explicit = value.get("exit_code")
        if isinstance(explicit, int) and not isinstance(explicit, bool):
            if nested and structured:
                return text, code, True
            return text, explicit, True
        return text, code, structured
    return "", None, False


def _wrapped_command(source):
    """Lex a literal awaited exec call; quoted JS and dynamic commands stay opaque."""
    source = _text(source)
    tokens, position = [], 0
    while position < len(source):
        char = source[position]
        if char.isspace():
            position += 1
            continue
        if source.startswith("//", position):
            end = source.find("\n", position)
            position = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", position):
            end = source.find("*/", position + 2)
            if end < 0:
                return ""
            position = end + 2
            continue
        if char in "\"'`/":
            start, quote = position, char
            position += 1
            while position < len(source):
                if source[position] == "\\":
                    position += 2
                elif source[position] == quote:
                    position += 1
                    break
                else:
                    position += 1
            else:
                return ""
            literal = source[start:position]
            value = None
            if quote in "\"'":
                try:
                    value = json.loads(literal) if quote == '"' else ast.literal_eval(literal)
                except (ValueError, SyntaxError):
                    return ""
            tokens.append(("string", value))
            continue
        match = re.match(r"[A-Za-z_$][\w$]*", source[position:])
        if match:
            tokens.append(("word", match[0]))
            position += len(match[0])
        else:
            tokens.append(("punct", char))
            position += 1
    if any(value in ("function", "eval") for kind, value in tokens if kind == "word"):
        return ""
    if any(tokens[index:index + 2] == [("punct", "="), ("punct", ">")] for index in range(len(tokens) - 1)):
        return ""
    found = []
    pattern = [("word", "tools"), ("punct", "."), ("word", "exec_command"), ("punct", "("), ("punct", "{")]
    for index in range(len(tokens) - 4):
        if tokens[index:index + 5] != pattern:
            continue
        if index == 0 or tokens[index - 1] != ("word", "await"):
            return ""
        depth, commands = 1, []
        for offset in range(index + 5, len(tokens)):
            kind, value = tokens[offset]
            if depth == 1 and (tokens[offset:offset + 3] == [("punct", ".")] * 3 or (kind, value) == ("punct", "[")):
                return ""
            if depth == 1 and value == "cmd" and kind in ("word", "string"):
                rest = tokens[offset + 1:offset + 3]
                if (len(rest) != 2 or rest[0] != ("punct", ":") or rest[1][0] != "string"
                        or not isinstance(rest[1][1], str)
                        or tokens[offset + 3:offset + 4] not in ([("punct", ",")], [("punct", "}")])):
                    return ""
                commands.append(rest[1][1])
            if kind == "punct" and value in ("{", "["):
                depth += 1
            elif kind == "punct" and value in ("}", "]"):
                depth -= 1
                if depth == 0:
                    break
        else:
            return ""
        if len(commands) != 1:
            return ""
        found.append(commands[0])
    return found[0] if len(found) == 1 else ""


def _command(item):
    arguments = item.get("arguments", item.get("input"))
    if isinstance(arguments, str):
        arguments = _text(arguments)
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = arguments
        arguments = parsed
    if isinstance(arguments, dict):
        command = arguments.get("cmd", arguments.get("command"))
        if isinstance(command, str):
            return _text(command), False
        return _wrapped_command(arguments.get("code", arguments.get("input"))), True
    if isinstance(arguments, str):
        return (_wrapped_command(arguments), True) if "tools.exec_command" in arguments else (arguments, False)
    return "", False


def _check(command, depth=0):
    """Recognize executable checks, never a check name merely inside a quote."""
    if depth > 2 or not command:
        return [], False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = list(lexer)
    except ValueError:
        return [], False
    segments, current = [], []
    for token in tokens:
        if token in ("&&", "||", ";", "|", "&"):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    kinds, nested_safe = [], True
    for words in segments:
        while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
            words = words[1:]
        if not words:
            continue
        executable = Path(words[0]).name
        args = words[1:]
        if executable == "env":
            nested, safe = _check(shlex.join(args), depth + 1)
            kinds.extend(nested)
            nested_safe = nested_safe and safe
            continue
        if executable in ("bash", "zsh", "sh") and len(args) == 2 and args[0] in ("-c", "-lc"):
            nested, safe = _check(args[1], depth + 1)
            kinds.extend(nested)
            nested_safe = nested_safe and safe
            continue
        if any(arg in ("--help", "--version", "-h", "-V", "--collect-only", "--listTests",
                       "--list-tests", "--showConfig", "--listFilesOnly") for arg in args):
            continue
        kind = None
        if executable in ("pytest", "py.test"):
            kind = "test"
        elif re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable):
            if args[:2] in (["-m", "pytest"], ["-m", "unittest"]):
                kind = "test"
            elif args and re.fullmatch(r"(?:test_[^/]+|[^/]+_test)\.py", Path(args[0]).name):
                kind = "test"
            elif args and Path(args[0]).name == "smoke.py":
                kind = "smoke"
        elif executable in ("node", "nodejs") and "--test" in args:
            kind = "test"
        elif executable in ("npm", "pnpm", "yarn", "bun"):
            action = args[1] if len(args) > 1 and args[0] in ("run", "run-script") else (args[0] if args else "")
            kind = {"test": "test", "lint": "lint", "typecheck": "typecheck", "smoke": "smoke"}.get(action.split(":")[0])
        elif executable == "npx":
            nested, safe = _check(shlex.join(args), depth + 1)
            kinds.extend(nested)
            nested_safe = nested_safe and safe
        elif executable in ("mypy", "pyright") or (executable == "tsc" and "--noEmit" in args
                                                    and args[args.index("--noEmit") + 1:args.index("--noEmit") + 2] != ["false"]):
            kind = "typecheck"
        elif executable in ("eslint", "flake8", "pylint") or executable == "ruff" and args[:1] == ["check"]:
            kind = "lint"
        elif executable in ("black", "prettier") and "--check" in args:
            kind = "lint"
        elif executable in ("cargo", "go", "swift", "make") and args[:1] == ["test"]:
            kind = "test"
        if kind:
            kinds.append(kind)
    # A shell pipeline or later command can hide a check's exit code. Its
    # explicit summary is still useful, but its overall exit code is not.
    trusted_exit = nested_safe and len(segments) == 1 and len(kinds) == 1
    return sorted(set(kinds)), trusted_exit


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e12
            and (not isinstance(value, float) or math.isfinite(value)))


def _add(row, field, value=1):
    row[field] = min(10 ** 12, row[field] + value)


def _row(model, effort):
    return {"model": model, "effort": effort, "samples": 0, "usage_samples": 0,
            "latency_samples": 0, "latency_ms_total": 0,
            "token_totals": {field: 0 for field in TOKEN_FIELDS},
            "token_samples": {field: 0 for field in TOKEN_FIELDS},
            "verified_passes": 0, "verified_failures": 0}


class FeedbackStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "adaptive-feedback.json"
        self.lock = threading.RLock()
        self.handoffs = WeakValueDictionary()
        self.scopes, self.statistics = OrderedDict(), {}
        self.persistence_error = False
        self._load()

    @contextmanager
    def handoff(self, scope):
        """Order one stable scope's observe/relay/record without locking others."""
        if scope is None:
            yield
            return
        key = hashlib.sha256(_text(str(scope)).encode()).hexdigest()
        with self.lock:
            lock = self.handoffs.get(key)
            if lock is None:
                lock = threading.RLock()
                self.handoffs[key] = lock
        # Keep a strong reference while waiting and throughout the relay. The
        # global aggregate lock is released before any native request occurs.
        with lock:
            yield

    def _scope(self, scope):
        key = hashlib.sha256(_text(str(scope)).encode()).hexdigest()
        if key not in self.scopes:
            self.scopes[key] = {"pending": None, "seen": OrderedDict(),
                                "recent": None, "failures": 0}
        self.scopes.move_to_end(key)
        while len(self.scopes) > MAX_SCOPES:
            self.scopes.popitem(last=False)
        return self.scopes[key]

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                    raise ValueError("unprotected feedback")
                data = json.loads(source.read(32769))
            if data.get("version") != 1 or not isinstance(data.get("route_statistics"), list):
                raise ValueError("unsupported feedback")
            for row in data["route_statistics"][:10]:
                if not isinstance(row, dict) or row.get("model") not in MODELS or row.get("effort") not in EFFORTS:
                    continue
                clean = _row(row["model"], row["effort"])
                for field in ("samples", "usage_samples", "latency_samples", "latency_ms_total", "verified_passes", "verified_failures"):
                    if _number(row.get(field)) and (field == "latency_ms_total" or isinstance(row[field], int)):
                        clean[field] = row[field]
                for group in ("token_totals", "token_samples"):
                    values = row.get(group)
                    if isinstance(values, dict):
                        for field in TOKEN_FIELDS:
                            if isinstance(values.get(field), int) and _number(values[field]):
                                clean[group][field] = values[field]
                self.statistics[(clean["model"], clean["effort"])] = clean
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self.persistence_error = True

    def _save(self):
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".adaptive-feedback-", dir=self.path.parent)
            with os.fdopen(fd, "w") as destination:
                os.fchmod(destination.fileno(), 0o600)
                json.dump({"version": 1, "route_statistics": list(self.statistics.values())}, destination,
                          separators=(",", ":"), allow_nan=False)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            self.persistence_error = False
        except (OSError, ValueError, TypeError):
            self.persistence_error = True
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def record(self, scope, model, effort, usage, latency_ms, completion):
        if model not in MODELS or effort not in EFFORTS:
            return
        with self.lock:
            row = self.statistics.setdefault((model, effort), _row(model, effort))
            _add(row, "samples")
            if _number(latency_ms):
                _add(row, "latency_samples")
                _add(row, "latency_ms_total", latency_ms)
            observed = False
            if isinstance(usage, dict):
                usage = {**usage}
                for group, source, target in (("input_tokens_details", "cached_tokens", "cached_input_tokens"),
                                              ("input_tokens_details", "cache_write_tokens", "cache_write_input_tokens"),
                                              ("output_tokens_details", "reasoning_tokens", "reasoning_tokens")):
                    details = usage.get(group)
                    if isinstance(details, dict) and source in details:
                        usage[target] = details[source]
                for field in TOKEN_FIELDS:
                    value = usage.get(field)
                    if isinstance(value, int) and _number(value):
                        _add(row["token_totals"], field, value)
                        _add(row["token_samples"], field)
                        observed = True
            _add(row, "usage_samples", int(observed))
            if scope is not None:
                state = self._scope(scope)
                state["pending"] = {"model": model, "effort": effort} if completion in ("completed", "response.completed") else None
            self._save()

    def observe(self, scope, payload):
        if scope is None:
            return None
        items = payload.get("input") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return None
        batch = []
        for index in range(len(items) - 1, max(-1, len(items) - MAX_BATCH - 1), -1):
            item = items[index]
            if not isinstance(item, dict) or item.get("type") not in TOOL_OUTPUTS:
                break
            batch.append((index, item))
        if not batch:
            return None
        ids = {item.get("call_id") for _, item in batch if isinstance(item.get("call_id"), str)}
        calls = {}
        start = batch[-1][0]
        for item in reversed(items[max(0, start - 512):start]):
            if (isinstance(item, dict) and item.get("type") in TOOL_CALLS
                    and isinstance(item.get("call_id"), str) and item["call_id"] in ids):
                calls.setdefault(item["call_id"], item)
        with self.lock:
            state = self._scope(scope)
            results, kinds = [], set()
            for _, item in reversed(batch):
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or call_id not in calls:
                    continue
                command, wrapped = _command(calls[call_id])
                check_kinds, trusted_exit = _check(command)
                if not check_kinds:
                    continue
                text, code, structured = _output(item.get("output"), nested=wrapped)
                fingerprint = hashlib.sha256((_text(call_id) + "\0" + str(code) + "\0" + text).encode()).hexdigest()
                if fingerprint in state["seen"]:
                    continue
                state["seen"][fingerprint] = True
                while len(state["seen"]) > MAX_SEEN:
                    state["seen"].popitem(last=False)
                if (INFRA.search(text) or code in (124, 126, 127, 130, 137, 143)
                        or re.search(r"no tests ran|Ran\s+0\s+tests|collected\s+0\s+items|^#\s*tests\s+0\b", text, re.I | re.M)):
                    continue
                failed, passed = bool(FAIL.search(text)), bool(PASS.search(text))
                if structured and trusted_exit and code is not None:
                    failed = failed or code != 0
                    passed = passed or code == 0
                elif code is not None and (failed or passed):
                    failed = failed or code != 0
                else:
                    continue
                results.append("fail" if failed else "pass")
                kinds.update(check_kinds)
            if not results:
                return None
            outcome = "fail" if "fail" in results else "pass"
            route = state["pending"]
            summary = {"outcome": outcome, "checks": len(results), "passed_checks": results.count("pass"),
                       "failed_checks": results.count("fail"), "check_kinds": sorted(kinds),
                       "attributed_route": route}
            state["pending"] = None
            state["recent"] = summary
            state["failures"] = min(32, state["failures"] + 1) if outcome == "fail" else 0
            if route:
                row = self.statistics[(route["model"], route["effort"])]
                _add(row, "verified_failures" if outcome == "fail" else "verified_passes")
                self._save()
            return json.loads(json.dumps(summary))

    def context(self, scope):
        with self.lock:
            state = self._scope(scope) if scope is not None else None
            context = {"recent_verification": state["recent"] if state else None,
                       "consecutive_failures": state["failures"] if state else 0,
                       "route_statistics": list(self.statistics.values())}
            if self.persistence_error:
                context["persistence_error"] = True
            return json.loads(json.dumps(context))


def feedback_for(state_dir):
    path = os.path.abspath(os.fspath(state_dir))
    with _stores_lock:
        if path not in _stores:
            _stores[path] = FeedbackStore(path)
        _stores.move_to_end(path)
        while len(_stores) > 16:
            _stores.popitem(last=False)
        return _stores[path]
