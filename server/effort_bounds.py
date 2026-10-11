"""Editable Clef effort ranges, read afresh for each routing request."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from local_runtime import STATE
from routing_policy import EFFORTS, LUNA, SOL

DEFAULT_BOUNDS = {
    "luna": {"min": "medium", "max": "max"},
    "sol": {"min": "low", "max": "max"},
}


def bounds_path(path=None):
    return Path(path or os.environ.get("CLEF_EFFORT_BOUNDS_FILE")
                or Path(STATE) / "clef-effort-bounds.json").expanduser()


def validate_bounds(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BOUNDS):
        raise ValueError("Clef effort bounds must contain only luna and sol ranges")
    result = {}
    for model, defaults in DEFAULT_BOUNDS.items():
        entry = value.get(model, {})
        if not isinstance(entry, dict) or set(entry) - {"min", "max"}:
            raise ValueError("Clef effort ranges accept only min and max")
        entry = {**defaults, **entry}
        if any(not isinstance(effort, str) or effort not in EFFORTS
               for effort in entry.values()):
            raise ValueError("Clef effort bounds require low, medium, high, xhigh or max")
        if EFFORTS.index(entry["min"]) > EFFORTS.index(entry["max"]):
            raise ValueError("Clef effort floor cannot exceed its ceiling")
        result[model] = entry
    return result


def load_bounds(path=None):
    target = bounds_path(path)
    try:
        with target.open(encoding="utf-8") as handle:
            raw = handle.read(16385)
    except FileNotFoundError:
        return validate_bounds({})
    except (OSError, UnicodeError):
        raise ValueError("Cannot read Clef effort bounds") from None
    if len(raw) > 16384:
        raise ValueError("Clef effort bounds file is too large")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("Clef effort bounds must be valid JSON") from None
    return validate_bounds(value)


def allowed_efforts(model, bounds, sol_cap=None):
    name = {LUNA: "luna", SOL: "sol"}.get(model, model)
    if name not in DEFAULT_BOUNDS:
        raise ValueError("Clef effort bounds apply only to Luna and Sol")
    entry = bounds[name]
    floor, ceiling = EFFORTS.index(entry["min"]), EFFORTS.index(entry["max"])
    if name == "sol" and sol_cap is not None:
        if sol_cap not in EFFORTS:
            raise ValueError("unsupported Clef Sol effort cap")
        ceiling = min(ceiling, EFFORTS.index(sol_cap))
    return list(EFFORTS[floor:ceiling + 1])


def clamp_effort(model, effort, bounds, sol_cap=None):
    allowed = allowed_efforts(model, bounds, sol_cap)
    if not allowed:
        raise ValueError("No permitted Sol effort below the selected picker ceiling")
    effort = effort if effort in EFFORTS else "medium"
    index = max(EFFORTS.index(allowed[0]),
                min(EFFORTS.index(effort), EFFORTS.index(allowed[-1])))
    return EFFORTS[index]


def write_bounds(value, path=None):
    value = validate_bounds(value)
    target = bounds_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".clef-effort-bounds-", dir=target.parent)
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
    parser = argparse.ArgumentParser(description="Show or set Clef effort floors and ceilings")
    parser.add_argument("--file", type=Path, help="Use a different effort bounds file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    change = commands.add_parser("set")
    for model in DEFAULT_BOUNDS:
        for bound in ("min", "max"):
            change.add_argument(f"--{model}-{bound}", choices=EFFORTS)
    commands.add_parser("reset", help="Restore Luna medium–max and Sol low–max")
    args = parser.parse_args(argv)
    try:
        if args.command == "reset":
            value = write_bounds(DEFAULT_BOUNDS, args.file)
        else:
            value = load_bounds(args.file)
            if args.command == "set":
                changed = False
                for model in DEFAULT_BOUNDS:
                    for bound in ("min", "max"):
                        effort = getattr(args, f"{model}_{bound}")
                        if effort is not None:
                            value[model][bound] = effort
                            changed = True
                if not changed:
                    parser.error("set requires at least one floor or ceiling")
                value = write_bounds(value, args.file)
        print(json.dumps({"file": str(bounds_path(args.file)), "bounds": value}))
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
