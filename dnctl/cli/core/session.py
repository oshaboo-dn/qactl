"""SSH transport pool + per-request channel executor for DNOS devices.

Model:
  - One ``paramiko.SSHClient`` (the SSH *transport*, i.e. TCP + auth) is
    cached per ``(device_or_host, user)`` in ``TransportRegistry``.
  - Every tool call opens a **fresh channel** on top of that transport via
    ``run_once`` — runs ``set cli-terminal-length 0``, runs the user
    command, closes the channel.
  - Channels are independent CLI sessions on DNOS: one caller's
    ``configure`` / ``run start-shell`` can't leak into another caller's
    ``show``. The transport is shared, the channel state is not.

First command on every freshly-opened channel is
``set cli-terminal-length 0`` so pagination never fires.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import paramiko

from dnctl.core import devices as _dn_devices
from dnctl.core.cli_probe import (
    DeviceProbe,
    parse_expected_role,
    parse_mgmt0_ipv4,
    parse_ncc_serials,
    parse_system_id,
    parse_system_name,
)
from dnctl.core.cli_probe import probe_via as _probe_via

from .shell import (
    detect_prompt,
    drain,
    send_command,
    send_command_with_commit_conflict,
    send_command_with_confirm,
    send_command_with_password,
    send_config_help,
    send_help,
    send_ncm_cli,
    send_shell_exec,
)


# Device alias -> list of SSH host candidates. Backed by the canonical
# ``<repo>/devices/devices_mgmt0.json`` map (one source of truth shared
# with netconf-mcp); the ``expected_sns`` field on each entry is the SSH
# host list. cli-mcp doesn't care about ``expected_role`` / ``mgmt0`` —
# we only consume the SN hostnames — but we never overwrite those
# fields, so a follow-up ``netconf_add_device`` can fill them in without
# losing what cli-mcp registered. First reachable candidate wins;
# dual-NCC chassis list both NCCs so we fall through when the active
# one flips.


def _load_device_hosts() -> Dict[str, List[str]]:
    """Snapshot the canonical map into ``{alias: [sn, ...]}``.

    Pulls ``expected_sns`` from each entry. Aliases without an SN list
    yet (e.g. the entry was seeded by netconf-mcp before any SSH host
    was discovered) are skipped — cli-mcp can't connect to them anyway.
    """
    out: Dict[str, List[str]] = {}
    data = _dn_devices.load_device_map()
    for alias, entry in (data.get("devices") or {}).items():
        if not isinstance(alias, str) or alias.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        sns = entry.get("expected_sns")
        if not isinstance(sns, list):
            continue
        clean = [s for s in sns if isinstance(s, str) and s]
        if clean:
            out[alias] = clean
    return out


DEVICE_HOSTS: Dict[str, List[str]] = _load_device_hosts()


def reload_device_hosts() -> Dict[str, List[str]]:
    """Refresh the in-memory ``DEVICE_HOSTS`` cache from the canonical map.

    Call this when the on-disk map has been mutated by another process
    (e.g. ``netconf_add_device`` running in netconf-mcp on the same
    host) and cli-mcp wants to pick up the change without a restart.
    The same cache is also kept in sync transparently after every
    :func:`save_device_host` / :func:`remove_device_host` call
    from this process.
    """
    fresh = _load_device_hosts()
    DEVICE_HOSTS.clear()
    DEVICE_HOSTS.update(fresh)
    return dict(DEVICE_HOSTS)


def _refresh_alias_in_cache(alias: str) -> List[str]:
    """Re-read ``alias``'s SN list from the canonical map into the cache.

    The cache is keyed by **canonical** alias (mirroring
    :func:`_load_device_hosts`), so resolve a secondary nickname back to
    its canonical key before touching the cache — otherwise a refresh
    keyed off a nickname would leave a stale entry under the nickname and
    never update the real one.
    """
    canonical = _dn_devices.resolve_canonical(alias) or alias
    entry = _dn_devices.get_device_entry(canonical)
    sns: List[str] = []
    if isinstance(entry, dict):
        raw = entry.get("expected_sns")
        if isinstance(raw, list):
            sns = [s for s in raw if isinstance(s, str) and s]
    if sns:
        DEVICE_HOSTS[canonical] = list(sns)
    else:
        DEVICE_HOSTS.pop(canonical, None)
    # Drop any stale entry left under a secondary nickname.
    if alias != canonical:
        DEVICE_HOSTS.pop(alias, None)
    return list(sns)


def save_device_host(alias: str, sn: str) -> Tuple[bool, List[str]]:
    """Register ``sn`` under device ``alias`` in the canonical map.

    Appends to the alias's ``expected_sns`` list when the alias already
    exists, creates a new entry (with only ``expected_sns`` set)
    otherwise. Idempotent: returns ``added=False`` if ``sn`` is already
    present. Other fields on an existing entry (``expected_role``,
    ``mgmt0``, …) are preserved untouched. Also refreshes the
    in-memory ``DEVICE_HOSTS`` cache.

    Returns (added, hosts_after).
    """
    if not alias or not isinstance(alias, str):
        raise ValueError("alias must be a non-empty string")
    if alias.startswith("_"):
        raise ValueError("alias must not start with '_' (reserved for comments)")
    if not sn or not isinstance(sn, str):
        raise ValueError("sn must be a non-empty string")

    # Write under the canonical key, never a secondary nickname — passing
    # a nickname to update_device would fork a ghost canonical entry. A
    # brand-new name (resolves to nothing) becomes its own canonical key.
    canonical = _dn_devices.resolve_canonical(alias) or alias
    entry = _dn_devices.get_device_entry(canonical) or {}
    current_raw = entry.get("expected_sns") if isinstance(entry, dict) else None
    if current_raw is None:
        current: List[str] = []
    elif isinstance(current_raw, list) and all(isinstance(h, str) for h in current_raw):
        current = list(current_raw)
    else:
        raise ValueError(
            f"devices_mgmt0.json: entry '{canonical}'.expected_sns must be a list of strings"
        )

    added = sn not in current
    hosts_after = list(current) + ([sn] if added else [])
    if added:
        _dn_devices.update_device(canonical, expected_sns=hosts_after)

    _refresh_alias_in_cache(canonical)
    return added, hosts_after


def remove_device_host(alias: str, sn: Optional[str] = None) -> Tuple[bool, List[str]]:
    """Remove a host (``sn``) or the whole ``alias`` from the canonical map.

    - ``sn=None``     : drop the alias entirely (every field, not just SNs).
    - ``sn`` provided : drop that one host from ``expected_sns``; if the
                        list becomes empty the entry is removed too. Other
                        fields on the entry are preserved otherwise.

    Idempotent. Returns ``(changed, remaining_hosts)`` where
    ``remaining_hosts`` is the post-op list (``[]`` if the alias is
    gone). Also refreshes the in-memory ``DEVICE_HOSTS`` cache.
    """
    if not alias or not isinstance(alias, str):
        raise ValueError("name must be a non-empty string")

    # Resolve a secondary nickname to its canonical key first. Operating
    # on the raw nickname would make remove_device a silent no-op (it only
    # pops canonical keys) while we report success, and update_device would
    # fork a ghost entry under the nickname.
    canonical = _dn_devices.resolve_canonical(alias) or alias
    entry = _dn_devices.get_device_entry(canonical)
    if entry is None:
        return False, []

    current_raw = entry.get("expected_sns") if isinstance(entry, dict) else None
    if current_raw is None:
        current: List[str] = []
    elif isinstance(current_raw, list) and all(isinstance(h, str) for h in current_raw):
        current = list(current_raw)
    else:
        raise ValueError(
            f"devices_mgmt0.json: entry '{canonical}'.expected_sns must be a list of strings"
        )

    if sn is None:
        _dn_devices.remove_device(canonical)
        _refresh_alias_in_cache(canonical)
        return True, []

    if sn not in current:
        return False, list(current)

    remaining = [h for h in current if h != sn]
    if not remaining:
        _dn_devices.remove_device(canonical)
    else:
        _dn_devices.update_device(canonical, expected_sns=remaining)
    _refresh_alias_in_cache(canonical)
    return True, remaining


from dnctl.core import credentials as _creds

DEFAULT_USER = _creds.DNROOT_USER
DEFAULT_PASSWORD = _creds.DNROOT_PASSWORD
DEFAULT_CONNECT_TIMEOUT = 15
DEFAULT_CMD_TIMEOUT = 30
DEFAULT_IDLE_MAX = 1800  # seconds — idle transports are reaped after this.
DEFAULT_INIT_TIMEOUT = 10.0
DEFAULT_BANNER_WAIT = 2.0
# Total budget for coaxing a CLI prompt out of a freshly-opened channel.
# A responsive box lands a prompt inside the first ``DEFAULT_BANNER_WAIT``
# window; a slow-to-print one (odd login banner / MOTD / sluggish PTY, e.g.
# DNAAS-LEAF-B13) needs a few extra nudges. We keep re-draining + nudging
# until this budget is spent before declaring the prompt undetectable.
# Override per-environment with ``DNCTL_CLI_PROMPT_TIMEOUT`` (seconds), and
# the per-drain window with ``DNCTL_CLI_BANNER_WAIT``.
DEFAULT_PROMPT_TIMEOUT = 15.0


def _env_float(name: str, default: float) -> float:
    """Read a positive float from env ``name``; fall back to ``default``.

    Anything missing, unparseable, or non-positive yields ``default`` — a bad
    knob value must never make prompt detection give up faster than the
    built-in behaviour.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


