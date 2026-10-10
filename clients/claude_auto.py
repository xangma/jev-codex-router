#!/usr/bin/env python3
"""Route user turns through Clef while the native Claude CLI owns execution."""

from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable


MODES = ("auto-cheap", "auto", "auto-deep")
EFFORTS = ("low", "medium", "high", "max")
_EOF = object()
_MAX_LINE = 16 * 1024 * 1024


class ControllerError(RuntimeError):
    """A decision or native control operation could not be verified."""


def session_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ControllerError("Session ID must be a UUID") from exc


def _model_name(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(
        c.isspace() or ord(c) < 32 for c in value
    )


def decide(task, candidates, mode="auto", context=None):
    # The doctor and protocol tests do not require the decision client.
    if __package__:
        from .clef_route import decide as route
    else:
        from clef_route import decide as route
    return route(task, candidates, mode=mode, context=context)


class NativeClaude:
    """One native CLI process, speaking its streaming JSON control protocol."""

    def __init__(
        self, *, command="claude", session_id=None, resume=None,
        initial_model=None, initial_effort=None, permission=None,
        request_timeout=30.0, turn_timeout=1800.0, popen=subprocess.Popen,
    ):
        if session_id is not None and resume is not None:
            raise ControllerError("Use a new session ID or resume, not both")
        self.resume = session_uuid(resume) if resume is not None else None
        self.session_id = self.resume or session_uuid(session_id or str(uuid.uuid4()))
        self.command = command
        self.initial_model = initial_model
        self.initial_effort = initial_effort
        self.permission = permission or (lambda request: False)
        self.request_timeout = request_timeout
        self.turn_timeout = turn_timeout
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not math.isfinite(value) or value <= 0
               for value in (request_timeout, turn_timeout)):
            raise ControllerError("Timeouts must be finite and positive")
        self._popen = popen
        self.process = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._events = queue.Queue()
        self._pending = deque()
        self._write_lock = threading.Lock()
        self._closing = False
        self._turn_active = False
        self.catalog = []
        self.applied = None

    def start(self):
        if self.process is not None:
            raise ControllerError("Native CLI is already running")
        args = [
            self.command, "-p", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--permission-prompt-tool", "stdio",
            f"--resume={self.resume}" if self.resume else f"--session-id={self.session_id}",
        ]
        if self.initial_model is not None:
            if not _model_name(self.initial_model):
                raise ControllerError("Invalid native model")
            args.extend(["--model", self.initial_model])
        if self.initial_effort is not None:
            if self.initial_effort not in EFFORTS:
                raise ControllerError("Invalid native effort")
            args.extend(["--effort", self.initial_effort])
        try:
            # Inherit native authentication, working directory, settings, and tools.
            # A separate process group permits bounded cleanup of our own jobs.
            self.process = self._popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                start_new_session=os.name != "nt",
            )
            self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
            initialized = self.request({"subtype": "initialize", "hooks": {}})
            self.applied = self.get_applied()
            self.catalog = self._catalog(initialized.get("models"))
            return self
        except Exception:
            self.close()
            raise

    def _read_stdout(self):
        try:
            while True:
                line = self.process.stdout.readline(_MAX_LINE + 1)
                if not line:
                    break
                if len(line) > _MAX_LINE:
                    raise ControllerError("Native JSON message exceeds the supported size")
                try:
                    message = json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise ControllerError("Native CLI returned malformed JSON") from exc
                if not isinstance(message, dict):
                    raise ControllerError("Native CLI returned an invalid message")
                self._events.put(message)
        except Exception as exc:
            self._events.put(ControllerError(str(exc)) if isinstance(exc, ControllerError)
                             else ControllerError("Native output could not be read"))
        finally:
            self._events.put(_EOF)

    def _drain_stderr(self):
        # Native stderr can contain private configuration; never mirror or store it.
        try:
            while self.process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def _send(self, message):
        if self.process is None or self.process.poll() is not None:
            raise ControllerError("Native CLI is not running")
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ControllerError("Native input could not be written") from exc

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControllerError("Native CLI response timed out")
        try:
            message = self._events.get(timeout=remaining)
        except queue.Empty as exc:
            raise ControllerError("Native CLI response timed out") from exc
        if message is _EOF:
            raise ControllerError("Native CLI exited before completing the operation")
        if isinstance(message, Exception):
            raise message
        return message

    def _permission_request(self, message):
        request = message.get("request")
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not isinstance(request, dict):
            raise ControllerError("Invalid native control request")
        if request.get("subtype") != "can_use_tool":
            self._send({"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": "This controller does not support that interaction",
            }})
            return
        if (not isinstance(request.get("input"), dict)
                or not isinstance(request.get("tool_name"), str)
                or not isinstance(request.get("tool_use_id"), str)):
            raise ControllerError("Invalid native tool permission request")
        allowed = False
        if not self._closing:
            try:
                allowed = self.permission(request) is True
            except (EOFError, KeyboardInterrupt):
                allowed = False
        answer = {"behavior": "allow", "updatedInput": request["input"]} if allowed else {
            "behavior": "deny", "message": "Tool execution was not approved by the user",
        }
        self._send({"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": answer,
        }})

    def request(self, request, *, timeout=None):
        request_id = str(uuid.uuid4())
        self._send({"type": "control_request", "request_id": request_id, "request": request})
        deadline = time.monotonic() + (self.request_timeout if timeout is None else timeout)
        while True:
            message = self._receive(deadline)
            if message.get("type") == "control_request":
                self._permission_request(message)
            elif message.get("type") == "control_response":
                response = message.get("response")
                if not isinstance(response, dict):
                    raise ControllerError("Invalid native control response")
                if response.get("request_id") != request_id:
                    continue
                if response.get("subtype") != "success":
                    raise ControllerError(f"Native control request rejected: {request.get('subtype')}")
                body = response.get("response", {})
                if not isinstance(body, dict):
                    raise ControllerError("Invalid native control response body")
                return body
            else:
                self._pending.append(message)

    def get_applied(self):
        body = self.request({"subtype": "get_settings"})
        applied = body.get("applied")
        if not isinstance(applied, dict) or not _model_name(applied.get("model")):
            raise ControllerError("Native CLI did not report its applied model")
        effort = applied.get("effort")
        if effort is not None and effort not in EFFORTS:
            raise ControllerError("Native CLI reported an unsupported effort")
        # Deliberately discard effective/sources: they can contain credentials.
        return {"model": applied["model"], "effort": effort}

    def _catalog(self, options):
        if not isinstance(options, list) or not options:
            raise ControllerError("Native CLI did not provide a model catalog")
        initial = self.applied["model"]
        catalog = []
        seen = set()
        for option in options:
            if not isinstance(option, dict) or not _model_name(option.get("value")):
                continue
            alias = option["value"]
            self.request({"subtype": "set_model", "model": alias})
            model = self.get_applied()["model"]
            if model in seen:
                continue
            if option.get("supportsEffort") is True:
                advertised = option.get("supportedEffortLevels")
                if not isinstance(advertised, list):
                    raise ControllerError("Native model has no verified effort levels")
                efforts = [effort for effort in EFFORTS if effort in advertised]
                if not efforts:
                    raise ControllerError("Native model has no supported effort levels")
            else:
                efforts = [None]
            description = option.get("description")
            catalog.append({"alias": alias, "model": model, "efforts": efforts,
                            "description": description[:512] if isinstance(description, str) else ""})
            seen.add(model)
        self.request({"subtype": "set_model", "model": initial})
        self.applied = self.get_applied()
        if not catalog:
            raise ControllerError("Native model catalog is empty")
        return catalog

    def configure(self, model, effort):
        if effort is None:
            self.request({"subtype": "set_model", "model": model})
        else:
            self.request({"subtype": "apply_flag_settings", "settings": {
                "model": model, "effortLevel": effort,
            }})
        self.applied = self.get_applied()
        return self.applied

    def run_turn(self, prompt, *, on_event=None):
        self._send({"type": "user", "message": {"role": "user", "content": prompt},
                    "parent_tool_use_id": None, "session_id": self.session_id})
        self._turn_active = True
        deadline = time.monotonic() + self.turn_timeout
        while True:
            message = self._pending.popleft() if self._pending else self._receive(deadline)
            if message.get("type") == "control_request":
                self._permission_request(message)
                continue
            if message.get("type") == "control_response":
                continue
            reported_session = message.get("session_id")
            if reported_session is not None and reported_session != self.session_id:
                raise ControllerError("Native response belongs to a different session")
            if on_event:
                on_event(message)
            if message.get("type") == "result":
                self._turn_active = False
                return message

    def _signal(self, sig):
        if os.name != "nt" and getattr(self.process, "pid", None):
            try:
                os.killpg(self.process.pid, sig)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        if sig == signal.SIGTERM:
            self.process.terminate()
        else:
            self.process.kill()

    def close(self):
        if self.process is None:
            return True
        self._closing = True
        if self._turn_active and self.process.poll() is None:
            try:
                self.request({"subtype": "interrupt"}, timeout=min(self.request_timeout, 2.0))
            except ControllerError:
                pass
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        clean = True
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            clean = False
            self._signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal(signal.SIGKILL)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    clean = False
        # A native parent can exit while an inherited pipe remains open in a
        # background tool. Clean up only the process group we created, then do
        # not block closing a stream still owned by a reader thread.
        if os.name != "nt" and getattr(self.process, "pid", None):
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except OSError:
                pass
        readers = (self._stdout_thread, self._stderr_thread)
        for reader in readers:
            if reader is not None:
                reader.join(timeout=0.5)
        if any(reader is not None and reader.is_alive() for reader in readers):
            if os.name != "nt" and getattr(self.process, "pid", None):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except OSError:
                    pass
        for stream, reader in ((self.process.stdout, self._stdout_thread),
                               (self.process.stderr, self._stderr_thread)):
            if reader is not None:
                reader.join(timeout=0.5)
                if reader.is_alive():
                    clean = False
                    continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        return clean and self.process.returncode == 0


