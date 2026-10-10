"""Local Clef decisions over a caller's verified native model/effort inventory."""
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from local_runtime import local_secret

MODES = {
    "auto-cheap": "Prefer low effort; increase when difficulty or failed checks warrant care.",
    "auto": "Balance care and cost using sufficient effort for the remaining work.",
    "auto-deep": "Prefer careful reasoning for difficult work; keep trivial work at low effort.",
}


class RoutingError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bounded(text, limit):
    text = str(text)
    if len(text) <= limit:
        return text
    half = (limit - 30) // 2
    return text[:half] + "\n[local routing excerpt]\n" + text[-half:]


def decision_body(task, candidates, mode="auto", context=None):
    if mode not in MODES:
        raise RoutingError("Unknown Auto mode")
    if not isinstance(task, str) or not task.strip():
        raise RoutingError("A nonempty task is required")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 80:
        raise RoutingError("A bounded native model inventory is required")
    seen = set()
    criteria, options = {}, {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise RoutingError("Invalid native model inventory")
        model, effort = candidate.get("model"), candidate.get("effort")
        if (not isinstance(model, str) or not model or len(model) > 160
                or effort not in (None, "low", "medium", "high", "xhigh", "max")):
            raise RoutingError("Invalid native model or effort")
        pair = model, effort
        if pair in seen:
            continue
        seen.add(pair)
        name = f"route_{len(options):02d}"
        options[name] = {"model": model, "effort": effort}
        description = bounded(candidate.get("description", "Available native model"), 400)
        criteria[name] = f"Model {model}; effort {effort or 'unsupported'}. {description}"
    state = {"task": bounded(task, 5000), "effort_preference": MODES[mode], "mode": mode}
    if context:
        # Context is evidence for this decision; only the native client gets full history.
        state["context"] = bounded(json.dumps(context, ensure_ascii=False, allow_nan=False), 4000)
    questions = {"route": {"type": "choice", "instructions": (
        "Select the least costly sufficient available native model and effort for remaining work. "
        "Model capability and effort are separate: do not choose a stronger model merely because "
        "the mode prefers deeper reasoning. " + MODES[mode] + " This is an effort preference, not "
        "a minimum or maximum; every listed route remains eligible in every mode. A model without "
        "an effort control is still eligible for simple work. Consider the task, active context, "
        "observed checks and cache costs when supplied. Transport, quota and permission failures "
        "alone do not establish reasoning failure. Confidence is diagnostic, not a dispatch gate."
    ), "criteria": criteria}}
    return {"required_decision_provider": "clef", "state": state, "questions": questions}, options


def decide(task, candidates, mode="auto", context=None):
    body, options = decision_body(task, candidates, mode, context)
    secret = local_secret()
    if not secret:
        raise RoutingError("Protected local decision credential unavailable")
    request = urllib.request.Request("http://127.0.0.1:4319/ask",
        data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=35) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise RoutingError("Invalid local decision")
        if data.get("decision_provider") != "clef":
            raise RoutingError("Local Clef adapter required")
        choice = data["answers"]["route"]["choice"]
        selected = options[choice]
    except urllib.error.HTTPError as error:
        error.close()
        raise RoutingError("Local Clef routing unavailable or invalid") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise RoutingError("Local Clef routing unavailable or invalid") from None
    return {**selected, "source": "clef", "decision_cache": data.get("decision_cache"),
            "usage": data.get("usage"), "ms": data.get("ms")}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", required=True,
                        help="Read a decision request from stdin; print only its selected route")
    parser.parse_args()
    try:
        request = json.loads(sys.stdin.buffer.read(1024 * 1024))
        selected = decide(request["task"], request["candidates"], request.get("mode", "auto"),
                          request.get("context"))
        print(json.dumps(selected))
    except (RoutingError, ValueError, TypeError, KeyError):
        print("Local Clef routing unavailable or invalid", file=sys.stderr)
        raise SystemExit(2)
