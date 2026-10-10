"""Install the local Clef profile using the embedded router's managed commands."""
import argparse
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
        "ProgramArguments": [str(binary), "--model", str(model), "--alias", "clef-flash",
                             "--host", "127.0.0.1", "--port", "8082", "-ngl", "99", "-np", "1",
                             "-c", "8192", "-b", "8192", "-ub", "8192", "--no-webui",
                             "--api-key-file", str(key_file)],
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

    if existing != service and registered():
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
    run(REPO / "router/bin/control", "picker", "set", "jev/auto", "show")
    env["JEV_PYTHON"] = sys.executable
    run("/bin/zsh", REPO / "server/install-service.sh")
    verify_clef_adapter()
    if args.smoke:
        run(sys.executable, REPO / "server/smoke.py")
    print(json.dumps({"installed": "clef", "model": "jev/auto", "display": "Auto (Clef)",
                      "next": "Restart Codex when convenient, then select Auto (Clef)."}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(json.dumps({"installed": False, "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
