"""VLESS → local SOCKS5 bridge for Telegram (Telethon).

Telethon can only talk to SOCKS5 / HTTP / MTProxy proxies — it has no native
understanding of the VLESS (Xray / V2Ray) protocol. To let the parser connect
to Telegram through a VLESS server we:

  1. Parse a ``vless://`` share link into an Xray outbound configuration.
  2. Generate a minimal Xray client config that exposes a *local* SOCKS5
     inbound (127.0.0.1:<port>) and routes all traffic through the VLESS
     outbound.
  3. Launch the bundled ``xray`` binary as a background subprocess.
  4. Hand the local SOCKS5 address back to Telethon as an ordinary proxy.

The ``vless://`` link format (VMessAEAD / REALITY share link) is::

    vless://<uuid>@<host>:<port>?<query>#<remark>

Common query parameters that we honour: ``type`` (transport: tcp/ws/grpc/http),
``security`` (none/tls/reality), ``sni``/``host``, ``fp`` (uTLS fingerprint),
``pbk`` (REALITY public key), ``sid`` (REALITY short id), ``flow``, ``path``,
``serviceName`` and ``alpn``.

The UUID in the link is the VLESS credential (Xray's analogue of a
"login/password"). Some providers additionally protect the link with HTTP basic
auth in the ``<uuid>`` position as ``user:pass`` — both are forwarded to Xray.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

# Keep a reference to spawned processes / temp files so we can clean them up
# on interpreter exit (and so the GC does not kill the proxy mid-run).
_RUNNING: list["VlessBridge"] = []


def is_vless_link(raw: str) -> bool:
    """True when *raw* looks like a ``vless://`` share link."""
    return bool(raw) and raw.strip().lower().startswith("vless://")


