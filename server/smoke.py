"""Small end-to-end check through the parent router; never print credentials."""
import http.client
import argparse
import hashlib
import json
import secrets
import os
from pathlib import Path

from healthcheck import healthy
from effort_bounds import allowed_efforts, validate_bounds
from local_runtime import STATE
from routing_policy import EFFORTS, POLICY_VERSION, SOL, TIERS


def main():
    parser = argparse.ArgumentParser(description="Verify a live Auto route")
    parser.add_argument("--model", choices=("jev/auto", "jev/auto-local", "jev/auto-cheap", "jev/auto-deep"), default="jev/auto")
    parser.add_argument("--expect-effort", choices=EFFORTS,
                        help="Require an exact effort for this trivial live request")
    parser.add_argument("--sol-effort-cap", choices=EFFORTS,
                        help="Maximum Sol effort, intersected with configured effort bounds")
    args = parser.parse_args()
    if not healthy():
        raise RuntimeError("local health check failed")
    health = http.client.HTTPConnection("127.0.0.1", 4319, timeout=5)
    try:
        health.request("GET", "/health")
        health_data = json.loads(health.getresponse().read())
        decision_provider = health_data.get("decision_provider", "jev")
        if decision_provider not in ("jev", "clef"):
            raise RuntimeError("unknown running decision provider")
        expected_provider = os.environ.get("JEV_DECISION_PROVIDER")
        if expected_provider and decision_provider != expected_provider:
            raise RuntimeError("running decision provider differs from the requested profile")
        if health_data.get("policy_version") != POLICY_VERSION:
            raise RuntimeError("running policy differs from checkout; reload required")
    finally:
        health.close()
    secret = (Path(STATE) / "caller-secret").read_text().strip()
    connection = http.client.HTTPConnection("127.0.0.1", 4202, timeout=120)
    result = {"policy": POLICY_VERSION, "requested_model": args.model, "model": None, "status": None}
    mode = args.model.split("/", 1)[1]
    sol_cap = args.sol_effort_cap or {"auto-cheap": "low", "auto-deep": "high"}.get(mode, "medium")
    output_text = []
    response_model = None
    smoke_key = "jev-smoke-" + secrets.token_hex(16)
    smoke_scope = hashlib.sha256(("prompt:" + smoke_key).encode()).hexdigest()[:16]
    try:
        body = {"model": args.model, "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "Reply only OK."}]}
        ], "prompt_cache_key": smoke_key, "stream": True}
        if decision_provider == "clef":
            body["reasoning"] = {"effort": sol_cap}
        connection.request("POST", f"/_codex-router/{secret}/v1/responses",
                           json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        result["http"] = response.status
        if response.status != 200:
            raise RuntimeError(f"parent router returned HTTP {response.status}")
        for line in response:
            if not line.startswith(b"data: "):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            if event.get("type") == "response.created":
                response_model = (event.get("response") or {}).get("model")
            if event.get("type") == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    output_text.append(delta)
            if event.get("type") == "response.completed":
                result["status"] = (event.get("response") or {}).get("status")
        if result["status"] != "completed":
            raise RuntimeError("no successful terminal response")
        route = None
        with (Path(STATE) / "jev-router-live.jsonl").open(encoding="utf-8") as log:
            for line in log:
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("cache_scope") == smoke_scope:
                    route = entry
        if (
            not isinstance(route, dict)
            or route.get("decision_source") != decision_provider
            or route.get("gate") not in ("apply", "astra_policy", "clef_sol_ceiling", "clef_effort_bounds")
            or route.get("status") != 200
            or route.get("completion_status") != "response.completed"
        ):
            raise RuntimeError("the request completed without a verified decision; check the provider and route")
        local_mode = mode == "auto-local"
        permitted = (health_data.get("local_execution_models", []) if local_mode
                     else health_data.get("allowed_execution_models", list(TIERS)))
        if route.get("model") not in permitted or route.get("effort") not in EFFORTS:
            raise RuntimeError("the Jev decision did not report a supported model and effort")
        if decision_provider == "clef" and (
            route.get("model") not in permitted
            or any(attempt.get("model") not in permitted
                   for attempt in route.get("attempts", []))
        ):
            raise RuntimeError("Clef attempted a model outside its execution ceiling")
        if decision_provider == "clef":
            if health_data.get("effort_policy") != "effort-bounds-v1":
                raise RuntimeError("The live adapter does not enforce configured effort bounds")
            bounds = validate_bounds(health_data.get("effort_bounds"))
            if (health_data.get("luna_efforts") != allowed_efforts("luna", bounds)
                    or sol_cap not in health_data.get("sol_effort_caps", [])
                    or route.get("auto_mode") != mode
                    or route.get("sol_effort_cap") != sol_cap
                    or route.get("effort_bounds") != bounds):
                raise RuntimeError("The live route did not apply the configured effort bounds")
            for attempt in [route, *route.get("attempts", [])]:
                effort = attempt.get("effort")
                executor = health_data.get("local_executor") or {}
                eligible = (executor.get("efforts", []) if local_mode
                            and attempt.get("model") == executor.get("model")
                            else allowed_efforts(attempt.get("model"), bounds, sol_cap))
                if effort not in eligible:
                    raise RuntimeError("Clef attempted reasoning outside its configured bounds")
        if args.expect_effort and route.get("effort") != args.expect_effort:
            raise RuntimeError("The live route did not select the expected reasoning effort")
        if route["model"] in TIERS and response_model != route["model"]:
            raise RuntimeError("the upstream response model did not match the Jev route")
        if route["model"] not in TIERS and not response_model:
            raise RuntimeError("the local response did not report its upstream model")
        expected_label = f"{route['model']} · reasoning: {route['effort']}"
        if expected_label not in "".join(output_text)[:240]:
            raise RuntimeError("the streamed assistant text did not show its routed model and effort")
        result.update({key: route.get(key) for key in (
            "model", "effort", "effort_transport", "gate", "decision_source", "auto_mode", "effort_policy", "sol_effort_cap", "effort_bounds"
        )})
        result["response_model"] = response_model
        result["visible_model_effort"] = "confirmed"
        print(json.dumps(result))
    finally:
        connection.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Transport exceptions can carry request URLs: expose only their class.
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        raise SystemExit(1)
