"""Stable, retrievable previews of oversized textual tool results.

Only tool result strings are reduced. Full UTF-8 output is retained privately;
an archive failure leaves the original request intact. Content addressing keeps
the same historical result byte-identical on subsequent replays and restarts.
"""
import hashlib
import os
from pathlib import Path
import stat
import tempfile

MAX_OUTPUT_BYTES = 16 * 1024
TOOL_OUTPUTS = ("function_call_output", "custom_tool_call_output")


def _archive(data, state_dir):
    root = Path(state_dir) / "tool-output"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("tool output directory must be owner-only")
        digest = hashlib.sha256(data).hexdigest()
        name = digest + ".txt"
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            # Publish atomically: another request must never read a partial file.
            fd, temporary = tempfile.mkstemp(prefix=".output-", dir=root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                os.link(temporary, name, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                pass
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size != len(data)
                    or hashlib.file_digest(handle, "sha256").hexdigest() != digest):
                raise OSError("invalid tool output archive")
        return str(root / name)
    finally:
        if temporary is not None:
            os.unlink(temporary)
        os.close(directory)


def preview_tool_outputs(payload, state_dir):
    """Replace oversized text results in place; return only numeric telemetry."""
    stats = {"count": 0, "original_bytes": 0, "preview_bytes": 0}
    items = payload.get("input")
    if not isinstance(items, list):
        return stats
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") not in TOOL_OUTPUTS:
            continue
        output = item.get("output")
        # Structured content can include images or opaque payloads. Preserve it.
        if not isinstance(output, str):
            continue
        data = output.encode("utf-8", "replace")
        if len(data) <= MAX_OUTPUT_BYTES:
            continue
        try:
            path = _archive(data, state_dir)
        except (OSError, ValueError):
            continue
        note = (f"[Router tool output preview: {len(data)} UTF-8 bytes. "
                f"Full output: {path}. Read this file for omitted details.]\n")
        divider = "\n[... omitted from preview; full output is in the file above ...]\n"
        budget = MAX_OUTPUT_BYTES - len((note + divider).encode("utf-8"))
        if budget < 2:
            continue
        head = budget * 2 // 3
        preview = (note + data[:head].decode("utf-8", "ignore") + divider
                   + data[-(budget - head):].decode("utf-8", "ignore"))
        items[index] = {**item, "output": preview}
        stats["count"] += 1
        stats["original_bytes"] += len(data)
        stats["preview_bytes"] += len(preview.encode("utf-8"))
    return stats