def _find_xray_binary() -> str | None:
    """Locate the ``xray`` executable.

    Honours the ``XRAY_BIN`` env override first, then falls back to whatever is
    on ``PATH`` (``xray`` or the older ``v2ray``).
    """
    override = os.environ.get("XRAY_BIN")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    for name in ("xray", "v2ray"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _pick_free_port() -> int:
    """Ask the OS for a free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_vless_link(raw: str) -> dict:
    """Parse a ``vless://`` link into an Xray outbound ``settings``/``stream``.

    Returns a dict with keys: ``outbound`` (full Xray outbound object) and
    ``label`` (human-readable host:port for logging). Raises ``ValueError`` on
    malformed input.
    """
    raw = raw.strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "vless":
        raise ValueError(f"Not a vless:// link: {raw!r}")

    uuid = unquote(parsed.username or "")
    # Some links carry user:pass — Xray's VLESS user id is the username part.
    if not uuid:
        raise ValueError("VLESS link is missing the user id (uuid)")

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise ValueError("VLESS link is missing host or port")

    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    transport = (q.get("type") or "tcp").lower()
    security = (q.get("security") or "none").lower()
    flow = q.get("flow") or ""

    # --- user object --------------------------------------------------------
    user: dict = {"id": uuid, "encryption": q.get("encryption", "none")}
    if flow:
        user["flow"] = flow

    outbound: dict = {
        "protocol": "vless",
        "tag": "vless-out",
        "settings": {
            "vnext": [
                {
                    "address": host,
                    "port": int(port),
                    "users": [user],
                }
            ]
        },
        "streamSettings": _build_stream_settings(transport, security, q, host),
    }

    return {"outbound": outbound, "label": f"{host}:{port}"}


def _build_stream_settings(transport: str, security: str, q: dict, host: str) -> dict:
    """Construct Xray ``streamSettings`` from query parameters."""
    stream: dict = {"network": transport, "security": security}

    # --- TLS / REALITY ------------------------------------------------------
    sni = q.get("sni") or q.get("host") or host
    fp = q.get("fp") or "chrome"
    alpn = q.get("alpn")
    if security == "tls":
        tls: dict = {"serverName": sni, "fingerprint": fp}
        if alpn:
            tls["alpn"] = alpn.split(",")
        if q.get("allowInsecure") in ("1", "true", "True"):
            tls["allowInsecure"] = True
        stream["tlsSettings"] = tls
    elif security == "reality":
        reality: dict = {
            "serverName": sni,
            "fingerprint": fp,
            "publicKey": q.get("pbk", ""),
            "shortId": q.get("sid", ""),
            "spiderX": q.get("spx", ""),
        }
        stream["realitySettings"] = reality

    # --- transport-specific -------------------------------------------------
    if transport == "ws":
        stream["wsSettings"] = {
            "path": q.get("path", "/"),
            "headers": {"Host": q.get("host") or sni},
        }
    elif transport == "grpc":
        stream["grpcSettings"] = {"serviceName": q.get("serviceName", "")}
    elif transport in ("http", "h2"):
        stream["network"] = "http"
        http_settings: dict = {"path": q.get("path", "/")}
        if q.get("host"):
            http_settings["host"] = q.get("host").split(",")
        stream["httpSettings"] = http_settings
    elif transport == "tcp" and q.get("headerType") == "http":
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {"path": [q.get("path", "/")]},
            }
        }

    return stream


class VlessBridge:
    """Manages a local SOCKS5 ↔ VLESS Xray subprocess."""

    def __init__(self, link: str):
        self.link = link
        self.process: subprocess.Popen | None = None
        self.config_path: str | None = None
        self.local_host = "127.0.0.1"
        self.local_port: int | None = None
        self.label: str = ""

    def start(self, wait_timeout: float = 10.0) -> tuple[str, int]:
        """Launch Xray and return the local ``(host, port)`` SOCKS5 endpoint.

        Raises ``RuntimeError`` if the xray binary is missing or the proxy
        does not come up within *wait_timeout* seconds.
        """
        binary = _find_xray_binary()
        if not binary:
            raise RuntimeError(
                "VLESS proxy requires the 'xray' binary, which was not found. "
                "Install it (https://github.com/XTLS/Xray-core) or set XRAY_BIN."
            )

        parsed = parse_vless_link(self.link)
        self.label = parsed["label"]
        self.local_port = _pick_free_port()

        config = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "socks-in",
                    "listen": self.local_host,
                    "port": self.local_port,
                    "protocol": "socks",
                    "settings": {"udp": True, "auth": "noauth"},
                }
            ],
            "outbounds": [parsed["outbound"]],
        }

        fd, self.config_path = tempfile.mkstemp(prefix="xray-vless-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)

        logger.info("Starting VLESS bridge via %s → SOCKS5 %s:%s (server %s)",
                    binary, self.local_host, self.local_port, self.label)

        self.process = subprocess.Popen(
            [binary, "run", "-c", self.config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _RUNNING.append(self)
        atexit.register(self.stop)

        self._wait_until_ready(wait_timeout)
        return self.local_host, self.local_port

    def _wait_until_ready(self, timeout: float) -> None:
        """Poll the local SOCKS5 port until it accepts connections."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                out = ""
                if self.process.stdout is not None:
                    try:
                        out = self.process.stdout.read().decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
                raise RuntimeError(
                    f"xray exited early (code {self.process.returncode}): {out.strip()}"
                )
            try:
                with socket.create_connection(
                    (self.local_host, self.local_port), timeout=0.5
                ):
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(
            f"VLESS bridge did not become ready within {timeout}s "
            f"(local SOCKS5 {self.local_host}:{self.local_port})"
        )

    def stop(self) -> None:
        """Terminate the Xray subprocess and remove the temp config."""
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if self.config_path and os.path.exists(self.config_path):
            try:
                os.remove(self.config_path)
            except OSError:
                pass
        self.config_path = None
        if self in _RUNNING:
            _RUNNING.remove(self)


def start_vless_bridge(link: str) -> tuple[str, int]:
    """Convenience helper: start a VLESS bridge and return ``(host, port)``.

    The bridge keeps running for the lifetime of the process (cleaned up at
    interpreter exit). Intended to be called once and the resulting local
    SOCKS5 endpoint reused for all Telethon connections.
    """
    bridge = VlessBridge(link)
    return bridge.start()