class ConnectError(RuntimeError):
    """Raised when we cannot establish the SSH transport (TCP or auth)."""


@dataclass
class Transport:
    """A cached SSH connection (TCP + auth). Holds no CLI state itself."""

    key: Tuple[str, str]              # (device_or_host, user)
    device: Optional[str]
    host: str
    user: str
    client: paramiko.SSHClient
    last_used: float = field(default_factory=time.time)
    # >0 while a command/sequence is actively running on this transport.
    # The idle reaper must never close a busy transport: a single long
    # step (e.g. a 2-hour ``target-stack load``) doesn't touch
    # ``last_used`` mid-read, so without this flag it would be reaped
    # ~30 min in and the download would die under us.
    in_use: int = 0

    def close(self, reason: str = "closed") -> None:  # noqa: ARG002 - reason is for log parity
        try:
            self.client.close()
        except Exception:
            pass

    def is_alive(self) -> bool:
        try:
            t = self.client.get_transport()
            return bool(t and t.is_active())
        except Exception:
            return False


class TransportRegistry:
    """Pool of SSH transports keyed by ``(device_or_host, user)``.

    Callers go through :func:`run_once` below; the registry itself only
    guarantees that a live, authenticated SSH connection exists for the
    key. No CLI state is cached — channels are opened per call.
    """

    def __init__(self, idle_max: float = DEFAULT_IDLE_MAX) -> None:
        self._transports: Dict[Tuple[str, str], Transport] = {}
        self._key_locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._registry_lock = threading.Lock()
        self._idle_max = idle_max
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    def _key_lock(self, key: Tuple[str, str]) -> threading.Lock:
        with self._registry_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def get(
        self,
        device: Optional[str],
        host: Optional[str],
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASSWORD,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ) -> Transport:
        """Return a live transport for the key, opening one if needed."""
        if not device and not host:
            raise ValueError("Must provide device or host")
        key = (device or host or "", user)
        # Per-key lock serialises concurrent first-openers so we only auth once.
        with self._key_lock(key):
            t = self._transports.get(key)
            if t and t.is_alive():
                t.last_used = time.time()
                return t
            if t:
                t.close(reason="stale")
                self._transports.pop(key, None)
            t = _open_transport(
                device=device,
                host=host,
                user=user,
                password=password,
                connect_timeout=connect_timeout,
            )
            self._transports[key] = t
            return t

    def _mark(self, transport: "Transport", delta: int) -> None:
        """Adjust a transport's in-flight counter under the registry lock."""
        with self._registry_lock:
            transport.in_use = max(0, transport.in_use + delta)

    def drop(self, key: Tuple[str, str], reason: str = "dropped") -> bool:
        """Close and forget the transport for ``key`` (if any)."""
        with self._key_lock(key):
            t = self._transports.pop(key, None)
        if not t:
            return False
        t.close(reason=reason)
        return True

    def close_all(self, reason: str = "shutdown") -> None:
        with self._registry_lock:
            items = list(self._transports.items())
            self._transports.clear()
        for _, t in items:
            t.close(reason=reason)

    def list_open(self) -> List[Dict[str, object]]:
        with self._registry_lock:
            return [
                {
                    "device": t.device,
                    "host": t.host,
                    "user": t.user,
                    "last_used_s_ago": int(time.time() - t.last_used),
                    "alive": t.is_alive(),
                }
                for t in self._transports.values()
            ]

    def _select_stale(self, now: float) -> List[Tuple[str, str]]:
        """Keys eligible for reaping: idle-past-max or dead, AND not busy.

        A transport with ``in_use > 0`` is mid-command and must never be
        reaped — closing its client would kill the in-flight channel
        (this is what used to truncate long ``target-stack load`` reads).
        """
        with self._registry_lock:
            return [
                k for k, t in self._transports.items()
                if t.in_use == 0
                and (now - t.last_used > self._idle_max or not t.is_alive())
            ]

    def _reap_loop(self) -> None:
        while True:
            time.sleep(60)
            for k in self._select_stale(time.time()):
                self.drop(k, reason="idle-reap")


