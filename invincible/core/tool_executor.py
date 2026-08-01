# invincible/core/tool_executor.py
"""Execution layer for MCP tools (execute_bash, write_file).

Security model - decided explicitly up front, not bolted on after the fact:

  1. execute_bash uses a DENYLIST, not an allowlist: known-dangerous command
     patterns are blocked outright, everything else is allowed through. This
     keeps the tool usable for arbitrary dev work while still catching the
     small set of commands most likely to do irreversible damage.
  2. write_file additionally has its own path denylist: even an otherwise
     harmless-looking write is blocked outright if its target is a file
     this project depends on for its own security or state (`.env`,
     `providers.yaml`, `sessions.db`, Invincible's own source, its tests,
     or `.git/`). Confirmation is a good backstop, but it shouldn't be the
     only thing standing between a cloud AI and this server rewriting its
     own auth check.
  3. Every execute_bash and write_file call that isn't blocked still stops
     and waits for interactive confirmation, typed at the same terminal
     running this server. This is a local single-user tool - the person
     approving *is* the person sitting at the machine - so a synchronous
     terminal prompt is the natural confirmation surface, not a second HTTP
     round-trip or a web UI.
  4. Authentication for who can reach this code at all lives one layer up,
     in the MCP endpoint's dependency (MCP_SHARED_SECRET, independent of
     GATEWAY_API_KEY). This module assumes the caller is already
     authenticated - it only decides whether a specific action is safe and
     approved, not who's allowed to ask.

KNOWN LIMIT: the denylist is a text-pattern match, not a real shell parser.
`powershell -Command "..."`, `cmd /c "..."`, or any other wrapper/encoding
can smuggle an arbitrary command past every pattern below. The denylist
exists to catch the obvious, high-blast-radius cases without a prompt; it
is not the real safety boundary. The confirmation step is - read what you
approve.
"""
import asyncio
import logging
import os
import re
import subprocess

logger = logging.getLogger("invincible.tool_executor")

# Matched against the full command string, case-insensitive. Each entry is
# (compiled pattern, human-readable reason) so a block can explain itself
# in the response instead of failing silently.
DENYLIST_PATTERNS = [
    # --- Unix / POSIX ---
    (re.compile(r"rm\s+(-\w*r\w*f\w*|-\w*f\w*r\w*)\s+(/|~|\$HOME)(\s|/|$)", re.I),
     "recursive force-delete of home or root"),
    (re.compile(r"rm\s+-[a-z]*r[a-z]*\s+/(\s|$)", re.I),
     "recursive delete starting at filesystem root"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.I),
     "fork bomb"),
    (re.compile(r"\bdd\s+.*of=/dev/", re.I),
     "raw write to a block device"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I),
     "filesystem format command"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)", re.I),
     "redirect writing directly to a disk device"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
     "system power/shutdown command"),
    (re.compile(r"\bsudo\b", re.I),
     "privilege escalation via sudo"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\s+/(\s|$)", re.I),
     "world-writable permissions on filesystem root"),
    (re.compile(r"\bchown\s+-R\s+\S+\s+/(\s|$)", re.I),
     "recursive ownership change on filesystem root"),
    (re.compile(r"(curl|wget)\s+.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I),
     "piping a remote download straight into a shell"),
    (re.compile(r"\bkill\s+-9\s+-1\b", re.I),
     "kill all processes"),
    (re.compile(r">\s*/etc/(passwd|shadow|sudoers)\b", re.I),
     "overwrite of a core system credentials file"),

    # --- Windows / cmd.exe ---
    # rd/rmdir/del/erase with an /s (recurse) flag AND a drive-root target
    # (C:\, C:\*, C:\*.*). Flags can appear in either order around the
    # target, so both lookaheads scan the whole command rather than
    # anchoring to a fixed position. A subdirectory target (rd /s C:\build)
    # does NOT match - that's the Windows equivalent of `rm -rf ./build`
    # and is left to the confirmation step, same as its Unix counterpart.
    (re.compile(
        r"\b(rd|rmdir|del|erase)\b"
        r"(?=.*(?<!\S)/s(?!\S))"
        r"(?=.*[A-Za-z]:\\+(\*(\.\*)?)?(\s|[\"'&|]|$))",
        re.I,
    ), "recursive delete targeting a Windows drive root"),
    (re.compile(r"\bformat\s+[A-Za-z]:", re.I),
     "formatting a Windows drive"),
]

