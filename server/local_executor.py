"""Portable, opt-in executor settings for Auto (Clef Local)."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from local_runtime import STATE
from routing_policy import EFFORTS, NATIVE_MODELS

MAX_FILE_BYTES = 16384
DEFAULT_EXECUTOR = {
    "name": "Local executor",
    "description": "A configured open-weight coding model for clear, bounded work.",
    "efforts": ["medium", "high"],
    "context_window": 131072,
}


def executor_path(path=None):
    return Path(path or os.environ.get("CLEF_LOCAL_EXECUTOR_FILE")
                or Path(STATE) / "clef-local-executor.json").expanduser()


def _text(value, field, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError(f"Local executor {field} must be bounded text without control characters")
    return value.strip()


def validate_executor(value):
    if not isinstance(value, dict) or set(value) - {"model", *DEFAULT_EXECUTOR}:
        raise ValueError("Local executor settings contain unsupported fields")
    model = _text(value.get("model"), "model", 256)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}", model):
        raise ValueError("Local executor model must be a configured provider/model route")
    provider, upstream = model.split("/", 1)
    if (provider in {"jev", "openai", "native", "chatgpt"}
            or upstream in NATIVE_MODELS or upstream.startswith("gpt-")):
        raise ValueError("Local executor must use a separate non-native model provider")
    result = {**DEFAULT_EXECUTOR, **value, "model": model}
    result["name"] = _text(result["name"], "name", 120)
    result["description"] = _text(result["description"], "description", 1200)
    efforts = result["efforts"]
    if (not isinstance(efforts, list) or not 1 <= len(efforts) <= len(EFFORTS)
            or any(not isinstance(effort, str) or effort not in EFFORTS for effort in efforts)
            or len(set(efforts)) != len(efforts)):
        raise ValueError("Local executor efforts must be distinct supported reasoning efforts")
    result["efforts"] = [effort for effort in EFFORTS if effort in efforts]
    window = result["context_window"]
    if not isinstance(window, int) or isinstance(window, bool) or not 1024 <= window <= 10000000:
        raise ValueError("Local executor context window must be an integer from 1024 to 10000000")
    return result


def load_executor(path=None):
    try:
        with executor_path(path).open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Cannot read local executor settings") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Local executor settings file is too large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Local executor settings must be valid JSON") from None
    return validate_executor(value)


def write_executor(value, path=None):
    value = validate_executor(value)
    target = executor_path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".clef-local-executor-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Use a different local executor settings file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    change.add_argument("--model", required=True, help="An existing configured provider/model route")
    change.add_argument("--name")
    change.add_argument("--description")
    change.add_argument("--efforts", help="Comma-separated supported efforts (default: medium,high)")
    change.add_argument("--context-window", type=int)
    commands.add_parser("disable", help="Remove the executor settings; keep the provider intact")
    args = parser.parse_args(argv)
    try:
        if args.command == "disable":
            executor_path(args.file).unlink(missing_ok=True)
            value = None
        elif args.command == "set":
            value = {"model": args.model}
            for name in ("name", "description", "context_window"):
                if getattr(args, name) is not None:
                    value[name] = getattr(args, name)
            if args.efforts is not None:
                value["efforts"] = args.efforts.split(",")
            value = write_executor(value, args.file)
        else:
            value = load_executor(args.file)
        print(json.dumps({"file": str(executor_path(args.file)), "executor": value}))
        if args.command in {"set", "disable"}:
            print("Publish picker changes: JEV_DECISION_PROVIDER=clef node server/configure-model.mjs; "
                  "then bin/jev-codex-router router refresh-catalog", file=sys.stderr)
        return 0
    except (ValueError, OSError):
        print("Invalid or unreadable local executor settings", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