def _try_connect_host(
    host: str,
    user: str,
    password: str,
    timeout: int,
) -> paramiko.SSHClient:
    key = _creds.SSH_KEY
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        key_filename=key,
        look_for_keys=bool(key),
        allow_agent=bool(key),
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    return client


def _open_transport(
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    connect_timeout: int,
) -> Transport:
    """Open TCP+auth only. No channel, no CLI state."""
    if device:
        candidates = DEVICE_HOSTS.get(device)
        if not candidates:
            raise ConnectError(f"Unknown device: {device}")
    else:
        assert host
        candidates = [host]

    last_err: Optional[Exception] = None
    client: Optional[paramiko.SSHClient] = None
    chosen_host = ""
    for cand in candidates:
        try:
            client = _try_connect_host(cand, user, password, connect_timeout)
            chosen_host = cand
            break
        except Exception as exc:
            last_err = exc
    if client is None:
        raise ConnectError(
            f"Could not connect to {device or host}: {last_err}"
        )

    return Transport(
        key=((device or host or ""), user),
        device=device,
        host=chosen_host,
        user=user,
        client=client,
    )


def _init_channel(
    channel: paramiko.Channel,
    prompt_timeout: Optional[float] = None,
    banner_wait: Optional[float] = None,
    dialect: Optional[object] = None,
) -> str:
    """Drain banner, detect prompt, disable pagination. Returns the prompt.

    Prompt detection on a fresh channel is best-effort and timing-sensitive:
    a responsive box paints its prompt inside the first short banner-drain
    window, but a slow-to-print one (long login banner / MOTD, sluggish PTY,
    odd prompt-print timing — e.g. DNAAS-LEAF-B13) can miss it. Rather than
    declaring failure after a single nudge, we re-drain and nudge (send a
    bare newline to coax a fresh prompt) in a bounded loop until either a
    prompt appears or the overall budget is spent. Fast boxes stay fast —
    ``drain(stop_on_prompt=True)`` bails in ~100-300 ms once the prompt lands
    — while slow boxes get the extra time they need before we give up.

    The detection budget resolves as: explicit ``prompt_timeout`` /
    ``banner_wait`` arg (when positive) > the ``DNCTL_CLI_PROMPT_TIMEOUT`` /
    ``DNCTL_CLI_BANNER_WAIT`` env knobs > the built-in
    :data:`DEFAULT_PROMPT_TIMEOUT` / :data:`DEFAULT_BANNER_WAIT`. A
    non-positive or missing arg falls through to the env/default so a bad
    knob can never make detection give up faster than the baseline.
    """
    banner_wait = (
        banner_wait if (banner_wait and banner_wait > 0)
        else _env_float("DNCTL_CLI_BANNER_WAIT", DEFAULT_BANNER_WAIT)
    )
    total_timeout = (
        prompt_timeout if (prompt_timeout and prompt_timeout > 0)
        else _env_float("DNCTL_CLI_PROMPT_TIMEOUT", DEFAULT_PROMPT_TIMEOUT)
    )

    deadline = time.time() + total_timeout
    banner = drain(channel, max_wait=banner_wait, stop_on_prompt=True, dialect=dialect)
    prompt = detect_prompt(banner, dialect=dialect)
    # Bounded nudge/backoff loop: keep poking the channel for a prompt until
    # the budget is exhausted. The first iteration runs even if the deadline
    # has already passed (a zero/tiny banner_wait shouldn't skip every nudge).
    while not prompt and time.time() < deadline:
        channel.send("\n")
        banner += drain(channel, max_wait=banner_wait, stop_on_prompt=True, dialect=dialect)
        prompt = detect_prompt(banner, dialect=dialect)
    if not prompt:
        raise RuntimeError("Could not detect CLI prompt on fresh channel")

    # Disable output pagination. DNOS uses a single
    # ``set cli-terminal-length 0``; other vendors supply their own
    # command(s) via the dialect (Cisco ``terminal length 0``, Junos
    # ``set cli screen-length 0``). Falls back to the DNOS command when no
    # dialect is given so legacy callers are unchanged.
    page_off = (
        dialect.page_off if dialect is not None else ("set cli-terminal-length 0",)
    )
    for init_cmd in page_off:
        _, _, _, hit = send_command(
            channel, init_cmd, prompt,
            overall_timeout=DEFAULT_INIT_TIMEOUT, dialect=dialect,
        )
        if not hit:
            raise RuntimeError(f"Timed out waiting for prompt after '{init_cmd}'")
    return prompt


