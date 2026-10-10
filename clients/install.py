"""Install launchers for native Clef clients without changing client settings."""
import argparse
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

MARKER = "# Jev Clef native client launcher"


def install_launcher(path, command):
    if path.exists() or path.is_symlink():
        if path.is_symlink() or MARKER not in path.read_text():
            raise RuntimeError(f"Unrelated existing launcher: {path}")
    content = "#!/bin/sh\n" + MARKER + "\nexec " + shlex.join(command) + ' "$@"\n'
    temporary = path.with_name(path.name + ".install")
    temporary.write_text(content)
    temporary.chmod(0o700)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--claude", action="store_true")
    parser.add_argument("--antigravity", action="store_true")
    args = parser.parse_args()
    both = not (args.claude or args.antigravity)
    root = Path(__file__).resolve().parent
    destination = Path.home() / ".local/bin"
    destination.mkdir(parents=True, exist_ok=True)
    if both or args.claude:
        if not shutil.which("claude"):
            raise RuntimeError("Official Claude Code CLI unavailable")
        install_launcher(destination / "claude-clef", [sys.executable, str(root / "claude_auto.py")])
        print("Installed " + str(destination / "claude-clef"))
    if both or args.antigravity:
        if not args.node:
            raise RuntimeError("Node.js 22.19+ unavailable")
        version = subprocess.check_output([args.node, "--version"], text=True).strip().lstrip("v")
        numbers = tuple(int(n) for n in version.split(".")[:2])
        if numbers < (22, 19):
            raise RuntimeError("Node.js 22.19+ required")
        install_launcher(destination / "antigravity-clef",
                         [str(Path(args.node).resolve()), str(root / "antigravity_auto.mjs")])
        print("Installed " + str(destination / "antigravity-clef"))


if __name__ == "__main__":
    main()
