"""Per-app proxy routing for LDPlayer instances.

Routes traffic from a specific app (e.g. Facebook) through an external HTTP
proxy while all other apps keep their direct connection.  Uses iptables
owner-marking + a local transparent proxy (redsocks) to achieve per-app
routing without affecting other traffic.

Requirements:
  - Instance must have root enabled (``ldconsole modify --root 1``)
  - ``redsocks`` binary pushed to ``/data/local/tmp/redsocks`` on the device

Flow:
  1. Get the target app's UID via ``dumpsys package``
  2. Push a redsocks config that forwards to the external HTTP proxy
  3. Start redsocks listening on a local port
  4. Add iptables rules: mark the UID's packets, REDIRECT them to redsocks
  5. On teardown: remove iptables rules, kill redsocks, clean up
"""

from __future__ import annotations

import re
import textwrap
import time

from .adb import Adb, AdbError

REDSOCKS_PORT = 12345
REDSOCKS_DEVICE_PATH = "/data/local/tmp"
REDSOCKS_CONFIG_DEVICE = f"{REDSOCKS_DEVICE_PATH}/redsocks.conf"
REDSOCKS_BINARY_DEVICE = f"{REDSOCKS_DEVICE_PATH}/redsocks"
REDSOCKS_LOG_DEVICE = f"{REDSOCKS_DEVICE_PATH}/redsocks.log"

IPTABLES_MARK_CHAIN = "FB_PROXY_MARK"
IPTABLES_REDIRECT_CHAIN = "FB_PROXY_REDIRECT"

#: Relative path to the bundled redsocks binary (ARM64 Android)
from pathlib import Path as _Path
_REDSOCKS_CANDIDATES = [
    _Path(__file__).resolve().parent / "bin" / "redsocks_arm64",
    _Path(__file__).resolve().parent / "bin" / "redsocks",
    _Path(__file__).resolve().parent.parent / "redsocks_arm64",
]


def _find_redsocks_binary() -> _Path | None:
    """Locate a pre-compiled redsocks binary next to the package or in repo root."""
    for p in _REDSOCKS_CANDIDATES:
        if p.is_file():
            return p
    return None


class ProxyError(RuntimeError):
    pass


# ----------------------------------------------------------------------- UID
def _get_app_uid(adb: Adb, index: int, package: str) -> int:
    """Resolve the Linux UID assigned to *package* on the device."""
    try:
        out = adb.shell(index,
                        ["dumpsys", "package", package],
                        timeout=20, discover=True)
    except AdbError as exc:
        raise ProxyError(f"dumpsys package failed: {exc}")

    # userId=10123  (or  uid=10123)
    for line in out.splitlines():
        m = re.search(r"userId=(\d+)", line)
        if m:
            return int(m.group(1))
        m = re.search(r"uid=(\d+)", line)
        if m:
            return int(m.group(1))
    raise ProxyError(
        f"could not determine UID for {package} — "
        f"package may not be installed yet")


def _check_root(adb: Adb, index: int) -> bool:
    """Return True when ``su`` gives a root shell."""
    try:
        out = adb.shell(index, ["su", "-c", "id"], timeout=10, discover=True)
        return "uid=0" in out
    except AdbError:
        return False


# ---------------------------------------------------------------- redsocks
def _redsocks_config_content(proxy_host: str, proxy_port: int) -> str:
    """Generate a redsocks config that forwards to an HTTP proxy."""
    return textwrap.dedent(f"""\
        base {{
            log_debug = off;
            log_info = off;
            log = "stderr";
            daemon = off;
            redirector = iptables;
        }}

        redsocks {{
            local_ip = 127.0.0.1;
            local_port = {REDSOCKS_PORT};
            ip = {proxy_host};
            port = {proxy_port};
            type = http;
            login = "";
            password = "";
        }}
    """)


def _push_redsocks(adb: Adb, index: int, proxy_host: str,
                   proxy_port: int) -> None:
    """Push the redsocks binary and config to the device."""
    binary = _find_redsocks_binary()
    if binary is None:
        raise ProxyError(
            "redsocks binary not found — place a compiled redsocks at "
            "ldplayer/bin/redsocks_arm64 (ARM64 Linux, statically linked)")

    # push binary
    adb.push(index, str(binary), REDSOCKS_BINARY_DEVICE, discover=True)
    adb.shell(index, ["chmod", "755", REDSOCKS_BINARY_DEVICE],
              timeout=10, discover=True)

    # push config
    config = _redsocks_config_content(proxy_host, proxy_port)
    _push_text(adb, index, REDSOCKS_CONFIG_DEVICE, config)


def _push_text(adb: Adb, index: int, remote_path: str, content: str) -> None:
    """Write *content* to *remote_path* on the device via a heredoc."""
    # Escape for safe shell embedding
    escaped = content.replace("\\", "\\\\").replace("'", "'\\''")
    adb.shell(index,
              ["sh", "-c",
               f"cat > '{remote_path}' << 'EOF'\n{content}\nEOF"],
              timeout=10, discover=True)


def _start_redsocks(adb: Adb, index: int) -> None:
    """Kill any stale redsocks, then start a fresh one."""
    # kill stale instance
    adb.shell(index, ["pkill", "-f", "redsocks"], timeout=5, discover=True)
    time.sleep(0.5)

    # start in background, log to file
    adb.shell(
        index,
        ["sh", "-c",
         f"nohup {REDSOCKS_BINARY_DEVICE} -c {REDSOCKS_CONFIG_DEVICE} "
         f"> {REDSOCKS_LOG_DEVICE} 2>&1 &"],
        timeout=10, discover=True,
    )
    time.sleep(1.0)

    # verify it is running
    out = adb.shell(index, ["pgrep", "-f", "redsocks"],
                    timeout=5, discover=True)
    if not out.strip():
        # show log for debugging
        log = adb.shell(index, ["cat", REDSOCKS_LOG_DEVICE],
                        timeout=5, discover=True)
        raise ProxyError(
            f"redsocks failed to start — log:\n{log.strip()}")