@dataclass
class StepCapture:
    """One DNOS command's exchange on a shared channel.

    ``head_prompt_line`` is the full rendered prompt+command line as DNOS
    echoed it back (e.g. ``HOST(cfg 21-Apr-2026-09:55:32)# rollback 1``).
    ``output`` is the cleaned body between that echo and the next prompt.
    ``tail_prompt`` is the prompt line that followed. ``hit_prompt`` is
    ``False`` iff the step timed out waiting for that trailing prompt.
    """

    command: str
    head_prompt_line: str
    output: str
    tail_prompt: str
    hit_prompt: bool


@dataclass
class Invocation:
    """Result of a single ``run_once`` / ``run_sequence`` / ``run_sequence_pw`` call.

    For single-command runs ``steps`` contains one entry; for multi-step
    sequences it contains one entry per step executed (and stops early on
    a timeout). ``output`` / ``head_prompt_line`` / ``tail_prompt`` /
    ``hit_prompt`` still reflect the LAST step (for back-compat; unchanged
    by design). Loggers that want the full transcript read ``steps``.
    """

    output: str
    hit_prompt: bool
    head_prompt_line: str
    tail_prompt: str
    host: str
    device: Optional[str]
    steps: List[StepCapture] = field(default_factory=list)


def run_once(
    registry: TransportRegistry,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    command: str,
    timeout: float = DEFAULT_CMD_TIMEOUT,
    mode: str = "command",
    shell_entry: str = "run start shell",
) -> Invocation:
    """Open a fresh channel, run ``command``, close the channel.

    The channel is ephemeral — each call gets its own independent CLI
    session (own prompt, own mode, own pagination). The underlying SSH
    transport is reused across calls via ``registry``.

    ``mode`` selects how ``command`` is delivered to DNOS:
      - ``"command"`` (default): newline-terminated, waits for prompt. Use
        for anything that should actually execute.
      - ``"help"``: sends ``command + " ?"`` WITHOUT a newline, collects
        the context-help block, then clears the buffered prefix with
        Ctrl-U + newline. The base command is never submitted, so this is
        safe for leaf-complete destructive commands.
      - ``"config_help"``: same as ``"help"`` but the channel is first
        pushed into ``configure`` mode so the ``?`` trigger enumerates the
        configuration grammar. The candidate is never modified (only ``?``
        is sent and Ctrl-U wipes it) and the channel is left back in
        operational mode via ``end`` before teardown.
      - ``"shell_exec"``: enters ``shell_entry`` (default
        ``run start shell`` — also supports ``run start shell ncc <id>``,
        ``run start shell ncp <id>``, or ``run start shell ncc <id>
        container <name>``), handles the second password prompt, runs
        ``command`` as a single Linux line, captures its output, then sends
        ``exit`` to return to DNOS. Uses ``password`` for the shell's own
        auth challenge.

    Retries once on a transient transport failure (e.g. the cached
    connection was silently dropped between calls) by discarding and
    reopening the transport.
    """
    if mode not in ("command", "help", "config_help", "shell_exec"):
        raise ValueError(f"invalid mode: {mode!r}")
    # Resolve the vendor dialect for this device (DNOS for unknown /
    # host-only). DNOS's dialect reproduces the legacy defaults, so the
    # DNOS path is unchanged. Lazy import keeps the module import graph
    # acyclic (vendor plugins import dnctl.cli.core.shell).
    from dnctl.cli.vendors.registry import dialect_for_device
    dialect = dialect_for_device(device, host)
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        transport = registry.get(
            device=device, host=host, user=user, password=password,
        )
        registry._mark(transport, 1)
        channel = None
        try:
            channel = transport.client.invoke_shell(width=500, height=1000)
            channel.settimeout(0.5)
            prompt = _init_channel(channel, dialect=dialect)
            if mode == "help":
                output, head, tail, hit = send_help(
                    channel, command, prompt, overall_timeout=timeout,
                )
            elif mode == "config_help":
                output, head, tail, hit = send_config_help(
                    channel, command, prompt, overall_timeout=timeout,
                )
            elif mode == "shell_exec":
                output, head, tail, hit = send_shell_exec(
                    channel, command, password, prompt,
                    overall_timeout=timeout,
                    shell_entry=shell_entry,
                )
            else:
                output, head, tail, hit = send_command(
                    channel, command, prompt, overall_timeout=timeout,
                    dialect=dialect,
                )
            transport.last_used = time.time()
            return Invocation(
                output=output,
                hit_prompt=hit,
                head_prompt_line=head,
                tail_prompt=tail,
                host=transport.host,
                device=transport.device,
                steps=[StepCapture(command, head, output, tail, hit)],
            )
        except (paramiko.SSHException, EOFError, OSError) as exc:
            # Transport probably died under us — drop it and retry once.
            last_exc = exc
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            registry.drop(transport.key, reason="transport-broken")
            if attempt == 2:
                raise
            continue
        finally:
            registry._mark(transport, -1)
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
    # Unreachable (loop either returns or raises), but keeps type-checkers happy.
    raise RuntimeError(f"run_once failed: {last_exc}")


