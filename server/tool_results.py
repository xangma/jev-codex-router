"""Bounded tool-result inspection; process status outranks words in stdout."""
import json
import math
import re

_MAX_DEPTH = 8
_MAX_NODES = 128
_MAX_TEXT = 8192
# Matches the relay's response-body ceiling. Status may follow a large stdout
# field, so truncating JSON before decoding loses authoritative exit metadata.
_MAX_JSON = 64 * 1024 * 1024
_UNKNOWN, _CLEAN, _FAILED, _RUNNING, _DIAGNOSTIC_FAILURE = range(5)
_EXIT = re.compile(r"^\s*Process exited with code ([+-]?\d+)\s*$", re.M)
_RUN = re.compile(r"^\s*(?:Process running with session ID|Script running with cell ID)\s+\S+", re.M)
_OUTPUT = re.compile(r"(?:^|\n)(?:Output|Final output):[ \t]*\n?")
_PREFIX = re.compile(r"^(?:Script completed|Wall time|Chunk ID|Process exited|Exit code|Session ID)\b")
_DIAGNOSTIC = re.compile(
    r"^\s*(?:Traceback \(most recent call last\):|FAILED\b|"
    r"(?:ERROR|FAIL|FATAL|PANIC)(?:\s*:|\s+)|"
    r"(?:tests?|lint|typecheck|smoke)\s+failed\b|[\w.]*(?:Error|Exception)\s*:)",
    re.I | re.M,
)
_FIELDS = ("output", "stdout", "stderr", "text", "content", "result", "value")
_STRUCTURED = ("structuredContent", "structured_content")


def _exit_code(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _bounded(text, limit=_MAX_TEXT):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n" + text[-half:]


def _json_value(text):
    if len(text) > _MAX_JSON:
        return None
    if not text.lstrip().startswith(("{", "[", '"')):
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _wrapped_json(text):
    marker = _OUTPUT.search(text[:_MAX_TEXT])
    if marker is None:
        return None
    prefix = [line.strip() for line in text[:marker.start()].splitlines() if line.strip()]
    if not prefix or not all(_PREFIX.match(line) for line in prefix):
        return None
    if len(text) - marker.end() > _MAX_JSON:
        return None
    return _json_value(text[marker.end():].strip())


def _running(value):
    if value.get("status") in ("running", "in_progress", "pending", "queued"):
        return True
    for key in ("session_id", "cell_id"):
        item = value.get(key)
        if (isinstance(item, str) and item.strip()) or (type(item) is int and item > 0):
            return True
    return False


def _combined(statuses):
    for status in (_FAILED, _CLEAN, _RUNNING, _DIAGNOSTIC_FAILURE):
        if status in statuses:
            return status
    return _UNKNOWN


def _inspect(value, remaining, depth=0):
    if depth > _MAX_DEPTH or remaining[0] <= 0:
        return _UNKNOWN
    remaining[0] -= 1
    if isinstance(value, dict):
        if value.get("isError") is True:
            return _FAILED
        code = _exit_code(value.get("exit_code"))
        if code is not None:
            return _FAILED if code else _CLEAN
        statuses = [_inspect(value[key], remaining, depth + 1)
                    for key in _STRUCTURED if key in value]
        structured = _combined(statuses)
        if structured in (_CLEAN, _FAILED, _RUNNING):
            return structured
        if _running(value):
            return _RUNNING
        statuses += [_inspect(value[key], remaining, depth + 1)
                     for key in _FIELDS if key in value]
        combined = _combined(statuses)
        if combined in (_CLEAN, _FAILED, _RUNNING):
            return combined
        return _CLEAN if value.get("isError") is False else combined
    if isinstance(value, (list, tuple)):
        statuses = [_inspect(item, remaining, depth + 1) for item in value[:_MAX_NODES]]
        return _combined(statuses)
    if isinstance(value, str):
        decoded = _json_value(value)
        if decoded is None:
            decoded = _wrapped_json(value)
        if decoded is not None:
            return _inspect(decoded, remaining, depth + 1)
        text = _bounded(value)
        # Native exec metadata precedes stdout; printed source/log lines inside
        # Final output must not override the actual process exit status.
        metadata = text.split("Final output:", 1)[0]
        codes = []
        for match in _EXIT.findall(metadata):
            if len(match.lstrip("+-")) <= 12:
                codes.append(int(match))
        if codes:
            return _FAILED if any(codes) else _CLEAN
        if _RUN.search(metadata):
            return _RUNNING
        return _DIAGNOSTIC_FAILURE if _DIAGNOSTIC.search(text) else _UNKNOWN
    return _UNKNOWN


def tool_result_failed(value):
    """Recognize exec/MCP failures without treating quoted error words as errors."""
    return _inspect(value, [_MAX_NODES]) in (_FAILED, _DIAGNOSTIC_FAILURE)


def output_text(value, limit=4000):
    """Extract a bounded text preview without serializing arbitrary dictionaries."""
    limit = max(0, min(int(limit), _MAX_TEXT))
    parts, remaining = [], [_MAX_NODES, limit]

    def collect(item, depth=0):
        if depth > _MAX_DEPTH or remaining[0] <= 0 or remaining[1] <= 0:
            return
        remaining[0] -= 1
        if isinstance(item, str):
            decoded = _json_value(item)
            if decoded is None:
                decoded = _wrapped_json(item)
            if isinstance(decoded, (dict, list)):
                before = remaining[1]
                collect(decoded, depth + 1)
                if remaining[1] != before:
                    return
            text = item[-remaining[1]:]
            parts.append(text)
            remaining[1] -= len(text)
        elif isinstance(item, dict):
            for key in _STRUCTURED + _FIELDS:
                if key in item:
                    collect(item[key], depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item[:_MAX_NODES]:
                collect(child, depth + 1)

    collect(value)
    return "\n".join(parts)[:limit]
