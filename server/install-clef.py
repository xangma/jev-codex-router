"""Install the local Clef profile using the embedded router's managed commands."""
import argparse
import configparser
import io
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.request

REPO = Path(__file__).resolve().parent.parent
LABEL = "com.xangma.clef-flash"
PICKER_MODELS = ("jev/auto-cheap", "jev/auto", "jev/auto-deep")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def private_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def ensure_key(path):
    if not path.exists():
        private_file(path, (secrets.token_hex(32) + "\n").encode())
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Clef key must be an owner-only regular file")
    if not path.read_text().strip():
        raise RuntimeError("Clef key file is empty")


def positive_seconds(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("idle timeout must be a positive integer") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("idle timeout must be a positive integer")
    return seconds


def render_clef_preset(existing_text, model, sleep_idle_seconds=300):
    """Refresh managed options while retaining other models and custom options."""
    seconds = positive_seconds(sleep_idle_seconds)
    if any(character in str(model) for character in "\r\n"):
        raise ValueError("The model path cannot contain a newline")
    preset = configparser.ConfigParser(interpolation=None)
    preset.optionxform = str
    lines = existing_text.splitlines(keepends=True)
    section_start = next((index for index, line in enumerate(lines)
                          if line.lstrip().startswith("[")), len(lines))
    preamble = "".join(lines[:section_start])
    metadata = configparser.ConfigParser(interpolation=None)
    metadata.read_string("[metadata]\n" + preamble)
    if metadata.has_option("metadata", "version"):
        if metadata["metadata"]["version"].strip() != "1":
            raise ValueError("Unsupported llama.cpp preset version")
    else:
        preamble = preamble.rstrip() + ("\n" if preamble.strip() else "") + "version = 1\n"
    if section_start < len(lines):
        preset.read_string("".join(lines[section_start:]))
    for section in ("*", "clef-flash"):
        if not preset.has_section(section):
            preset.add_section(section)
    preset["*"]["sleep-idle-seconds"] = str(seconds)
    for alias in ("m", "a", "ngl", "np", "c", "b", "ub", "sleep-idle-seconds"):
        preset.remove_option("clef-flash", alias)
    preset["clef-flash"].update({
        "model": str(model), "alias": "clef-flash", "n-gpu-layers": "99",
        "parallel": "1", "ctx-size": "8192", "batch-size": "8192",
        "ubatch-size": "8192", "load-on-startup": "false",
    })
    output = io.StringIO()
    preset.write(output)
    return preamble.rstrip() + "\n\n" + output.getvalue()


def write_clef_preset(path, model, sleep_idle_seconds=300):
    """Atomically install an owner-only preset; return whether it changed."""
    existing = ""
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("Clef preset must be an owner-only regular file")
        existing = path.read_text()
    rendered = render_clef_preset(existing, model, sleep_idle_seconds)
    if existing == rendered:
        return False
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        private_file(temporary, rendered.encode())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def clef_service_arguments(binary, preset_file, key_file):
    return [str(binary), "--models-preset", str(preset_file), "--models-max", "1",
            "--models-autoload", "--host", "127.0.0.1", "--port", "8082",
            "--no-webui", "--api-key-file", str(key_file)]


def wait_clef(key_file, timeout=120):
    deadline = time.monotonic() + timeout
    opener = local_opener()
    while time.monotonic() < deadline:
        request = urllib.request.Request("http://127.0.0.1:8082/v1/models", headers={
            "Authorization": "Bearer " + key_file.read_text().strip(),
        })
        try:
            with opener.open(request, timeout=5) as response:
                models = json.load(response).get("data", [])
                if any(model.get("id") == "clef-flash" for model in models):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Clef did not become ready; inspect its private service log")


def verify_clef_adapter():
    with local_opener().open("http://127.0.0.1:4319/health", timeout=5) as response:
        health = json.load(response)
    expected = {"decision_provider": "clef", "decision_model": "clef-flash",
                "decision_configured": True, "baseline_model": "gpt-6.1-sol"}
    if not health.get("ok") or any(health.get(key) != value for key, value in expected.items()):
        raise RuntimeError("The running adapter does not match the local Clef profile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llama-server", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--models-preset", type=Path,
                        default=Path.home() / ".local/share/clef/models.ini",
                        help="Owner-only llama.cpp router preset file")
    parser.add_argument("--sleep-idle-seconds", type=positive_seconds, default=300,
                        help="Inherited model idle timeout in seconds (default: 300)")
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--share-chatgpt", action="store_true",
                        help="Allow the local router to use Codex's signed-in ChatGPT session")
    parser.add_argument("--smoke", action="store_true",
                        help="Send one quota-consuming Codex request after installation")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This launchd profile requires macOS")
    if not args.share_chatgpt:
        parser.error("Native execution requires explicit --share-chatgpt consent")
    binary, model = args.llama_server.resolve(), args.model.resolve()
    preset_file = args.models_preset.expanduser().absolute()
    if not os.access(binary, os.X_OK) or not model.is_file():
        parser.error("A built llama-server and a downloaded Clef GGUF are required")
    if not args.node:
        parser.error("Node.js 22.19+ is required")
    node = str(Path(args.node).resolve())
    env = dict(os.environ)
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    env["CODEX_ROUTER_NODE_BIN"] = node
    env["CODEX_ROUTER_INSTALL_SKILLS"] = "0"
    env["CODEX_ROUTER_MANAGE_AGENT_DEFAULTS"] = "0"
    codex_home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex")))
    state = Path(env.get("MODEL_ROUTER_STATE_DIR") or env.get("CODEX_ROUTER_STATE_DIR")
                 or env.get("KIMI_CODEX_STATE_DIR") or str(codex_home / "codex-router"))
    env["CODEX_ROUTER_STATE_DIR"] = str(state)
    key_file = state / "clef-api.key"
    env.update(JEV_DECISION_PROVIDER="clef", CLEF_ENDPOINT="http://127.0.0.1:8082/v1/systemone",
               CLEF_MODEL="clef-flash", CLEF_API_KEY_FILE=str(key_file),
               JEV_DECISION_TIMEOUT="30", JEV_BASELINE_MODEL="gpt-6.1-sol")

    def run(*command, capture=False):
        return subprocess.run([str(part) for part in command], cwd=REPO, env=env,
                              check=True, capture_output=capture, text=True)

    run(node, REPO / "router/src/service-write-guard.mjs", "--live-install")
    run(node, "-e", "const [a,b]=process.versions.node.split('.').map(Number);process.exit(a>22||(a===22&&b>=19)?0:1)")
    # Prepare dependencies in a temporary Codex home before touching real state.
    run(REPO / "install.sh", "--prepare-only")
    config = codex_home / "config.toml"
    if config.exists():
        backup = state / "clef-install-backups" / str(time.time_ns()) / "config.toml"
        private_file(backup, config.read_bytes())
    ensure_key(key_file)
    preset_changed = write_clef_preset(preset_file, model, args.sleep_idle_seconds)
    logs = Path.home() / "Library/Logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name in ("clef-flash.out.log", "clef-flash.err.log"):
        path = logs / name
        if not path.exists():
            private_file(path, b"")
        path.chmod(0o600)
    plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    plist.parent.mkdir(parents=True, exist_ok=True)
    if not plist.exists():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 8082))
    service = {
        "Label": LABEL,
        "ProgramArguments": clef_service_arguments(binary, preset_file, key_file),
        "WorkingDirectory": str(binary.parent), "RunAtLoad": True, "KeepAlive": True,
        "StandardOutPath": str(logs / "clef-flash.out.log"),
        "StandardErrorPath": str(logs / "clef-flash.err.log"),
        "Umask": 0o077,
    }
    existing = None
    if plist.exists():
        existing = plistlib.loads(plist.read_bytes())
        if existing.get("ProgramArguments", [])[:1] != [str(binary)]:
            raise RuntimeError("Existing Clef service uses a different binary; review before replacing")
    if existing != service:
        temporary = plist.with_name(plist.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            private_file(temporary, plistlib.dumps(service))
            temporary.replace(plist)
        finally:
            temporary.unlink(missing_ok=True)
    domain = f"gui/{os.getuid()}"
    service_target = f"{domain}/{LABEL}"

    def registered():
        return subprocess.run(["launchctl", "print", service_target], capture_output=True).returncode == 0

    if (existing != service or preset_changed) and registered():
        run("launchctl", "bootout", service_target, capture=True)
        deadline = time.monotonic() + 15
        while registered():
            if time.monotonic() >= deadline:
                raise RuntimeError("Previous Clef service has not stopped")
            time.sleep(0.5)
    if not registered():
        # launchd can briefly reject bootstrap after a completed bootout.
        for attempt in range(5):
            result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
            if result.returncode == 0 or registered():
                break
            if result.returncode != 5 or attempt == 4:
                raise RuntimeError("Clef service could not be registered")
            time.sleep(1)
    wait_clef(key_file)

    cli = REPO / "router/bin/codex-router"
    exists = subprocess.run([str(cli), "providers", "generic", "show", "jev", "--json"],
                            cwd=REPO, env=env, capture_output=True).returncode == 0
    run(cli, "providers", "generic", "edit" if exists else "add", "jev", "--name", "Clef Router",
        "--base-url", "http://127.0.0.1:4319/v1", "--adapter", "openai-responses", "--allow-private")
    run(cli, "providers", "generic", "enable", "jev")
    run(node, REPO / "server/configure-model.mjs")
    run(node, REPO / "server/configure-auth.mjs")
    run(cli, "chatgpt-session", "enable")
    run(REPO / "router/install.sh", "--auto", "--providers", "configured", "--no-tray")
    run(cli, "refresh-catalog")
    for picker_model in PICKER_MODELS:
        run(REPO / "router/bin/control", "picker", "set", picker_model, "show")
    env["JEV_PYTHON"] = sys.executable
    run("/bin/zsh", REPO / "server/install-service.sh")
    verify_clef_adapter()
    if args.smoke:
        run(sys.executable, REPO / "server/smoke.py")
    print(json.dumps({"installed": "clef", "model": "jev/auto", "models": list(PICKER_MODELS),
                      "display": "Auto Standard (Clef)", "models_preset": str(preset_file),
                      "sleep_idle_seconds": args.sleep_idle_seconds,
                      "next": "Restart Codex when convenient, then select Auto Cheap, Standard, or Deep (Clef)."}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, configparser.Error, subprocess.CalledProcessError) as error:
        print(json.dumps({"installed": False, "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