def run_ncm_cli(
    registry: TransportRegistry,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    ncm_commands: List[str],
    shell_entry: str,
    timeout: float = DEFAULT_CMD_TIMEOUT,
    answer: str = "y",
) -> Invocation:
    """Open a fresh channel and drive the NCM nested CLI, then close it.

    Same transport-reuse + retry semantics as :func:`run_once`, but instead
    of a single DNOS command it enters ``shell_entry``
    (``run start shell ncm <id>``) and runs ``ncm_commands`` against the
    NCM switch's own (ICOS-style) CLI via :func:`send_ncm_cli`, returning
    the combined transcript. Interactive ``[y/n]:`` confirms raised by a
    command are answered with ``answer``. The channel is always left back
    at the DNOS prompt (best-effort) before teardown.
    """
    if not ncm_commands:
        raise ValueError("ncm_commands must be non-empty")
    joined = " ; ".join(ncm_commands)
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        transport = registry.get(
            device=device, host=host, user=user, password=password,
        )
        registry._mark(transport, 1)
        channel = None
        try:
            channel = transport.client.invoke_shell(width=500, height=1000)
            channel.settimeout(0.5)
            prompt = _init_channel(channel)
            output, head, tail, hit = send_ncm_cli(
                channel, ncm_commands, password, prompt,
                shell_entry=shell_entry, overall_timeout=timeout,
                answer=answer,
            )
            transport.last_used = time.time()
            return Invocation(
                output=output,
                hit_prompt=hit,
                head_prompt_line=head,
                tail_prompt=tail,
                host=transport.host,
                device=transport.device,
                steps=[StepCapture(joined, head, output, tail, hit)],
            )
        except (paramiko.SSHException, EOFError, OSError) as exc:
            last_exc = exc
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            registry.drop(transport.key, reason="transport-broken")
            if attempt == 2:
                raise
            continue
        finally:
            registry._mark(transport, -1)
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
    raise RuntimeError(f"run_ncm_cli failed: {last_exc}")


