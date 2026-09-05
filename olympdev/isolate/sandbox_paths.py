from pathlib import Path

STDOUT_FILE = "stdout.txt"
STDERR_FILE = "stderr.txt"

SIMPLE_RUN_INPUT = "input.txt"
SIMPLE_RUN_BINARY = "binary.bin"

TOOL_BINARY = "tool"
TOOL_STDIN = "stdin.txt"

# Files jobs write themselves; digest-controlled paths must not collide.
COMPILE_RESERVED_PATHS = frozenset({"compile_stdout.txt", "compile_stderr.txt"})
SIMPLE_RUN_RESERVED_PATHS = frozenset({
    SIMPLE_RUN_INPUT, SIMPLE_RUN_BINARY, STDOUT_FILE, STDERR_FILE,
})
TOOL_RESERVED_PATHS = frozenset({
    TOOL_BINARY, TOOL_STDIN, STDOUT_FILE, STDERR_FILE,
})


def sandbox_relpath(
    value: str | Path,
    *,
    reserved: frozenset[str] = frozenset(),
) -> str:
    """A relative path safe to place inside /box (no abs, no .., no reserved)."""
    raw = value.as_posix() if isinstance(value, Path) else str(value)
    if not raw or raw.startswith("/") or Path(raw).is_absolute():
        raise ValueError(f"sandbox path must be relative: {raw!r}")
    parts = [part for part in Path(raw).parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"invalid sandbox path: {raw!r}")
    for part in parts:
        if part.startswith("-"):
            raise ValueError(f"sandbox path must not start with '-': {raw!r}")
        if ":" in part or "=" in part:
            raise ValueError(f"sandbox path must not contain ':' or '=': {raw!r}")
    rel = "/".join(parts)
    if rel in reserved:
        raise ValueError(f"sandbox path is reserved: {rel}")
    return rel
