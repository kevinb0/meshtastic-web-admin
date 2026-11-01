"""HTTP/HTTPS MeshInterface implementation for Meshtastic web endpoints (e.g. http://meshtastic.local).

Meshtastic firmware with built-in web/Wi-Fi support (ESP32, Linux native meshtasticd) exposes:
  - PUT /api/v1/toradio       (Content-Type: application/x-protobuf) -> raw serialized ToRadio bytes
  - GET /api/v1/fromradio     (Accept: application/x-protobuf)       -> raw serialized FromRadio bytes (0 bytes when empty)

This module subclasses ``meshtastic.mesh_interface.MeshInterface`` to speak that HTTP protobuf
protocol directly, and also provides an optional ``HookedTCPInterface`` for port-4403 TCP fallback
while preserving raw ``FromRadio`` packet observation for relay/signal tracking.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

import requests
import urllib3
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2
from meshtastic.tcp_interface import DEFAULT_TCP_PORT, TCPInterface

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

PROTOBUF_MIME = "application/x-protobuf"


def normalize_target_url(raw_target: str) -> Tuple[str, str, int]:
    """Normalize a user-entered target (e.g. 'meshtastic.local', 'http://meshtastic.local',
    'https://192.168.1.50', or 'tcp://192.168.1.50:4403').

    Returns:
        (mode, normalized_url_or_host, port)
        where mode is 'http' (covers http & https) or 'tcp'.
    """
    target = (raw_target or "").strip()
    if not target:
        target = "http://meshtastic.local"

    if "://" not in target:
        target = f"http://{target}"

    parsed = urlparse(target)
    scheme = (parsed.scheme or "http").lower()

    if scheme == "tcp":
        host = parsed.hostname or "meshtastic.local"
        port = parsed.port or DEFAULT_TCP_PORT
        return ("tcp", host, port)

    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported connection scheme '{scheme}'. Use http://, https://, or tcp://.")

    if not parsed.netloc:
        raise ValueError(f"Invalid target address: '{raw_target}'")

    base_url = f"{scheme}://{parsed.netloc}".rstrip("/")
    default_port = 443 if scheme == "https" else 80
    return ("http", base_url, parsed.port or default_port)


class HTTPMeshInterface(MeshInterface):
    """Meshtastic ``MeshInterface`` over the HTTP/HTTPS ``/api/v1/toradio`` & ``/api/v1/fromradio`` API."""

    def __init__(
        self,
        base_url: str = "http://meshtastic.local",
        *,
        on_raw_from_radio: Optional[Callable[[mesh_pb2.FromRadio], None]] = None,
        poll_interval: float = 0.20,
        request_timeout: float = 6.0,
        connect_now: bool = True,
        verify_tls: bool = False,
        no_proto: bool = False,
        no_nodes: bool = False,
        timeout: int = 30,
    ) -> None:
        _, normalized_url, _ = normalize_target_url(base_url)
        self.base_url: str = normalized_url
        self.toradio_url: str = f"{self.base_url}/api/v1/toradio"
        self.fromradio_url: str = f"{self.base_url}/api/v1/fromradio?all=false"
        self.poll_interval: float = poll_interval
        self.request_timeout: float = request_timeout
        self.verify_tls: bool = verify_tls
        self.on_raw_from_radio = on_raw_from_radio

        self._session = requests.Session()
        self._http_send_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self.consecutive_errors: int = 0
        self.last_error: Optional[str] = None
        self.last_http_ok_ts: Optional[float] = None

        super().__init__(noProto=no_proto, noNodes=no_nodes, timeout=timeout)

        if connect_now:
            self.connect()

    def connect(self) -> None:
        """Verify HTTP endpoint reachability, start polling thread, and begin Meshtastic config sync."""
        self._stop_event.clear()
        # Drain any stale packet or verify endpoint reachability before starting poll loop
        try:
            resp = self._session.get(
                self.fromradio_url,
                headers={"Accept": PROTOBUF_MIME},
                timeout=self.request_timeout,
                verify=self.verify_tls,
            )
            resp.raise_for_status()
            self.last_http_ok_ts = time.time()
            self.consecutive_errors = 0
            self.last_error = None
        except requests.RequestException as exc:
            raise self.MeshInterfaceError(
                f"Unable to reach Meshtastic HTTP API at {self.base_url}: {exc}"
            ) from exc

        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name=f"meshtastic-http-poll-{self.base_url}",
            daemon=True,
        )
        self._poll_thread.start()

        # Request full NodeDB, localConfig, moduleConfig, and channels from the Admin Node
        self._startConfig()

    def _sendToRadioImpl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Serialize ``ToRadio`` and PUT it to ``/api/v1/toradio``."""
        if self._stop_event.is_set() and not toRadio.disconnect:
            return

        payload = toRadio.SerializeToString()
        if not payload:
            return

        with self._http_send_lock:
            try:
                resp = self._session.put(
                    self.toradio_url,
                    data=payload,
                    headers={"Content-Type": PROTOBUF_MIME, "Accept": PROTOBUF_MIME},
                    timeout=self.request_timeout,
                    verify=self.verify_tls,
                )
                resp.raise_for_status()
                self.last_http_ok_ts = time.time()
                self.consecutive_errors = 0
                self.last_error = None
            except requests.RequestException as exc:
                self.consecutive_errors += 1
                self.last_error = str(exc)
                logger.warning("HTTP PUT /api/v1/toradio failed (%s): %s", self.base_url, exc)
                if not toRadio.disconnect:
                    raise self.MeshInterfaceError(
                        f"Failed sending command to {self.base_url}: {exc}"
                    ) from exc

    def _handleFromRadio(self, fromRadioBytes: bytes) -> None:
        """Observe raw ``FromRadio`` protobuf before delegating to ``MeshInterface._handleFromRadio``."""
        if self.on_raw_from_radio is not None:
            try:
                parsed = mesh_pb2.FromRadio()
                parsed.ParseFromString(fromRadioBytes)
                self.on_raw_from_radio(parsed)
            except Exception as exc:
                logger.debug("on_raw_from_radio hook error: %s", exc)
        super()._handleFromRadio(fromRadioBytes)

    def _poll_loop(self) -> None:
        """Continuously drain ``/api/v1/fromradio?all=false`` packets."""
        while not self._stop_event.is_set():
            try:
                resp = self._session.get(
                    self.fromradio_url,
                    headers={"Accept": PROTOBUF_MIME},
                    timeout=self.request_timeout,
                    verify=self.verify_tls,
                )
                resp.raise_for_status()
                self.last_http_ok_ts = time.time()
                if self.consecutive_errors > 0:
                    self.consecutive_errors = 0
                    self.last_error = None

                body = resp.content
                if body:
                    try:
                        self._handleFromRadio(body)
                    except Exception as parse_exc:
                        logger.warning("Ignoring malformed FromRadio frame (%d bytes): %s", len(body), parse_exc)
                    # Immediately poll again without sleeping so initial NodeDB/config dumps drain fast
                    continue

                self._stop_event.wait(self.poll_interval)
            except requests.RequestException as exc:
                if self._stop_event.is_set():
                    break
                self.consecutive_errors += 1
                self.last_error = str(exc)
                if self.consecutive_errors == 1 or self.consecutive_errors % 10 == 0:
                    logger.warning(
                        "HTTP poll error #%d from %s: %s",
                        self.consecutive_errors,
                        self.base_url,
                        exc,
                    )
                if self.consecutive_errors >= 8 and self.isConnected.is_set():
                    self._disconnected()
                backoff = min(3.0, 0.4 * self.consecutive_errors)
                self._stop_event.wait(backoff)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.exception("Unexpected error in HTTPMeshInterface poll loop: %s", exc)
                self._stop_event.wait(0.5)

    def close(self) -> None:
        """Stop polling, send disconnect notification best-effort, and close HTTP session."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        if self.heartbeatTimer:
            self.heartbeatTimer.cancel()
            self.heartbeatTimer = None

        try:
            self._sendDisconnect()
        except Exception:
            pass

        if self._poll_thread and self._poll_thread.is_alive() and self._poll_thread is not threading.current_thread():
            self._poll_thread.join(timeout=1.5)

        try:
            self._session.close()
        except Exception:
            pass

        self.isConnected.clear()


class HookedTCPInterface(TCPInterface):
    """``TCPInterface`` wrapper that also invokes ``on_raw_from_radio`` for every ``FromRadio`` message."""

    def __init__(
        self,
        hostname: str,
        port_number: int = DEFAULT_TCP_PORT,
        *,
        on_raw_from_radio: Optional[Callable[[mesh_pb2.FromRadio], None]] = None,
        timeout: int = 30,
    ) -> None:
        self.on_raw_from_radio = on_raw_from_radio
        self.last_error: Optional[str] = None
        self.consecutive_errors: int = 0
        self.last_http_ok_ts: Optional[float] = time.time()
        super().__init__(hostname=hostname, portNumber=port_number, timeout=timeout)

    def _handleFromRadio(self, fromRadioBytes: bytes) -> None:
        if self.on_raw_from_radio is not None:
            try:
                parsed = mesh_pb2.FromRadio()
                parsed.ParseFromString(fromRadioBytes)
                self.on_raw_from_radio(parsed)
                self.last_http_ok_ts = time.time()
            except Exception as exc:
                logger.debug("on_raw_from_radio TCP hook error: %s", exc)
        super()._handleFromRadio(fromRadioBytes)