def run_sequence(
    registry: TransportRegistry,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    commands: List[str],
    timeout: float = DEFAULT_CMD_TIMEOUT,
    stop_predicate: Optional[Callable[["StepCapture"], bool]] = None,
    auto_confirm: bool = False,
    prompt_timeout: Optional[float] = None,
    banner_wait: Optional[float] = None,
) -> Invocation:
    """Open a fresh channel, run ``commands`` in order, return the last result.

    Same transport-reuse + retry semantics as :func:`run_once`, but runs
    every command in ``commands`` on the SAME ephemeral channel. The channel
    is still closed at the end, so any session-scoped state (e.g. a
    ``set cli-no-confirm`` that disables prompt confirmation) dies with the
    channel and never leaks into other tool calls.

    Only the *last* command's output is returned in the ``Invocation``; the
    earlier ones are treated as session setup. ``hit_prompt=False`` is
    returned if any command in the list times out.

    ``stop_predicate`` (optional) lets the caller abort the sequence early
    based on a step's outcome — it's invoked after every step with the
    just-completed ``StepCapture``, and a truthy return value stops the
    loop (subsequent commands are NOT sent). Useful for "stop on first
    DNOS error" without baking error detection into the transport layer.
    The channel is still closed cleanly on abort. Independent of the
    automatic ``hit_prompt=False`` early-exit.

    ``auto_confirm`` (default ``False``) routes every step through
    :func:`send_command_with_confirm` instead of :func:`send_command`,
    which watches for ``(yes/no)?`` / ``[y/n]?`` prompts mid-command and
    answers them with ``yes\\n``. Use on the GI (Genesis Image) shell
    where ``set cli-no-confirm`` is not available — without it,
    ``request system target-stack load`` (and similar) wedges the
    channel forever. On deployed DNOS, prefix the sequence with
    ``set cli-no-confirm`` instead and keep ``auto_confirm=False``.

    ``prompt_timeout`` / ``banner_wait`` (optional) widen the fresh-channel
    prompt-detection budget for this call — handed straight to
    :func:`_init_channel`. Use on a box whose prompt is slow/odd enough to
    defeat the default budget; ``None`` keeps the env/default behaviour.
    """
    if not commands:
        raise ValueError("commands must be non-empty")
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        transport = registry.get(
            device=device, host=host, user=user, password=password,
        )
        registry._mark(transport, 1)
        channel = None
        try:
            channel = transport.client.invoke_shell(width=500, height=1000)
            channel.settimeout(0.5)
            prompt = _init_channel(
                channel, prompt_timeout=prompt_timeout, banner_wait=banner_wait,
            )

            steps: List[StepCapture] = []
            last_output = ""
            last_head = ""
            last_tail = ""
            last_hit = True
            for cmd in commands:
                if auto_confirm:
                    output, head, tail, hit = send_command_with_confirm(
                        channel, cmd, prompt, overall_timeout=timeout,
                    )
                else:
                    output, head, tail, hit = send_command(
                        channel, cmd, prompt, overall_timeout=timeout,
                    )
                step = StepCapture(cmd, head, output, tail, hit)
                steps.append(step)
                last_output, last_head, last_tail, last_hit = (
                    output, head, tail, hit,
                )
                # Keep the transport warm mid-sequence: a long scale push
                # can run for many minutes, and the idle reaper would
                # otherwise close the connection under us (it only sees
                # last_used, which is stamped once at acquire time).
                transport.last_used = time.time()
                if not hit:
                    break
                if stop_predicate is not None and stop_predicate(step):
                    break

            transport.last_used = time.time()
            return Invocation(
                output=last_output,
                hit_prompt=last_hit,
                head_prompt_line=last_head,
                tail_prompt=last_tail,
                host=transport.host,
                device=transport.device,
                steps=steps,
            )
        except (paramiko.SSHException, EOFError, OSError) as exc:
            last_exc = exc
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            registry.drop(transport.key, reason="transport-broken")
            if attempt == 2:
                raise
            continue
        finally:
            registry._mark(transport, -1)
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
    raise RuntimeError(f"run_sequence failed: {last_exc}")