class ClaudeAuto:
    def __init__(self, *, mode="auto", resume=None, native_factory=NativeClaude,
                 decider=decide, **native_options):
        if mode not in MODES:
            raise ControllerError("Unknown Auto mode")
        self.mode = mode
        self.session_id = session_uuid(resume) if resume is not None else str(uuid.uuid4())
        self._resume = resume
        self._has_history = resume is not None
        self._factory = native_factory
        self._native_options = native_options
        self._decider = decider
        self.native = None
        self.last_decision = None
        self.context = {}

    def start(self):
        identity = {"resume": self.session_id} if self._resume is not None else {"session_id": self.session_id}
        self.native = self._factory(**identity, **self._native_options).start()
        return self

    def candidates(self):
        if self.native is None:
            raise ControllerError("Native CLI has not been initialized")
        return [{"model": option["model"], "effort": effort,
                 "description": option["description"]} for option in self.native.catalog
                for effort in option["efforts"]]

    def run_turn(self, prompt, *, on_event=None):
        if not isinstance(prompt, str):
            raise ControllerError("Prompt must be text")
        if self.native is None:
            self.start()
        candidates = self.candidates()
        choice = self._decider(prompt, candidates, mode=self.mode, context=dict(self.context))
        if not isinstance(choice, dict) or not any(
            choice.get("model") == candidate["model"] and
            choice.get("effort") == candidate["effort"] for candidate in candidates
        ):
            raise ControllerError("Clef selected a route outside the native catalog")
        model, effort = choice["model"], choice.get("effort")
        applied = self.native.configure(model, effort)
        if applied != {"model": model, "effort": effort} and effort == "max":
            # CLI 2.1.110 ignores max in apply_flag_settings. Preserve the same
            # native transcript and verify the launch-time setting before input.
            if not self.native.close():
                raise ControllerError("Native session did not close cleanly; max restart was canceled")
            identity = {"resume": self.session_id} if self._has_history else {
                "session_id": self.session_id,
            }
            self.native = self._factory(**identity, initial_model=model,
                                        initial_effort="max", **self._native_options).start()
            if not any(c["model"] == model and c["effort"] == effort for c in self.candidates()):
                raise ControllerError("Selected route is no longer in the native catalog")
            applied = self.native.get_applied()
        if applied != {"model": model, "effort": effort}:
            raise ControllerError("Native model and effort do not match the Clef decision")
        self.last_decision = choice
        if on_event:
            on_event({"type": "clef_route", "mode": self.mode,
                      "model": model, "effort": effort, "source": choice.get("source")})
        result = self.native.run_turn(prompt, on_event=on_event)
        self._has_history = True
        self.context = {"previous_task": prompt[-4000:],
                        "previous_response": str(result.get("result", ""))[-4000:],
                        "last_model": model, "last_effort": effort,
                        "last_result": result.get("subtype")}
        return result

    def close(self):
        return self.native.close() if self.native is not None else True