# Paths (relative to the repo root) that write_file refuses to touch
# outright, regardless of confirmation. Repo root is resolved the same way
# Router resolves providers.yaml (three dirname() calls up from this file:
# invincible/core/tool_executor.py -> invincible/core -> invincible -> repo root).
#
# Case-insensitive on purpose: Windows filesystems treat .env and .ENV as
# the same file, so a differently-cased target must not slip past.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WRITE_DENYLIST_PATTERNS = [
    (re.compile(r"^\.env(\..+)?$", re.I), "Invincible's .env file"),
    (re.compile(r"^providers\.yaml$", re.I), "provider configuration"),
    (re.compile(r"^sessions\.db$", re.I), "the session database"),
    (re.compile(r"^invincible/", re.I), "Invincible's own source code"),
    (re.compile(r"^tests/", re.I), "the test suite"),
    (re.compile(r"^\.git/", re.I), "git internals"),
]

# Narrower than WRITE_DENYLIST_PATTERNS on purpose: invincible/ and tests/ are
# blocked from being overwritten, but reading them is the entire point of
# giving a cloud AI a read_file tool - it needs to see the code before it
# can usefully write or run anything. providers.yaml only holds api_key_env
# *names*, not actual key values, so it's not a secret either. This list is
# only things that would leak an actual credential or sensitive local state
# if their contents were read out over the tunnel.
READ_DENYLIST_PATTERNS = [
    (re.compile(r"^\.env(\..+)?$", re.I), "Invincible's .env file"),
    (re.compile(r"^sessions\.db$", re.I), "the session database"),
    (re.compile(r"^\.git/", re.I), "git internals"),
]


class ToolBlocked(Exception):
    """Command or write target matched a denylist; never reached confirmation."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ToolDeclined(Exception):
    """Operator typed 'n' (or just hit enter) at the confirmation prompt."""
    pass


def check_denylist(command: str) -> None:
    for pattern, reason in DENYLIST_PATTERNS:
        if pattern.search(command):
            raise ToolBlocked(reason)


def _check_protected_path(path: str, patterns: list, verb: str) -> None:
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, _REPO_ROOT)
    except ValueError:
        return  # different drive on Windows - can't be inside the repo root
    if rel.startswith(".."):
        return  # outside the repo root - confirmation (for writes) is the gate here
    rel = rel.replace(os.sep, "/")
    for pattern, reason in patterns:
        if pattern.match(rel):
            raise ToolBlocked(f"{verb} of {reason} ({rel})")


def check_read_denylist(path: str) -> None:
    _check_protected_path(path, READ_DENYLIST_PATTERNS, "read")


def check_write_denylist(path: str) -> None:
    """Block writes to files this project depends on for its own security
    or state. Only applies to paths that resolve *inside* the repo root -
    a write outside the repo entirely is a different risk profile and is
    left to the confirmation step, same as any other write."""
    _check_protected_path(path, WRITE_DENYLIST_PATTERNS, "write")


async def confirm(prompt: str) -> bool:
    """Block and wait for y/n on the terminal running this server.

    Runs input() in a worker thread via asyncio.to_thread so the event loop
    stays free for other in-flight requests while we wait on the operator.
    """
    def _ask():
        try:
            answer = input(f"{prompt} [y/N]: ").strip().lower()
        except EOFError:
            return False
        return answer in ("y", "yes")

    return await asyncio.to_thread(_ask)


async def execute_bash(command: str, timeout: float = 30.0) -> dict:
    check_denylist(command)  # raises ToolBlocked; caller maps it to a response

    print(f"\n[MCP] Cloud AI wants to run:\n  $ {command}")
    if not await confirm("Allow this command?"):
        raise ToolDeclined()

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"stdout": "", "stderr": f"Command timed out after {timeout}s", "returncode": -1}

        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }
    except Exception as e:
        logger.error(f"execute_bash failed: {e}")
        return {"stdout": "", "stderr": str(e), "returncode": -1}


async def write_file(path: str, content: str) -> dict:
    check_write_denylist(path)  # raises ToolBlocked; caller maps it to a response

    print(f"\n[MCP] Cloud AI wants to write {len(content)} bytes to:\n  {path}")
    if not await confirm("Allow this write?"):
        raise ToolDeclined()

    try:
        dirname = os.path.dirname(os.path.abspath(path))
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return {"status": "written", "path": path, "bytes": len(content)}
    except Exception as e:
        logger.error(f"write_file failed: {e}")
        return {"status": "error", "error": str(e)}


async def read_file(path: str) -> dict:
    """No confirmation prompt - reading isn't destructive, so the friction
    wouldn't buy anything. The denylist is the only gate: it blocks reading
    out actual secrets/state (.env, sessions.db, .git/) but deliberately
    allows reading invincible/ and tests/ and providers.yaml, since letting the
    cloud AI see the code is the entire point of this tool."""
    check_read_denylist(path)  # raises ToolBlocked; caller maps it to a response

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"status": "read", "path": path, "content": content}
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {path}"}
    except IsADirectoryError:
        return {"status": "error", "error": f"Path is a directory, not a file: {path}"}
    except Exception as e:
        logger.error(f"read_file failed: {e}")
        return {"status": "error", "error": str(e)}