def run_sequence_pw(
    registry: TransportRegistry,
    device: Optional[str],
    host: Optional[str],
    user: str,
    password: str,
    commands: List[Tuple[str, Optional[str]]],
    timeout: float = DEFAULT_CMD_TIMEOUT,
    capture_all: bool = False,
    commit_conflict_answer: Optional[str] = None,
) -> Invocation:
    """Same as :func:`run_sequence` but each command can carry a sub-prompt password.

    ``commands`` is a list of ``(cmd, sub_password_or_None)`` pairs. When the
    second element is not ``None`` the command is sent via
    :func:`send_command_with_password`, which watches for a ``Password:``
    prompt emitted mid-command (e.g. the sftp/scp password prompt raised by
    DNOS' ``request file upload`` / ``request file download``) and answers
    it with the supplied secret before waiting for the DNOS prompt.

    Plain commands (second element ``None``) go through the normal
    :func:`send_command` path — unless ``commit_conflict_answer`` is set, in
    which case they go through :func:`send_command_with_commit_conflict`,
    which answers DNOS' live-``commit`` rebase prompt ("another session
    committed, commit/merge-only/abort?") with that answer (typically
    ``abort``) so the channel can't hang on it. Safe for any
    ``configure → commit`` sequence: the prompt only fires on a live commit,
    so non-commit steps behave exactly like :func:`send_command`. The whole
    list runs on one ephemeral channel; any session-scoped state dies with
    the channel.

    ``capture_all=True`` concatenates every command's cleaned stdout (in
    execution order, separated by ``\\n``) into ``Invocation.output``
    instead of returning only the last. Useful when the caller needs to
    inspect the output of a middle step (e.g. ``edit_config`` wants the
    ``commit check`` verdict even though the sequence ends with ``abort``).
    """
    if not commands:
        raise ValueError("commands must be non-empty")
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        transport = registry.get(
            device=device, host=host, user=user, password=password,
        )
        registry._mark(transport, 1)
        channel = None
        try:
            channel = transport.client.invoke_shell(width=500, height=1000)
            channel.settimeout(0.5)
            prompt = _init_channel(channel)

            steps: List[StepCapture] = []
            all_outputs: List[str] = []
            last_output = ""
            last_head = ""
            last_tail = ""
            last_hit = True
            for cmd, sub_pw in commands:
                if sub_pw is None:
                    if commit_conflict_answer is not None:
                        output, head, tail, hit = send_command_with_commit_conflict(
                            channel, cmd, prompt, overall_timeout=timeout,
                            answer=commit_conflict_answer,
                        )
                    else:
                        output, head, tail, hit = send_command(
                            channel, cmd, prompt, overall_timeout=timeout,
                        )
                else:
                    output, head, tail, hit = send_command_with_password(
                        channel, cmd, sub_pw, prompt,
                        overall_timeout=timeout,
                    )
                steps.append(StepCapture(cmd, head, output, tail, hit))
                all_outputs.append(output)
                last_output, last_head, last_tail, last_hit = (
                    output, head, tail, hit,
                )
                # Keep the transport warm mid-sequence (see run_sequence):
                # a large scale deploy sends thousands of statements on one
                # channel and must not be reaped for idleness while busy.
                transport.last_used = time.time()
                if not hit:
                    break

            transport.last_used = time.time()
            combined = "".join(all_outputs) if capture_all else last_output
            return Invocation(
                output=combined,
                hit_prompt=last_hit,
                head_prompt_line=last_head,
                tail_prompt=last_tail,
                host=transport.host,
                device=transport.device,
                steps=steps,
            )
        except (paramiko.SSHException, EOFError, OSError) as exc:
            last_exc = exc
            try:
                if channel is not None:
                    channel.close()
            except Exception:
                pass
            registry.drop(transport.key, reason="transport-broken")
            if attempt == 2:
                raise
            continue
        finally:
            registry._mark(transport, -1)
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
    raise RuntimeError(f"run_sequence_pw failed: {last_exc}")