def approve_tool(request):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    print(f"\nClaude requests {request['tool_name']}:", file=sys.stderr)
    print(json.dumps(request["input"], ensure_ascii=False, indent=2), file=sys.stderr)
    try:
        print("Allow this tool call? [y/N] ", file=sys.stderr, end="", flush=True)
        return sys.stdin.readline().rstrip("\r\n").lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def cli_version(command):
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError("Native CLI version could not be read") from exc
    match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout)
    if result.returncode or not match:
        raise ControllerError("Native CLI version could not be read")
    return match.group(0)


def route_receipt(event):
    if event.get("type") == "clef_route":
        effort = event["effort"] or "n/a"
        print(f"Clef {event['mode']}: {event['model']} (effort {effort})",
              file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="auto")
    parser.add_argument("--resume", metavar="UUID")
    parser.add_argument("--print", "-p", dest="prompt", metavar="TASK")
    parser.add_argument("--output-format", choices=("text", "json"), default="text")
    parser.add_argument("--doctor", action="store_true", help="Initialize only; send no user prompt")
    parser.add_argument("--claude-bin", default="claude")
    args = parser.parse_args(argv)
    if args.doctor and args.prompt is not None:
        parser.error("--doctor cannot be combined with --print")
    controller = None
    try:
        controller = ClaudeAuto(mode=args.mode, resume=args.resume,
                               command=args.claude_bin, permission=approve_tool).start()
        if args.doctor:
            print(json.dumps({"cli_version": cli_version(args.claude_bin),
                              "models": controller.native.catalog,
                              "applied": controller.native.applied}, ensure_ascii=False))
            return 0
        if args.prompt is not None:
            result = controller.run_turn(args.prompt, on_event=route_receipt)
            print(json.dumps(result, ensure_ascii=False) if args.output_format == "json"
                  else result.get("result", ""))
            failed = result.get("is_error") or result.get("subtype") != "success"
            if failed and args.output_format == "text":
                print("Native Claude did not complete the turn", file=sys.stderr)
            return 1 if failed else 0
        if not sys.stdin.isatty():
            raise ControllerError("Use --print for noninteractive input")
        print(f"Native Claude session: {controller.session_id}", file=sys.stderr)
        while True:
            try:
                prompt = input("You> ")
            except EOFError:
                return 0
            if prompt in {"/exit", ":quit"}:
                return 0
            result = controller.run_turn(prompt, on_event=route_receipt)
            print(result.get("result", ""))
    except KeyboardInterrupt:
        return 130
    except ControllerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Decision-client/native failures must not dump prompts or configuration.
        print("Claude Auto could not complete the turn", file=sys.stderr)
        return 1
    finally:
        if controller is not None:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