# ---------------------------------------------------------------- iptables
def _setup_iptables(adb: Adb, index: int, uid: int) -> None:
    """Add iptables rules that mark packets from *uid* and REDIRECT them
    to the local redsocks port."""
    mark = "0x1"

    # Create custom chains for clean teardown
    _run_iptables(adb, index, ["-N", IPTABLES_MARK_CHAIN])
    _run_iptables(adb, index, ["-N", IPTABLES_REDIRECT_CHAIN])

    # Flush in case rules already exist from a prior incomplete teardown
    _run_iptables(adb, index, ["-F", IPTABLES_MARK_CHAIN])
    _run_iptables(adb, index, ["-F", IPTABLES_REDIRECT_CHAIN])

    # --- mangle table: mark packets from the target UID ---
    _run_iptables(adb, index, [
        "-t", "mangle", "-A", IPTABLES_MARK_CHAIN,
        "-m", "owner", "--uid-owner", str(uid),
        "-j", "MARK", "--set-mark", mark,
    ])
    # Jump into our chain from OUTPUT (mangle)
    _run_iptables(adb, index, [
        "-t", "mangle", "-A", "OUTPUT",
        "-j", IPTABLES_MARK_CHAIN,
    ])

    # --- nat table: REDIRECT marked TCP packets to redsocks ---
    _run_iptables(adb, index, [
        "-t", "nat", "-A", IPTABLES_REDIRECT_CHAIN,
        "-m", "mark", "--mark", mark,
        "-p", "tcp",
        "-j", "REDIRECT", "--to-ports", str(REDSOCKS_PORT),
    ])
    # Jump into our chain from OUTPUT (nat)
    _run_iptables(adb, index, [
        "-t", "nat", "-A", "OUTPUT",
        "-j", IPTABLES_REDIRECT_CHAIN,
    ])


def _teardown_iptables(adb: Adb, index: int) -> None:
    """Remove all iptables rules we added."""
    chains = {
        "mangle": IPTABLES_MARK_CHAIN,
        "nat": IPTABLES_REDIRECT_CHAIN,
    }
    for table, chain in chains.items():
        # remove jump from OUTPUT
        _run_iptables_safe(adb, index, [
            "-t", table, "-D", "OUTPUT", "-j", chain,
        ])
        # flush and delete the custom chain
        _run_iptables_safe(adb, index, ["-t", table, "-F", chain])
        _run_iptables_safe(adb, index, ["-t", table, "-X", chain])


def _run_iptables(adb: Adb, index: int, args: list[str]) -> str:
    """Run an iptables command, raising on failure."""
    return adb.shell(index, ["iptables"] + args, timeout=10, discover=True)


def _run_iptables_safe(adb: Adb, index: int, args: list[str]) -> None:
    """Run an iptables command, silently ignoring errors (for cleanup)."""
    try:
        adb.shell(index, ["iptables"] + args, timeout=10, discover=True)
    except AdbError:
        pass


# ----------------------------------------------------------------- public
def setup_per_app_proxy(adb: Adb, index: int, package: str,
                        proxy_host: str, proxy_port: int) -> None:
    """Route traffic from *package* through the HTTP proxy at
    *proxy_host*:*proxy_port*.  All other apps keep their direct
    connection.

    Raises ``ProxyError`` if root is unavailable, the binary is missing,
    or iptables rules fail to apply.
    """
    if not _check_root(adb, index):
        raise ProxyError(
            f"root is required for per-app proxy — "
            f"run: ldconsole modify --index {index} --root 1")

    uid = _get_app_uid(adb, index, package)
    print(f"[proxy] {package} UID = {uid}", flush=True)

    # push redsocks + config
    _push_redsocks(adb, index, proxy_host, proxy_port)

    # start the transparent proxy
    _start_redsocks(adb, index)
    print(f"[proxy] redsocks listening on 127.0.0.1:{REDSOCKS_PORT} "
          f"-> {proxy_host}:{proxy_port}", flush=True)

    # set up iptables routing
    _setup_iptables(adb, index, uid)
    print(f"[proxy] iptables rules active — {package} traffic "
          f"routed through proxy", flush=True)


def teardown_per_app_proxy(adb: Adb, index: int) -> None:
    """Remove all proxy routing rules and stop redsocks."""
    try:
        _teardown_iptables(adb, index)
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] iptables cleanup warning: {exc}", flush=True)

    try:
        adb.shell(index, ["pkill", "-f", "redsocks"],
                  timeout=5, discover=True)
    except AdbError:
        pass

    # clean up pushed files
    for remote in (REDSOCKS_CONFIG_DEVICE, REDSOCKS_BINARY_DEVICE,
                   REDSOCKS_LOG_DEVICE):
        try:
            adb.shell(index, ["rm", "-f", remote],
                      timeout=5, discover=True)
        except AdbError:
            pass

    print("[proxy] proxy teardown complete", flush=True)


def is_proxy_active(adb: Adb, index: int) -> bool:
    """Return True when our iptables chains are present."""
    try:
        out = adb.shell(index,
                        ["iptables", "-t", "mangle", "-S",
                         IPTABLES_MARK_CHAIN],
                        timeout=10, discover=True)
        return bool(out.strip()) and "No chain" not in out
    except AdbError:
        return False