# DNOS show-system / show-interfaces-management parsers + DeviceProbe
# now live in :mod:`dnctl.core.cli_probe` (one canonical CLI surface
# shared with netconf-mcp). cli-mcp's :func:`probe_device` below is the
# pool-aware adapter: it builds a closure that runs commands through
# the warm :class:`TransportRegistry` and hands it to
# :func:`dnctl.core.cli_probe.probe_via`.


def probe_device(
    registry: "TransportRegistry",
    host: str,
    user: str = "",
    password: str = "",
    timeout: float = DEFAULT_CMD_TIMEOUT,
    allow_missing_name: bool = False,
    discover_location: bool = False,
) -> "DeviceProbe":
    """SSH to ``host`` (via the cli-mcp transport pool) and run the canonical probe.

    Each show command runs on a fresh channel via :func:`run_once` —
    the underlying SSH transport is reused across the two commands by
    the shared :class:`TransportRegistry`, so we only pay for TCP +
    auth once per registration. Parsing of the responses is delegated
    to :func:`dnctl.core.cli_probe.probe_via`.

    Raises :class:`ConnectError` when SSH itself fails (TCP / auth)
    via the underlying ``run_once``; raises ``RuntimeError`` when
    ``show system`` runs but the output doesn't yield a parseable
    ``System Name:`` line — unless     ``allow_missing_name`` is set, in
    which case ``system_name`` comes back ``None`` (the GI-mode
    registration path uses this). Other parsed fields fall back to
    ``None`` silently — the caller decides whether a missing role /
    mgmt0 is fatal for its flow.

    ``discover_location`` runs an extra best-effort
    ``show lldp neighbors`` on the same warm transport and populates
    ``DeviceProbe.location`` (rack / mgmt switch / fabric leaves); it
    never fails the probe.
    """
    if not host:
        raise ValueError("host must be a non-empty string")
    eff_user = user or DEFAULT_USER
    eff_pw = password or DEFAULT_PASSWORD

    def _run_show(cmd: str) -> str:
        inv = run_once(
            registry=registry,
            device=None, host=host, user=eff_user, password=eff_pw,
            command=cmd, timeout=timeout,
        )
        return inv.output

    return _probe_via(
        _run_show,
        allow_missing_name=allow_missing_name,
        discover_location=discover_location,
    )


def resolve_system_name(
    registry: "TransportRegistry",
    host: str,
    user: str = "",
    password: str = "",
    timeout: float = DEFAULT_CMD_TIMEOUT,
) -> str:
    """SSH to ``host`` and return only the device's configured ``System Name``.

    Thin wrapper around :func:`probe_device` for callers that don't
    need the full registration probe (role / mgmt0 / system-id).
    """
    return probe_device(
        registry, host=host, user=user, password=password, timeout=timeout,
    ).system_name


__all__ = [
    "DEVICE_HOSTS",
    "DEFAULT_USER",
    "DEFAULT_PASSWORD",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_CMD_TIMEOUT",
    "ConnectError",
    "Transport",
    "TransportRegistry",
    "Invocation",
    "run_once",
    "run_ncm_cli",
    "run_sequence",
    "run_sequence_pw",
    "save_device_host",
    "remove_device_host",
    "reload_device_hosts",
    "DeviceProbe",
    "parse_system_name",
    "parse_system_id",
    "parse_expected_role",
    "parse_mgmt0_ipv4",
    "parse_ncc_serials",
    "probe_device",
    "resolve_system_name",
]
