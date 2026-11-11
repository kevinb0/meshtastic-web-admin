"""Thread-safe Meshtastic Admin Node manager, live RF/relay tracker, Admin Power prober,
and full local/remote node configuration controller."""

from __future__ import annotations

import base64
import collections
import json
import logging
import pathlib
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

import google.protobuf.json_format
from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2, mesh_pb2, module_config_pb2, portnums_pb2
from meshtastic.util import to_node_num
from pubsub import pub

from meshtastic_http import HookedTCPInterface, HTTPMeshInterface, normalize_target_url
from schema_utils import (
    CONFIG_SECTION_ENUM,
    MODULE_SECTION_ENUM,
    apply_dict_to_channel,
    apply_dict_to_protobuf,
    build_full_schema,
    channel_to_dict,
    protobuf_to_dict,
)

logger = logging.getLogger(__name__)

SESSION_PASSKEY_TTL_SECS = 240.0
PORT_SHORT_LABELS: Dict[int, str] = {
    portnums_pb2.PortNum.TEXT_MESSAGE_APP: "TEXT",
    portnums_pb2.PortNum.POSITION_APP: "POSITION",
    portnums_pb2.PortNum.NODEINFO_APP: "NODEINFO",
    portnums_pb2.PortNum.TELEMETRY_APP: "TELEMETRY",
    portnums_pb2.PortNum.ADMIN_APP: "ADMIN",
    portnums_pb2.PortNum.ROUTING_APP: "ROUTING",
    portnums_pb2.PortNum.TRACEROUTE_APP: "TRACEROUTE",
    portnums_pb2.PortNum.NEIGHBORINFO_APP: "NEIGHBOR",
    portnums_pb2.PortNum.WAYPOINT_APP: "WAYPOINT",
    portnums_pb2.PortNum.MAP_REPORT_APP: "MAP_REPORT",
    portnums_pb2.PortNum.STORE_FORWARD_APP: "STORE_FWD",
    portnums_pb2.PortNum.RANGE_TEST_APP: "RANGE_TEST",
    portnums_pb2.PortNum.PAXCOUNTER_APP: "PAXCOUNTER",
    portnums_pb2.PortNum.ALERT_APP: "ALERT",
    portnums_pb2.PortNum.ATAK_PLUGIN: "ATAK",
    portnums_pb2.PortNum.ATAK_FORWARDER: "ATAK_FWD",
}


def format_node_id(node_num: int) -> str:
    return f"!{int(node_num) & 0xFFFFFFFF:08x}"


class MeshAdminManager:
    """Manages pairing to the Admin Node, tracking mesh status/relays, probing Admin Power,
    and reading/writing all parameters on controllable nodes."""

    def __init__(
        self,
        cache_path: Optional[pathlib.Path] = None,
        auto_probe_admin: bool = True,
        probe_interval_secs: float = 4.5,
    ) -> None:
        self._lock = threading.RLock()
        self._admin_op_lock = threading.RLock()
        self._usb_lock = threading.Lock()
        self._iface: Optional[Any] = None
        self._target_input: str = "http://meshtastic.local"
        self._normalized_target: str = "http://meshtastic.local"
        self._connection_mode: str = "http"
        self._connecting: bool = False
        self._last_error: Optional[str] = None
        self._connected_at: Optional[float] = None

        self._cache_path = cache_path or (pathlib.Path(__file__).resolve().parent / "admin_cache.json")
        self._admin_cache: Dict[str, Dict[str, float]] = self._load_admin_cache()

        # Per-node live state keyed by node_num (int)
        self._live_nodes: Dict[int, Dict[str, Any]] = {}
        # Recent text messages across the mesh (newest last)
        self._messages: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        # Cached remote configurations keyed by (node_num, section_key)
        self._remote_config_cache: Dict[Tuple[int, str], Dict[str, Any]] = {}
        self._remote_channels_cache: Dict[int, Dict[int, channel_pb2.Channel]] = {}

        # Polite background Admin Power prober
        self.auto_probe_admin: bool = auto_probe_admin
        self.probe_interval_secs: float = probe_interval_secs
        self._probe_queue: Deque[int] = collections.deque()
        self._queued_set: set[int] = set()
        self._probed_session: Dict[int, str] = {}  # node_num -> 'verified' | 'denied' | 'timeout' | 'probing'
        self._probing_now: Optional[int] = None
        self._stop_worker = threading.Event()

        self._schema = build_full_schema()

        # Keep strong references to pypubsub listeners
        self._sub_receive = self._on_pub_receive
        self._sub_node_updated = self._on_pub_node_updated
        self._sub_conn_est = self._on_pub_connected
        self._sub_conn_lost = self._on_pub_disconnected
        self._sub_client_notif = self._on_pub_notification

        pub.subscribe(self._sub_receive, "meshtastic.receive")
        pub.subscribe(self._sub_node_updated, "meshtastic.node.updated")
        pub.subscribe(self._sub_conn_est, "meshtastic.connection.established")
        pub.subscribe(self._sub_conn_lost, "meshtastic.connection.lost")
        pub.subscribe(self._sub_client_notif, "meshtastic.clientNotification")

        self._probe_thread = threading.Thread(
            target=self._probe_worker_loop,
            name="meshtastic-admin-prober",
            daemon=True,
        )
        self._probe_thread.start()

    # ------------------------------------------------------------------
    # Persistence for confirmed Admin Access pairs
    # ------------------------------------------------------------------
    def _load_admin_cache(self) -> Dict[str, Dict[str, float]]:
        try:
            if self._cache_path.exists():
                raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception as exc:
            logger.warning("Could not load admin cache %s: %s", self._cache_path, exc)
        return {}

    def _save_admin_cache(self) -> None:
        try:
            with self._lock:
                payload = json.dumps(self._admin_cache, indent=2, sort_keys=True)
            self._cache_path.write_text(payload, encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not write admin cache %s: %s", self._cache_path, exc)

    def _mark_admin_verified(self, node_num: int, passkey: Optional[bytes] = None) -> None:
        now = time.time()
        admin_id = self._get_admin_node_id()
        target_id = format_node_id(node_num)
        changed = False
        with self._lock:
            entry = self._ensure_live_entry(node_num)
            entry["has_admin"] = True
            entry["admin_source"] = "local" if node_num == self._get_admin_node_num() else "verified"
            entry["admin_verified_at"] = now
            if passkey:
                entry["session_passkey"] = bytes(passkey)
                entry["passkey_ts"] = now
                if self._iface is not None and self._iface.nodesByNum is not None:
                    nd = self._iface._getOrCreateByNum(node_num)
                    nd["adminSessionPassKey"] = bytes(passkey)
            self._probed_session[node_num] = "verified"

            if admin_id and target_id != admin_id:
                bucket = self._admin_cache.setdefault(admin_id, {})
                if bucket.get(target_id) != now:
                    bucket[target_id] = now
                    changed = True
        if changed:
            self._save_admin_cache()

    # ------------------------------------------------------------------
    # Connection Management
    # ------------------------------------------------------------------
    def connect(self, target: str = "http://meshtastic.local", wait_secs: float = 12.0) -> Dict[str, Any]:
        """Pair to the Admin Node via HTTP/HTTPS (default ``http://meshtastic.local``) or TCP."""
        self.disconnect()

        mode, norm_target, port = normalize_target_url(target)
        with self._lock:
            self._target_input = target.strip() or "http://meshtastic.local"
            self._normalized_target = norm_target if mode == "http" else f"tcp://{norm_target}:{port}"
            self._connection_mode = mode
            self._connecting = True
            self._last_error = None
            self._live_nodes.clear()
            self._messages.clear()
            self._remote_config_cache.clear()
            self._remote_channels_cache.clear()
            self._probe_queue.clear()
            self._queued_set.clear()
            self._probed_session.clear()

        try:
            if mode == "http":
                iface = HTTPMeshInterface(
                    base_url=norm_target,
                    on_raw_from_radio=self._on_raw_from_radio,
                    connect_now=False,
                    timeout=20,
                )
                with self._lock:
                    self._iface = iface
                iface.connect()
            else:
                iface = HookedTCPInterface(
                    hostname=norm_target,
                    port_number=port,
                    on_raw_from_radio=self._on_raw_from_radio,
                    timeout=20,
                )
                with self._lock:
                    self._iface = iface

            iface.isConnected.wait(timeout=wait_secs)
            with self._lock:
                self._connecting = False
                if iface.myInfo is not None:
                    self._connected_at = time.time()
                    admin_num = int(iface.myInfo.my_node_num)
                    self._mark_admin_verified(admin_num)
                    self._restore_cached_admin_flags_locked()
                    self._schedule_initial_probes_locked()
                else:
                    self._last_error = (
                        f"Connected to {self._normalized_target}, waiting for radio NodeDB response. "
                        "Make sure no other meshtastic.local browser tab is open."
                    )
            return self.get_state()
        except Exception as exc:
            with self._lock:
                self._connecting = False
                self._last_error = str(exc)
            try:
                if self._iface is not None:
                    self._iface.close()
            except Exception:
                pass
            with self._lock:
                self._iface = None
            raise

    def disconnect(self) -> None:
        with self._lock:
            iface = self._iface
            self._iface = None
            self._connecting = False
            self._connected_at = None
            self._probe_queue.clear()
            self._queued_set.clear()
            self._probing_now = None
        if iface is not None:
            try:
                iface.close()
            except Exception as exc:
                logger.debug("Error closing MeshInterface: %s", exc)

    def shutdown(self) -> None:
        self._stop_worker.set()
        self.disconnect()

    def _get_admin_node_num(self) -> Optional[int]:
        iface = self._iface
        if iface is not None and iface.myInfo is not None:
            return int(iface.myInfo.my_node_num)
        return None

    def _get_admin_node_id(self) -> Optional[str]:
        num = self._get_admin_node_num()
        return format_node_id(num) if num is not None else None

    def _ensure_live_entry(self, node_num: int) -> Dict[str, Any]:
        entry = self._live_nodes.get(node_num)
        if entry is None:
            entry = {
                "node_num": int(node_num),
                "rx_snr": None,
                "rx_rssi": None,
                "hops_away": None,
                "via_mqtt": False,
                "relay_node_byte": None,
                "last_heard": None,
                "last_packet_type": None,
                "last_packet_ts": None,
                "last_message": None,
                "has_admin": False,
                "admin_source": None,
                "admin_verified_at": None,
                "session_passkey": None,
                "passkey_ts": 0.0,
                "neighbors": [],
                "last_traceroute": None,
                "owner_cache": None,
            }
            self._live_nodes[node_num] = entry
        return entry

    def _restore_cached_admin_flags_locked(self) -> None:
        admin_id = self._get_admin_node_id()
        if not admin_id:
            return
        cached_targets = self._admin_cache.get(admin_id, {})
        for target_id, ts in cached_targets.items():
            try:
                num = to_node_num(target_id)
                entry = self._ensure_live_entry(num)
                if not entry["has_admin"]:
                    entry["has_admin"] = True
                    entry["admin_source"] = "cached"
                    entry["admin_verified_at"] = ts
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Raw FromRadio & PubSub Event Handlers
    # ------------------------------------------------------------------
    def _on_raw_from_radio(self, from_radio: mesh_pb2.FromRadio) -> None:
        now_int = int(time.time())
        with self._lock:
            if from_radio.HasField("my_info"):
                admin_num = int(from_radio.my_info.my_node_num)
                entry = self._ensure_live_entry(admin_num)
                entry["has_admin"] = True
                entry["admin_source"] = "local"
                entry["hops_away"] = 0

            elif from_radio.HasField("node_info"):
                ni = from_radio.node_info
                num = int(ni.num)
                if num and num != 0xFFFFFFFF:
                    entry = self._ensure_live_entry(num)
                    if ni.HasField("hops_away"):
                        entry["hops_away"] = int(ni.hops_away)
                    if ni.via_mqtt:
                        entry["via_mqtt"] = True
                    if ni.snr != 0.0 and entry["rx_snr"] is None:
                        entry["rx_snr"] = round(float(ni.snr), 2)
                    if ni.last_heard > 0:
                        if entry["last_heard"] is None or int(ni.last_heard) > entry["last_heard"]:
                            entry["last_heard"] = int(ni.last_heard)

            elif from_radio.HasField("packet"):
                mp = from_radio.packet
                sender = int(getattr(mp, "from", 0) or 0)
                if sender == 0 or sender == 0xFFFFFFFF:
                    return

                entry = self._ensure_live_entry(sender)
                admin_num = self._get_admin_node_num()
                pkt_ts = int(mp.rx_time) if mp.rx_time > 0 else now_int
                entry["last_heard"] = pkt_ts
                entry["last_packet_ts"] = pkt_ts
                entry["via_mqtt"] = bool(mp.via_mqtt)

                if sender == admin_num:
                    entry["hops_away"] = 0
                    entry["relay_node_byte"] = None
                else:
                    if mp.rx_rssi != 0:
                        entry["rx_rssi"] = int(mp.rx_rssi)
                    if mp.rx_snr != 0.0 or mp.rx_rssi != 0:
                        entry["rx_snr"] = round(float(mp.rx_snr), 2)

                    if mp.hop_start > 0 and mp.hop_start >= mp.hop_limit:
                        hops = int(mp.hop_start - mp.hop_limit)
                        entry["hops_away"] = hops
                        if hops == 0:
                            entry["relay_node_byte"] = None
                        elif mp.relay_node != 0:
                            entry["relay_node_byte"] = int(mp.relay_node) & 0xFF
                    elif mp.relay_node != 0 and (entry.get("hops_away") or 0) > 0:
                        entry["relay_node_byte"] = int(mp.relay_node) & 0xFF

                if mp.HasField("decoded"):
                    port_int = int(mp.decoded.portnum)
                    entry["last_packet_type"] = PORT_SHORT_LABELS.get(
                        port_int,
                        portnums_pb2.PortNum.Name(port_int) if port_int in portnums_pb2.PortNum.values() else str(port_int),
                    )
                    payload = bytes(mp.decoded.payload)

                    if port_int == portnums_pb2.PortNum.TEXT_MESSAGE_APP:
                        text = payload.decode("utf-8", errors="replace")
                        msg_record = {
                            "id": int(mp.id),
                            "from_num": sender,
                            "from_id": format_node_id(sender),
                            "to_num": int(mp.to),
                            "to_id": "^all" if mp.to == 0xFFFFFFFF else format_node_id(int(mp.to)),
                            "channel": int(mp.channel),
                            "text": text,
                            "timestamp": pkt_ts,
                            "rx_snr": entry["rx_snr"],
                            "rx_rssi": entry["rx_rssi"],
                            "hops_away": entry["hops_away"],
                        }
                        entry["last_message"] = msg_record
                        self._messages.append(msg_record)

                    elif port_int == portnums_pb2.PortNum.ADMIN_APP and payload:
                        try:
                            adm = admin_pb2.AdminMessage()
                            adm.ParseFromString(payload)
                            variant = adm.WhichOneof("payload_variant") or ""
                            if adm.session_passkey or variant.endswith("_response"):
                                self._mark_admin_verified(sender, adm.session_passkey or None)
                            if adm.HasField("get_owner_response"):
                                u = adm.get_owner_response
                                entry["owner_cache"] = {
                                    "long_name": u.long_name,
                                    "short_name": u.short_name,
                                    "is_licensed": bool(u.is_licensed),
                                    "is_unmessagable": bool(u.is_unmessagable),
                                }
                        except Exception as exc:
                            logger.debug("Failed parsing incoming AdminMessage: %s", exc)

                    elif port_int == portnums_pb2.PortNum.NEIGHBORINFO_APP and payload:
                        try:
                            ni_pb = mesh_pb2.NeighborInfo()
                            ni_pb.ParseFromString(payload)
                            entry["neighbors"] = [
                                {
                                    "node_num": int(nb.node_id),
                                    "node_id": format_node_id(int(nb.node_id)),
                                    "snr": round(float(nb.snr), 2),
                                }
                                for nb in ni_pb.neighbors
                                if nb.node_id
                            ]
                        except Exception as exc:
                            logger.debug("Failed parsing NeighborInfo: %s", exc)

                    elif port_int == portnums_pb2.PortNum.TRACEROUTE_APP and payload:
                        try:
                            rd = mesh_pb2.RouteDiscovery()
                            rd.ParseFromString(payload)
                            entry["last_traceroute"] = self._format_route_discovery(
                                origin_num=int(mp.to),
                                target_num=sender,
                                rd=rd,
                                ts=pkt_ts,
                            )
                        except Exception as exc:
                            logger.debug("Failed parsing RouteDiscovery: %s", exc)
                else:
                    entry["last_packet_type"] = "PKI_DM" if mp.pki_encrypted else "ENCRYPTED"

                # Queue newly heard remote node for polite Admin Power check if not yet checked
                if (
                    self.auto_probe_admin
                    and admin_num is not None
                    and sender != admin_num
                    and not entry["has_admin"]
                    and sender not in self._probed_session
                ):
                    self._enqueue_probe_locked(sender)

    def _format_route_discovery(
        self,
        origin_num: int,
        target_num: int,
        rd: mesh_pb2.RouteDiscovery,
        ts: int,
    ) -> Dict[str, Any]:
        unk = -128
        forward_nums = [origin_num, *[int(n) for n in rd.route], target_num]
        snr_towards = [
            round(int(s) / 4.0, 2) if int(s) != unk else None
            for s in rd.snr_towards
        ]
        forward_hops: List[Dict[str, Any]] = []
        for idx, n in enumerate(forward_nums):
            snr_val = snr_towards[idx - 1] if (idx > 0 and idx - 1 < len(snr_towards)) else None
            forward_hops.append(
                {
                    "node_num": n,
                    "node_id": format_node_id(n),
                    "name": self._lookup_node_name_locked(n),
                    "snr": snr_val,
                }
            )

        back_nums = [target_num, *[int(n) for n in rd.route_back], origin_num]
        snr_back = [
            round(int(s) / 4.0, 2) if int(s) != unk else None
            for s in rd.snr_back
        ]
        back_hops: List[Dict[str, Any]] = []
        if rd.route_back or rd.snr_back:
            for idx, n in enumerate(back_nums):
                snr_val = snr_back[idx - 1] if (idx > 0 and idx - 1 < len(snr_back)) else None
                back_hops.append(
                    {
                        "node_num": n,
                        "node_id": format_node_id(n),
                        "name": self._lookup_node_name_locked(n),
                        "snr": snr_val,
                    }
                )

        return {
            "timestamp": ts,
            "forward": forward_hops,
            "back": back_hops,
            "relay_hops": max(0, len(forward_hops) - 2),
        }

    def _lookup_node_name_locked(self, node_num: int) -> str:
        iface = self._iface
        if iface is not None and iface.nodesByNum and node_num in iface.nodesByNum:
            u = iface.nodesByNum[node_num].get("user") or {}
            if u.get("longName"):
                return str(u["longName"])
            if u.get("shortName"):
                return str(u["shortName"])
        return format_node_id(node_num)

    def _on_pub_receive(self, packet: Optional[dict] = None, interface: Any = None, **kwargs: Any) -> None:
        if interface is not self._iface or not packet:
            return

    def _on_pub_node_updated(self, node: Optional[dict] = None, interface: Any = None, **kwargs: Any) -> None:
        if interface is not self._iface or not node:
            return
        with self._lock:
            num = node.get("num")
            if num:
                self._ensure_live_entry(int(num))
                self._restore_cached_admin_flags_locked()

    def _on_pub_connected(self, interface: Any = None, **kwargs: Any) -> None:
        if interface is not self._iface:
            return
        with self._lock:
            self._connected_at = time.time()
            self._last_error = None
            admin_num = self._get_admin_node_num()
            if admin_num is not None:
                self._mark_admin_verified(admin_num)
            self._restore_cached_admin_flags_locked()
            self._schedule_initial_probes_locked()

    def _on_pub_disconnected(self, interface: Any = None, **kwargs: Any) -> None:
        if interface is not self._iface:
            return
        with self._lock:
            self._last_error = f"Lost connection to {self._normalized_target}"

    def _on_pub_notification(self, notification: Any = None, interface: Any = None, **kwargs: Any) -> None:
        if interface is not self._iface or notification is None:
            return
        msg = getattr(notification, "message", "")
        if msg:
            logger.info("Radio clientNotification: %s", msg)

    # ------------------------------------------------------------------
    # Admin Power Discovery & Probing
    # ------------------------------------------------------------------
    def _has_legacy_admin_channel_locked(self) -> bool:
        iface = self._iface
        if iface is None or iface.localNode is None or not iface.localNode.channels:
            return False
        return any(
            ch.role != channel_pb2.Channel.Role.DISABLED
            and ch.settings
            and ch.settings.name.strip().lower() == "admin"
            for ch in iface.localNode.channels
        )

    def _node_can_attempt_admin_locked(self, node_num: int) -> bool:
        iface = self._iface
        if iface is None or iface.nodesByNum is None:
            return False
        nd = iface.nodesByNum.get(node_num) or {}
        u = nd.get("user") or {}
        if u.get("publicKey"):
            return True
        return self._has_legacy_admin_channel_locked()

    def _enqueue_probe_locked(self, node_num: int, force: bool = False) -> None:
        admin_num = self._get_admin_node_num()
        if admin_num is not None and node_num == admin_num:
            return
        if not force and node_num in self._queued_set:
            return
        if not force and not self._node_can_attempt_admin_locked(node_num):
            return
        if node_num not in self._queued_set:
            self._probe_queue.append(node_num)
            self._queued_set.add(node_num)

    def _schedule_initial_probes_locked(self) -> None:
        if not self.auto_probe_admin or self._iface is None or not self._iface.nodesByNum:
            return
        now = time.time()
        admin_num = self._get_admin_node_num()
        # Sort recently heard nodes first; only auto-probe nodes heard in the last 6 hours to save LoRa airtime
        candidates: List[Tuple[int, int]] = []
        for num, nd in self._iface.nodesByNum.items():
            if num == admin_num:
                continue
            entry = self._ensure_live_entry(int(num))
            if entry["has_admin"] or int(num) in self._probed_session:
                continue
            lh = int(nd.get("lastHeard") or entry.get("last_heard") or 0)
            if lh > 0 and (now - lh) <= 6 * 3600:
                candidates.append((lh, int(num)))
        candidates.sort(reverse=True)
        for _, num in candidates[:12]:
            self._enqueue_probe_locked(num)

    def probe_all_nodes(self, include_stale: bool = False) -> Dict[str, Any]:
        """Queue Admin Power verification probes for visible remote nodes."""
        queued = 0
        with self._lock:
            if self._iface is None or not self._iface.nodesByNum:
                return {"queued": 0}
            now = time.time()
            admin_num = self._get_admin_node_num()
            for num, nd in self._iface.nodesByNum.items():
                n = int(num)
                if n == admin_num:
                    continue
                lh = int(nd.get("lastHeard") or 0)
                if not include_stale and lh > 0 and (now - lh) > 24 * 3600:
                    continue
                self._probed_session.pop(n, None)
                self._enqueue_probe_locked(n, force=True)
                queued += 1
        return {"queued": queued, "queue_length": len(self._probe_queue)}

    def probe_node_admin(self, node_id: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Immediately probe a single node's Admin Power synchronously by requesting its Owner info."""
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        if admin_num is not None and node_num == admin_num:
            self._mark_admin_verified(node_num)
            return {"node_id": format_node_id(node_num), "has_admin": True, "status": "local"}

        with self._lock:
            self._probing_now = node_num
            self._probed_session[node_num] = "probing"
        try:
            req = admin_pb2.AdminMessage()
            req.get_owner_request = True
            with self._admin_op_lock:
                resp_pkt = self._send_admin_and_wait(
                    node_num=node_num,
                    admin_msg=req,
                    want_data_response=True,
                    timeout=timeout,
                )
            adm_raw = resp_pkt.get("decoded", {}).get("admin", {}).get("raw")
            if adm_raw is not None and adm_raw.HasField("get_owner_response"):
                self._mark_admin_verified(node_num, adm_raw.session_passkey or None)
                return {
                    "node_id": format_node_id(node_num),
                    "has_admin": True,
                    "status": "verified",
                }
            with self._lock:
                self._probed_session[node_num] = "denied"
            return {"node_id": format_node_id(node_num), "has_admin": False, "status": "denied"}
        except TimeoutError:
            with self._lock:
                self._probed_session[node_num] = "timeout"
            return {
                "node_id": format_node_id(node_num),
                "has_admin": bool(self._ensure_live_entry(node_num).get("has_admin")),
                "status": "timeout",
            }
        except Exception as exc:
            with self._lock:
                self._probed_session[node_num] = "denied"
            return {
                "node_id": format_node_id(node_num),
                "has_admin": False,
                "status": "denied",
                "detail": str(exc),
            }
        finally:
            with self._lock:
                if self._probing_now == node_num:
                    self._probing_now = None

    def _probe_worker_loop(self) -> None:
        while not self._stop_worker.is_set():
            next_num: Optional[int] = None
            with self._lock:
                if self._iface is not None and self._iface.isConnected.is_set() and self._probe_queue:
                    next_num = self._probe_queue.popleft()
                    self._queued_set.discard(next_num)

            if next_num is None:
                self._stop_worker.wait(0.8)
                continue

            try:
                self.probe_node_admin(format_node_id(next_num), timeout=8.0)
            except Exception as exc:
                logger.debug("Background admin probe error for %s: %s", format_node_id(next_num), exc)

            self._stop_worker.wait(self.probe_interval_secs)

    # ------------------------------------------------------------------
    # State Snapshot & Relay Topology Builder
    # ------------------------------------------------------------------
    def get_schema(self) -> Dict[str, Any]:
        return self._schema

    def get_state(self) -> Dict[str, Any]:
        """Return full dashboard snapshot: connection status, Admin Node details, visible nodes
        (signal, battery, last message, relay path, Admin Power icon flag), and recent message feed."""
        with self._lock:
            iface = self._iface
            connected = bool(iface is not None and iface.isConnected.is_set())
            admin_num = self._get_admin_node_num()
            admin_info = self._build_admin_node_summary_locked() if iface is not None else None
            nodes = self._build_nodes_list_locked() if iface is not None else []
            http_last_error = getattr(iface, "last_error", None) if iface is not None else None

            direct_count = sum(1 for n in nodes if not n["is_admin_node"] and n["relay"]["link_type"] == "direct")
            relayed_count = sum(1 for n in nodes if not n["is_admin_node"] and n["relay"]["link_type"] == "relayed")
            admin_count = sum(1 for n in nodes if n["has_admin"])

            # Enrich message feed with current sender names
            messages_out: List[Dict[str, Any]] = []
            for m in reversed(self._messages):
                mc = dict(m)
                mc["from_name"] = self._lookup_node_name_locked(m["from_num"])
                mc["to_name"] = "Broadcast" if m["to_num"] == 0xFFFFFFFF else self._lookup_node_name_locked(m["to_num"])
                messages_out.append(mc)

            return {
                "server_time": int(time.time()),
                "connection": {
                    "paired": iface is not None,
                    "connected": connected,
                    "connecting": self._connecting,
                    "mode": self._connection_mode,
                    "target_input": self._target_input,
                    "target": self._normalized_target,
                    "connected_at": self._connected_at,
                    "error": self._last_error or http_last_error,
                    "probe_queue_len": len(self._probe_queue),
                    "probing_node_id": format_node_id(self._probing_now) if self._probing_now else None,
                    "auto_probe_admin": self.auto_probe_admin,
                },
                "admin_node": admin_info,
                "counts": {
                    "total": len(nodes),
                    "remote": max(0, len(nodes) - (1 if admin_num is not None else 0)),
                    "admin_controllable": admin_count,
                    "direct": direct_count,
                    "relayed": relayed_count,
                },
                "nodes": nodes,
                "messages": messages_out[:40],
            }

    def _build_admin_node_summary_locked(self) -> Optional[Dict[str, Any]]:
        iface = self._iface
        if iface is None or iface.myInfo is None:
            return None
        admin_num = int(iface.myInfo.my_node_num)
        nd = (iface.nodesByNum or {}).get(admin_num) or {}
        user = nd.get("user") or {}
        metrics = nd.get("deviceMetrics") or {}
        meta = iface.metadata

        pub_key = user.get("publicKey") or ""
        if not pub_key and iface.localNode and iface.localNode.localConfig:
            sec_pk = bytes(iface.localNode.localConfig.security.public_key)
            if sec_pk:
                pub_key = base64.b64encode(sec_pk).decode("ascii")

        lora_region = None
        lora_preset = None
        if iface.localNode and iface.localNode.localConfig:
            lc = iface.localNode.localConfig.lora
            lora_region = config_pb2.Config.LoRaConfig.RegionCode.Name(lc.region)
            lora_preset = (
                config_pb2.Config.LoRaConfig.ModemPreset.Name(lc.modem_preset)
                if lc.use_preset
                else "CUSTOM"
            )

        fw_ver = getattr(meta, "firmware_version", None) if meta else None
        hw_model = user.get("hwModel")
        if (not hw_model or hw_model == "UNSET") and meta and meta.hw_model in mesh_pb2.HardwareModel.values():
            hw_model = mesh_pb2.HardwareModel.Name(meta.hw_model)

        batt = metrics.get("batteryLevel")
        return {
            "node_num": admin_num,
            "node_id": format_node_id(admin_num),
            "long_name": user.get("longName") or f"Meshtastic {format_node_id(admin_num)[-4:]}",
            "short_name": user.get("shortName") or format_node_id(admin_num)[-4:],
            "hw_model": hw_model or "UNKNOWN",
            "firmware_version": fw_ver,
            "role": user.get("role") or (
                config_pb2.Config.DeviceConfig.Role.Name(iface.localNode.localConfig.device.role)
                if iface.localNode and iface.localNode.localConfig
                else "CLIENT"
            ),
            "public_key": pub_key,
            "has_pkc": bool(getattr(meta, "hasPKC", True)) if meta else bool(pub_key),
            "has_legacy_admin_channel": self._has_legacy_admin_channel_locked(),
            "lora_region": lora_region,
            "lora_preset": lora_preset,
            "battery_level": int(batt) if batt is not None else None,
            "is_powered": batt is not None and int(batt) > 100,
            "voltage": round(float(metrics["voltage"]), 2) if metrics.get("voltage") else None,
            "channel_utilization": round(float(metrics["channelUtilization"]), 2) if metrics.get("channelUtilization") else None,
            "air_util_tx": round(float(metrics["airUtilTx"]), 2) if metrics.get("airUtilTx") else None,
        }

    def _build_nodes_list_locked(self) -> List[Dict[str, Any]]:
        iface = self._iface
        if iface is None:
            return []

        nodes_by_num = dict(iface.nodesByNum or {})
        for live_num in self._live_nodes:
            if live_num not in nodes_by_num and live_num != 0xFFFFFFFF:
                nodes_by_num[live_num] = {
                    "num": live_num,
                    "user": {
                        "id": format_node_id(live_num),
                        "longName": f"Meshtastic {format_node_id(live_num)[-4:]}",
                        "shortName": format_node_id(live_num)[-4:],
                    },
                }

        admin_num = self._get_admin_node_num()
        relay_children: Dict[int, List[Dict[str, str]]] = collections.defaultdict(list)
        intermediate_rows: List[Tuple[int, Dict[str, Any], Dict[str, Any], Dict[str, Any]]] = []

        # First pass: resolve each node's hops & upstream relay node
        for raw_num, nd in nodes_by_num.items():
            num = int(raw_num)
            if num == 0 or num == 0xFFFFFFFF:
                continue
            live = self._ensure_live_entry(num)
            is_admin = admin_num is not None and num == admin_num
            if is_admin and not live["has_admin"]:
                live["has_admin"] = True
                live["admin_source"] = "local"

            relay_info = self._resolve_node_relay_locked(num, nd, live, nodes_by_num, admin_num)
            via = relay_info.get("via_node")
            if via and via.get("node_num"):
                u = nd.get("user") or {}
                child_name = u.get("longName") or u.get("shortName") or format_node_id(num)
                relay_children[int(via["node_num"])].append(
                    {
                        "node_id": format_node_id(num),
                        "name": str(child_name),
                        "hops_away": relay_info.get("hops_away"),
                    }
                )
            intermediate_rows.append((num, nd, live, relay_info))

        # Second pass: assemble final node payloads including downstream 'relaying_for'
        out: List[Dict[str, Any]] = []
        for num, nd, live, relay_info in intermediate_rows:
            is_admin = admin_num is not None and num == admin_num
            u = nd.get("user") or {}
            owner_cache = live.get("owner_cache") or {}
            metrics = nd.get("deviceMetrics") or {}
            pos = nd.get("position") or {}

            long_name = owner_cache.get("long_name") or u.get("longName") or f"Meshtastic {format_node_id(num)[-4:]}"
            short_name = owner_cache.get("short_name") or u.get("shortName") or format_node_id(num)[-4:]
            node_id = u.get("id") or format_node_id(num)

            snr = live["rx_snr"] if live["rx_snr"] is not None else (
                round(float(nd["snr"]), 2) if nd.get("snr") is not None else None
            )
            rssi = live["rx_rssi"]
            batt_raw = metrics.get("batteryLevel")
            batt_int = int(batt_raw) if batt_raw is not None else None
            is_powered = batt_int is not None and batt_int > 100
            batt_pct = min(100, max(0, batt_int)) if (batt_int is not None and not is_powered) else None

            last_heard = live["last_heard"] or (int(nd["lastHeard"]) if nd.get("lastHeard") else None)
            relay_info["relaying_for"] = relay_children.get(num, [])

            probe_state = (
                "local"
                if is_admin
                else ("probing" if self._probing_now == num else self._probed_session.get(num, "unprobed"))
            )

            out.append(
                {
                    "node_num": num,
                    "node_id": node_id,
                    "is_admin_node": is_admin,
                    "has_admin": bool(live["has_admin"]),
                    "admin_source": live.get("admin_source"),
                    "admin_probe_state": probe_state,
                    "has_public_key": bool(u.get("publicKey")),
                    "public_key": u.get("publicKey") or "",
                    "long_name": long_name,
                    "short_name": short_name,
                    "hw_model": u.get("hwModel") or "UNSET",
                    "role": u.get("role") or "CLIENT",
                    "channel": int(nd.get("channel") or 0),
                    "signal": {
                        "snr": snr,
                        "rssi": rssi,
                        "quality": self._classify_signal(snr, rssi, is_admin, relay_info["link_type"]),
                        "is_last_hop": relay_info["link_type"] == "relayed",
                    },
                    "battery": {
                        "level": batt_pct,
                        "raw": batt_int,
                        "is_powered": is_powered,
                        "voltage": round(float(metrics["voltage"]), 2) if metrics.get("voltage") else None,
                        "channel_utilization": round(float(metrics["channelUtilization"]), 2)
                        if metrics.get("channelUtilization") is not None
                        else None,
                        "air_util_tx": round(float(metrics["airUtilTx"]), 2)
                        if metrics.get("airUtilTx") is not None
                        else None,
                        "uptime_seconds": int(metrics["uptimeSeconds"])
                        if metrics.get("uptimeSeconds") is not None
                        else None,
                    },
                    "last_heard": last_heard,
                    "last_packet_type": live.get("last_packet_type"),
                    "last_message": live.get("last_message"),
                    "relay": relay_info,
                    "position": {
                        "latitude": round(float(pos["latitude"]), 6) if pos.get("latitude") is not None else None,
                        "longitude": round(float(pos["longitude"]), 6) if pos.get("longitude") is not None else None,
                        "altitude": int(pos["altitude"]) if pos.get("altitude") is not None else None,
                    },
                }
            )

        # Sort: Admin Node first, then Admin-Controllable nodes, then most recently heard
        out.sort(
            key=lambda item: (
                0 if item["is_admin_node"] else 1,
                0 if item["has_admin"] else 1,
                -(item["last_heard"] or 0),
            )
        )
        return out

    def _resolve_node_relay_locked(
        self,
        num: int,
        nd: Dict[str, Any],
        live: Dict[str, Any],
        nodes_by_num: Dict[int, Dict[str, Any]],
        admin_num: Optional[int],
    ) -> Dict[str, Any]:
        if admin_num is not None and num == admin_num:
            return {
                "link_type": "local",
                "hops_away": 0,
                "via_mqtt": False,
                "relay_byte": None,
                "via_node": None,
                "traceroute": None,
                "neighbors": live.get("neighbors") or [],
            }

        hops = live["hops_away"]
        if hops is None and "hopsAway" in nd:
            hops = int(nd["hopsAway"])
        via_mqtt = bool(live["via_mqtt"] or nd.get("viaMqtt"))
        relay_byte = live.get("relay_node_byte")
        tr = live.get("last_traceroute")

        if via_mqtt and (hops is None or (hops == 0 and live["rx_rssi"] is None)):
            link_type = "mqtt"
        elif hops == 0:
            link_type = "direct"
        elif hops is not None and hops > 0:
            link_type = "relayed"
        else:
            link_type = "unknown"

        via_node: Optional[Dict[str, Any]] = None
        if link_type == "relayed":
            # 1. If we have a Traceroute to this node, the hop closest to the Admin Node (or last hop) is exact
            if tr and len(tr.get("forward") or []) >= 3:
                first_hop = tr["forward"][1]
                last_hop = tr["forward"][-2]
                via_node = {
                    "node_num": first_hop["node_num"],
                    "node_id": first_hop["node_id"],
                    "name": first_hop["name"],
                    "source": "traceroute",
                    "last_hop_name": last_hop["name"] if last_hop["node_num"] != first_hop["node_num"] else None,
                }
            # 2. Match Meshtastic 2.6+/2.7+ MeshPacket.relay_node lowest byte against known nodes
            elif relay_byte is not None and relay_byte != 0:
                candidates: List[Tuple[int, int, int, str]] = []
                for cand_raw, cand_nd in nodes_by_num.items():
                    cand_num = int(cand_raw)
                    if cand_num in (num, admin_num, 0, 0xFFFFFFFF):
                        continue
                    if (cand_num & 0xFF) == relay_byte:
                        cand_live = self._live_nodes.get(cand_num) or {}
                        cand_hops = cand_live.get("hops_away")
                        if cand_hops is None and "hopsAway" in cand_nd:
                            cand_hops = int(cand_nd["hopsAway"])
                        direct_rank = 0 if cand_hops == 0 else (1 if cand_hops is not None else 2)
                        lh = int(cand_live.get("last_heard") or cand_nd.get("lastHeard") or 0)
                        cu = cand_nd.get("user") or {}
                        cname = str(cu.get("longName") or cu.get("shortName") or format_node_id(cand_num))
                        candidates.append((direct_rank, -lh, cand_num, cname))
                if candidates:
                    candidates.sort()
                    _, _, best_num, best_name = candidates[0]
                    via_node = {
                        "node_num": best_num,
                        "node_id": format_node_id(best_num),
                        "name": best_name,
                        "source": "relay_byte",
                        "ambiguous_count": len(candidates),
                    }
            # 3. Check NeighborInfo reports from direct neighbors
            if via_node is None:
                for cand_num, cand_live in self._live_nodes.items():
                    if cand_num in (num, admin_num):
                        continue
                    if cand_live.get("hops_away") == 0:
                        for nb in cand_live.get("neighbors") or []:
                            if nb.get("node_num") == num:
                                via_node = {
                                    "node_num": cand_num,
                                    "node_id": format_node_id(cand_num),
                                    "name": self._lookup_node_name_locked(cand_num),
                                    "source": "neighbor_info",
                                }
                                break
                    if via_node:
                        break

        return {
            "link_type": link_type,
            "hops_away": hops,
            "via_mqtt": via_mqtt,
            "relay_byte": f"0x{relay_byte:02x}" if relay_byte else None,
            "via_node": via_node,
            "traceroute": tr,
            "neighbors": live.get("neighbors") or [],
        }

    @staticmethod
    def _classify_signal(
        snr: Optional[float],
        rssi: Optional[int],
        is_admin: bool,
        link_type: str,
    ) -> str:
        if is_admin:
            return "local"
        if link_type == "mqtt" and snr is None and rssi is None:
            return "mqtt"
        if snr is None and rssi is None:
            return "unknown"
        if (snr is not None and snr >= 5.0) or (rssi is not None and rssi >= -95):
            return "excellent"
        if (snr is not None and snr >= -2.0) or (rssi is not None and rssi >= -110):
            return "good"
        if (snr is not None and snr >= -10.0) or (rssi is not None and rssi >= -120):
            return "fair"
        return "weak"

    # ------------------------------------------------------------------
    # Low-Level Thread-Safe Admin Packet Transport
    # ------------------------------------------------------------------
    def _resolve_node_num(self, node_id: str) -> int:
        if not node_id or node_id == "^local":
            admin_num = self._get_admin_node_num()
            if admin_num is None:
                raise RuntimeError("No Admin Node is currently paired.")
            return admin_num
        return int(to_node_num(node_id))

    def _get_target_transport_params(self, node_num: int) -> Tuple[bool, Optional[bytes], int]:
        """Determine (pki_encrypted, public_key_bytes, channel_index) for sending AdminMessage."""
        iface = self._iface
        if iface is None:
            raise RuntimeError("Not paired to an Admin Node.")
        admin_num = self._get_admin_node_num()
        if admin_num is not None and node_num == admin_num:
            return (True, None, 0)

        nd = (iface.nodesByNum or {}).get(node_num) or {}
        u = nd.get("user") or {}
        pk_b64 = u.get("publicKey")
        if pk_b64:
            try:
                pk_bytes = base64.b64decode(pk_b64)
                if len(pk_bytes) == 32:
                    return (True, pk_bytes, 0)
            except Exception:
                pass

        # Fallback to legacy 'admin' channel if present
        admin_ch = iface.localNode._getAdminChannelIndex() if iface.localNode else 0
        if admin_ch > 0:
            return (False, None, admin_ch)

        return (True, None, 0)

    def _send_admin_and_wait(
        self,
        node_num: int,
        admin_msg: admin_pb2.AdminMessage,
        *,
        want_data_response: bool,
        timeout: float = 14.0,
    ) -> Dict[str, Any]:
        """Send an ``AdminMessage`` to ``node_num`` and wait for its data response or ACK/NAK."""
        iface = self._iface
        if iface is None:
            raise RuntimeError("Not paired to an Admin Node.")

        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num

        # Attach cached session_passkey if present
        with self._lock:
            entry = self._ensure_live_entry(node_num)
            passkey = entry.get("session_passkey")
            if not passkey and iface.nodesByNum and node_num in iface.nodesByNum:
                passkey = iface.nodesByNum[node_num].get("adminSessionPassKey")
            if passkey and not admin_msg.session_passkey:
                admin_msg.session_passkey = bytes(passkey)

        pki_enc, pub_key_bytes, ch_idx = self._get_target_transport_params(node_num)

        # Local Admin Node writes apply immediately without emitting a separate ROUTING_APP ACK packet
        if is_local and not want_data_response:
            iface.sendData(
                admin_msg,
                destinationId=node_num,
                portNum=portnums_pb2.PortNum.ADMIN_APP,
                wantAck=False,
                wantResponse=False,
                channelIndex=0,
                pkiEncrypted=True,
            )
            time.sleep(0.12)
            return {"status": "local_ok"}

        done = threading.Event()
        holder: Dict[str, Any] = {}

        def _on_resp(pkt: dict) -> None:
            # For remote writes over legacy admin channel, ignore local radio's implicit rebroadcast ACK
            routing = pkt.get("decoded", {}).get("routing")
            if (
                not want_data_response
                and not is_local
                and routing is not None
                and routing.get("errorReason", "NONE") == "NONE"
                and int(pkt.get("from", 0)) == admin_num
                and not pki_enc
            ):
                holder["implicit_ack"] = pkt
                return
            holder["packet"] = pkt
            done.set()

        sent_pkt = iface.sendData(
            admin_msg,
            destinationId=node_num,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            wantResponse=want_data_response,
            onResponse=_on_resp,
            onResponseAckPermitted=not want_data_response,
            channelIndex=ch_idx,
            pkiEncrypted=pki_enc,
            publicKey=pub_key_bytes,
        )

        if not done.wait(timeout=timeout):
            iface.responseHandlers.pop(sent_pkt.id, None)
            if "implicit_ack" in holder:
                return holder["implicit_ack"]
            raise TimeoutError(
                f"Timed out after {timeout:.0f}s waiting for response from {format_node_id(node_num)}"
            )

        pkt = holder["packet"]
        routing = pkt.get("decoded", {}).get("routing")
        if routing is not None:
            err = routing.get("errorReason", "NONE")
            if err != "NONE":
                if err in ("TIMEOUT", "MAX_RETRANSMIT", "NO_RESPONSE", "DUTY_CYCLE_LIMIT", "NO_CHANNEL"):
                    raise TimeoutError(
                        f"LoRa delivery timed out ({err}) communicating with {format_node_id(node_num)}"
                    )
                if err in ("UNAUTHORIZED", "PKI_FAILED", "PKI_UNKNOWN_PUBKEY", "BAD_REQUEST"):
                    with self._lock:
                        entry = self._ensure_live_entry(node_num)
                        entry["session_passkey"] = None
                        entry["passkey_ts"] = 0.0
                raise PermissionError(
                    f"Node {format_node_id(node_num)} rejected or failed admin request: {err}"
                )

        # Capture session_passkey from any AdminMessage response
        adm_raw = pkt.get("decoded", {}).get("admin", {}).get("raw")
        if adm_raw is not None and adm_raw.session_passkey:
            self._mark_admin_verified(node_num, adm_raw.session_passkey)

        return pkt

    def _ensure_fresh_session_passkey(self, node_num: int, timeout: float = 15.0) -> bytes:
        """Ensure we hold a fresh Meshtastic 2.5+ ``session_passkey`` (< 240s old) before writing."""
        with self._lock:
            entry = self._ensure_live_entry(node_num)
            pk = entry.get("session_passkey")
            age = time.time() - float(entry.get("passkey_ts") or 0.0)
            if pk and age < SESSION_PASSKEY_TTL_SECS:
                return bytes(pk)

        req = admin_pb2.AdminMessage()
        req.get_config_request = admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG
        pkt = self._send_admin_and_wait(
            node_num=node_num,
            admin_msg=req,
            want_data_response=True,
            timeout=timeout,
        )
        adm_raw = pkt.get("decoded", {}).get("admin", {}).get("raw")
        if adm_raw is not None and adm_raw.session_passkey:
            self._mark_admin_verified(node_num, adm_raw.session_passkey)
            return bytes(adm_raw.session_passkey)

        with self._lock:
            pk = self._ensure_live_entry(node_num).get("session_passkey")
            if pk:
                return bytes(pk)
        return b""

    # ------------------------------------------------------------------
    # Full Node Configuration Read / Write Operations
    # ------------------------------------------------------------------
    def get_owner(self, node_id: str, refresh: bool = False) -> Dict[str, Any]:
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num

        if not refresh:
            with self._lock:
                live = self._ensure_live_entry(node_num)
                if is_local and self._iface and self._iface.getMyUser():
                    u = self._iface.getMyUser()
                    return {
                        "long_name": u.get("longName") or "",
                        "short_name": u.get("shortName") or "",
                        "is_licensed": bool(u.get("isLicensed", False)),
                        "is_unmessagable": bool(u.get("isUnmessagable", False)),
                    }
                if live.get("owner_cache"):
                    return dict(live["owner_cache"])

        with self._admin_op_lock:
            req = admin_pb2.AdminMessage()
            req.get_owner_request = True
            pkt = self._send_admin_and_wait(
                node_num=node_num,
                admin_msg=req,
                want_data_response=True,
                timeout=6.0 if is_local else 14.0,
            )
            user_pb = pkt["decoded"]["admin"]["raw"].get_owner_response
            data = {
                "long_name": user_pb.long_name,
                "short_name": user_pb.short_name,
                "is_licensed": bool(user_pb.is_licensed),
                "is_unmessagable": bool(user_pb.is_unmessagable),
            }
            with self._lock:
                self._ensure_live_entry(node_num)["owner_cache"] = dict(data)
            return data

    def set_owner(self, node_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
        node_num = self._resolve_node_num(node_id)
        long_name = str(values.get("long_name") or "").strip()
        short_name = str(values.get("short_name") or "").strip()[:4]
        if not long_name:
            raise ValueError("Long Name cannot be empty.")
        if not short_name:
            raise ValueError("Short Name cannot be empty.")

        is_licensed = bool(values.get("is_licensed", False))
        is_unmessagable = bool(values.get("is_unmessagable", False))

        with self._admin_op_lock:
            passkey = self._ensure_fresh_session_passkey(node_num)
            msg = admin_pb2.AdminMessage()
            if passkey:
                msg.session_passkey = passkey
            msg.set_owner.long_name = long_name
            msg.set_owner.short_name = short_name
            msg.set_owner.is_licensed = is_licensed
            msg.set_owner.is_unmessagable = is_unmessagable

            self._send_admin_and_wait(
                node_num=node_num,
                admin_msg=msg,
                want_data_response=False,
                timeout=15.0,
            )

            updated = {
                "long_name": long_name,
                "short_name": short_name,
                "is_licensed": is_licensed,
                "is_unmessagable": is_unmessagable,
            }
            with self._lock:
                self._ensure_live_entry(node_num)["owner_cache"] = dict(updated)
                if self._iface and self._iface.nodesByNum and node_num in self._iface.nodesByNum:
                    u = self._iface.nodesByNum[node_num].setdefault("user", {})
                    u["longName"] = long_name
                    u["shortName"] = short_name
                    u["isLicensed"] = is_licensed
                    u["isUnmessagable"] = is_unmessagable
            return updated

    def get_config_section(
        self,
        node_id: str,
        kind: str,
        section: str,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num
        cache_key = (node_num, f"{kind}:{section}")

        if kind == "config":
            if section not in CONFIG_SECTION_ENUM:
                raise ValueError(f"Unknown radio config section '{section}'")
        elif kind == "module":
            if section not in MODULE_SECTION_ENUM:
                raise ValueError(f"Unknown module config section '{section}'")
        else:
            raise ValueError(f"Unsupported config kind '{kind}'")

        # Fast path for local Admin Node (already downloaded during _startConfig unless refresh=True
        # or section is device_ui/tak/statusmessage which might not be in LocalConfig)
        if is_local and not refresh and self._iface and self._iface.localNode:
            container = (
                self._iface.localNode.localConfig
                if kind == "config"
                else self._iface.localNode.moduleConfig
            )
            if hasattr(container, section) and container.HasField(section):
                data = protobuf_to_dict(getattr(container, section))
                data = self._augment_virtual_fields(node_num, kind, section, data, refresh=False)
                return data

        if not refresh:
            with self._lock:
                if cache_key in self._remote_config_cache:
                    return dict(self._remote_config_cache[cache_key])

        with self._admin_op_lock:
            req = admin_pb2.AdminMessage()
            if kind == "config":
                req.get_config_request = CONFIG_SECTION_ENUM[section]
            else:
                req.get_module_config_request = MODULE_SECTION_ENUM[section]

            pkt = self._send_admin_and_wait(
                node_num=node_num,
                admin_msg=req,
                want_data_response=True,
                timeout=6.0 if is_local else 15.0,
            )
            adm_raw = pkt["decoded"]["admin"]["raw"]
            resp_container = (
                adm_raw.get_config_response
                if kind == "config"
                else adm_raw.get_module_config_response
            )
            sub_pb = getattr(resp_container, section)
            data = protobuf_to_dict(sub_pb)
            data = self._augment_virtual_fields(node_num, kind, section, data, refresh=refresh)

            with self._lock:
                self._remote_config_cache[cache_key] = dict(data)
                if is_local and self._iface and self._iface.localNode:
                    local_container = (
                        self._iface.localNode.localConfig
                        if kind == "config"
                        else self._iface.localNode.moduleConfig
                    )
                    if hasattr(local_container, section):
                        getattr(local_container, section).CopyFrom(sub_pb)
            return data

    def _augment_virtual_fields(
        self,
        node_num: int,
        kind: str,
        section: str,
        data: Dict[str, Any],
        refresh: bool,
    ) -> Dict[str, Any]:
        out = dict(data)
        if kind == "config" and section == "position":
            with self._lock:
                nd = (self._iface.nodesByNum or {}).get(node_num) if self._iface else None
                pos = (nd or {}).get("position") or {}
                out["_fixed_latitude"] = round(float(pos["latitude"]), 6) if pos.get("latitude") is not None else 0.0
                out["_fixed_longitude"] = round(float(pos["longitude"]), 6) if pos.get("longitude") is not None else 0.0
                out["_fixed_altitude"] = int(pos["altitude"]) if pos.get("altitude") is not None else 0
        elif kind == "module" and section == "external_notification":
            out.setdefault("_ringtone", "")
            try:
                req = admin_pb2.AdminMessage()
                req.get_ringtone_request = True
                pkt = self._send_admin_and_wait(node_num, req, want_data_response=True, timeout=6.0)
                out["_ringtone"] = pkt["decoded"]["admin"]["raw"].get_ringtone_response
            except Exception:
                pass
        elif kind == "module" and section == "canned_message":
            out.setdefault("_canned_messages", "")
            try:
                req = admin_pb2.AdminMessage()
                req.get_canned_message_module_messages_request = True
                pkt = self._send_admin_and_wait(node_num, req, want_data_response=True, timeout=6.0)
                out["_canned_messages"] = pkt["decoded"]["admin"]["raw"].get_canned_message_module_messages_response
            except Exception:
                pass
        return out

    def set_config_section(
        self,
        node_id: str,
        kind: str,
        section: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num

        if kind == "config":
            if section not in CONFIG_SECTION_ENUM:
                raise ValueError(f"Unknown radio config section '{section}'")
            wrapper = config_pb2.Config()
        elif kind == "module":
            if section not in MODULE_SECTION_ENUM:
                raise ValueError(f"Unknown module config section '{section}'")
            wrapper = module_config_pb2.ModuleConfig()
        else:
            raise ValueError(f"Unsupported config kind '{kind}'")

        sub_pb = getattr(wrapper, section)

        # Seed from cached/current section first so any omitted read-only fields (like public_key) stay intact!
        try:
            current_dict = self.get_config_section(node_id, kind, section, refresh=False)
            apply_dict_to_protobuf(sub_pb, current_dict)
            if kind == "config" and section == "security" and current_dict.get("public_key"):
                sub_pb.public_key = base64.b64decode(current_dict["public_key"])
        except Exception:
            pass

        apply_dict_to_protobuf(sub_pb, values)

        with self._admin_op_lock:
            passkey = self._ensure_fresh_session_passkey(node_num)

            begin_msg = admin_pb2.AdminMessage()
            if passkey:
                begin_msg.session_passkey = passkey
            begin_msg.begin_edit_settings = True
            self._send_admin_and_wait(node_num, begin_msg, want_data_response=False, timeout=14.0)

            set_msg = admin_pb2.AdminMessage()
            if passkey:
                set_msg.session_passkey = passkey
            if kind == "config":
                getattr(set_msg.set_config, section).CopyFrom(sub_pb)
            else:
                getattr(set_msg.set_module_config, section).CopyFrom(sub_pb)
            self._send_admin_and_wait(node_num, set_msg, want_data_response=False, timeout=15.0)

            # Optional companion admin payloads (fixed position, ringtone, canned messages)
            if kind == "config" and section == "position" and values.get("fixed_position"):
                lat = float(values.get("_fixed_latitude") or 0.0)
                lon = float(values.get("_fixed_longitude") or 0.0)
                alt = int(float(values.get("_fixed_altitude") or 0))
                if lat != 0.0 or lon != 0.0:
                    fp_msg = admin_pb2.AdminMessage()
                    if passkey:
                        fp_msg.session_passkey = passkey
                    fp_msg.set_fixed_position.latitude_i = int(round(lat * 1e7))
                    fp_msg.set_fixed_position.longitude_i = int(round(lon * 1e7))
                    fp_msg.set_fixed_position.altitude = alt
                    self._send_admin_and_wait(node_num, fp_msg, want_data_response=False, timeout=14.0)

            if kind == "module" and section == "external_notification" and "_ringtone" in values:
                rt_val = str(values.get("_ringtone") or "")[:230]
                rt_msg = admin_pb2.AdminMessage()
                if passkey:
                    rt_msg.session_passkey = passkey
                rt_msg.set_ringtone_message = rt_val
                self._send_admin_and_wait(node_num, rt_msg, want_data_response=False, timeout=14.0)

            if kind == "module" and section == "canned_message" and "_canned_messages" in values:
                cm_val = str(values.get("_canned_messages") or "")[:200]
                cm_msg = admin_pb2.AdminMessage()
                if passkey:
                    cm_msg.session_passkey = passkey
                cm_msg.set_canned_message_module_messages = cm_val
                self._send_admin_and_wait(node_num, cm_msg, want_data_response=False, timeout=14.0)

            commit_msg = admin_pb2.AdminMessage()
            if passkey:
                commit_msg.session_passkey = passkey
            commit_msg.commit_edit_settings = True
            self._send_admin_and_wait(node_num, commit_msg, want_data_response=False, timeout=14.0)

            saved_dict = protobuf_to_dict(sub_pb)
            for vk in ("_fixed_latitude", "_fixed_longitude", "_fixed_altitude", "_ringtone", "_canned_messages"):
                if vk in values:
                    saved_dict[vk] = values[vk]

            with self._lock:
                self._remote_config_cache[(node_num, f"{kind}:{section}")] = dict(saved_dict)
                if is_local and self._iface and self._iface.localNode:
                    local_container = (
                        self._iface.localNode.localConfig
                        if kind == "config"
                        else self._iface.localNode.moduleConfig
                    )
                    if hasattr(local_container, section):
                        getattr(local_container, section).CopyFrom(sub_pb)

            return saved_dict

    def get_channel(self, node_id: str, index: int, refresh: bool = False) -> Dict[str, Any]:
        if not 0 <= index <= 7:
            raise ValueError("Channel index must be between 0 and 7")
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num

        if is_local and not refresh and self._iface and self._iface.localNode and self._iface.localNode.channels:
            ch = self._iface.localNode.channels[index]
            return channel_to_dict(ch)

        if not refresh:
            with self._lock:
                cached = self._remote_channels_cache.get(node_num, {}).get(index)
                if cached is not None:
                    return channel_to_dict(cached)

        with self._admin_op_lock:
            req = admin_pb2.AdminMessage()
            req.get_channel_request = index + 1  # Meshtastic get_channel_request is 1-based!
            pkt = self._send_admin_and_wait(
                node_num=node_num,
                admin_msg=req,
                want_data_response=True,
                timeout=6.0 if is_local else 15.0,
            )
            ch_pb = pkt["decoded"]["admin"]["raw"].get_channel_response
            ch_copy = channel_pb2.Channel()
            ch_copy.CopyFrom(ch_pb)
            ch_copy.index = index
            with self._lock:
                self._remote_channels_cache.setdefault(node_num, {})[index] = ch_copy
                if is_local and self._iface and self._iface.localNode and self._iface.localNode.channels:
                    self._iface.localNode.channels[index].CopyFrom(ch_copy)
            return channel_to_dict(ch_copy)

    def get_all_channels(self, node_id: str, refresh: bool = False) -> List[Dict[str, Any]]:
        return [self.get_channel(node_id, idx, refresh=refresh) for idx in range(8)]

    def set_channel(self, node_id: str, index: int, data: Dict[str, Any]) -> Dict[str, Any]:
        if not 0 <= index <= 7:
            raise ValueError("Channel index must be between 0 and 7")
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num

        ch_pb = channel_pb2.Channel()
        try:
            existing = self.get_channel(node_id, index, refresh=False)
            apply_dict_to_channel(ch_pb, existing, index)
        except Exception:
            ch_pb.index = index

        apply_dict_to_channel(ch_pb, data, index)
        if index == 0 and ch_pb.role == channel_pb2.Channel.Role.DISABLED:
            ch_pb.role = channel_pb2.Channel.Role.PRIMARY

        with self._admin_op_lock:
            passkey = self._ensure_fresh_session_passkey(node_num)

            begin_msg = admin_pb2.AdminMessage()
            if passkey:
                begin_msg.session_passkey = passkey
            begin_msg.begin_edit_settings = True
            self._send_admin_and_wait(node_num, begin_msg, want_data_response=False, timeout=14.0)

            set_msg = admin_pb2.AdminMessage()
            if passkey:
                set_msg.session_passkey = passkey
            set_msg.set_channel.CopyFrom(ch_pb)
            self._send_admin_and_wait(node_num, set_msg, want_data_response=False, timeout=15.0)

            commit_msg = admin_pb2.AdminMessage()
            if passkey:
                commit_msg.session_passkey = passkey
            commit_msg.commit_edit_settings = True
            self._send_admin_and_wait(node_num, commit_msg, want_data_response=False, timeout=14.0)

            with self._lock:
                self._remote_channels_cache.setdefault(node_num, {})[index] = ch_pb
                if is_local and self._iface and self._iface.localNode and self._iface.localNode.channels:
                    self._iface.localNode.channels[index].CopyFrom(ch_pb)
            return channel_to_dict(ch_pb)

    # ------------------------------------------------------------------
    # Node Actions (Traceroute, Sync Clock, Reboot, Shutdown, Reset NodeDB, Text)
    # ------------------------------------------------------------------
    def run_traceroute(self, node_id: str, hop_limit: int = 5, timeout: float = 18.0) -> Dict[str, Any]:
        iface = self._iface
        if iface is None:
            raise RuntimeError("Not paired to an Admin Node.")
        node_num = self._resolve_node_num(node_id)
        admin_num = self._get_admin_node_num()
        if admin_num is not None and node_num == admin_num:
            raise ValueError("Cannot traceroute the local Admin Node itself.")

        done = threading.Event()
        holder: Dict[str, Any] = {}

        def _on_tr(pkt: dict) -> None:
            holder["packet"] = pkt
            done.set()

        rd = mesh_pb2.RouteDiscovery()
        sent = iface.sendData(
            rd,
            destinationId=node_num,
            portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
            wantAck=True,
            wantResponse=True,
            onResponse=_on_tr,
            hopLimit=max(1, min(7, int(hop_limit))),
        )
        if not done.wait(timeout=timeout):
            iface.responseHandlers.pop(sent.id, None)
            raise TimeoutError(f"Traceroute to {format_node_id(node_num)} timed out after {timeout:.0f}s")

        pkt = holder["packet"]
        routing = pkt.get("decoded", {}).get("routing")
        if routing and routing.get("errorReason", "NONE") != "NONE":
            raise RuntimeError(f"Traceroute failed: {routing['errorReason']}")

        parsed = mesh_pb2.RouteDiscovery()
        parsed.ParseFromString(bytes(pkt["decoded"]["payload"]))
        with self._lock:
            tr_info = self._format_route_discovery(
                origin_num=admin_num or int(pkt.get("to", 0)),
                target_num=node_num,
                rd=parsed,
                ts=int(time.time()),
            )
            self._ensure_live_entry(node_num)["last_traceroute"] = tr_info
        return tr_info

    def perform_action(self, node_id: str, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        if action == "probe_admin":
            return self.probe_node_admin(node_id)
        if action == "traceroute":
            return {"traceroute": self.run_traceroute(node_id, hop_limit=int(params.get("hop_limit", 5)))}

        node_num = self._resolve_node_num(node_id)
        with self._admin_op_lock:
            passkey = self._ensure_fresh_session_passkey(node_num)
            msg = admin_pb2.AdminMessage()
            if passkey:
                msg.session_passkey = passkey

            if action == "sync_time":
                ts = int(params.get("timestamp") or time.time())
                msg.set_time_only = ts
            elif action == "reboot":
                msg.reboot_seconds = max(2, int(params.get("seconds", 5)))
            elif action == "shutdown":
                msg.shutdown_seconds = max(2, int(params.get("seconds", 5)))
            elif action == "reset_nodedb":
                msg.nodedb_reset = True
            elif action == "remove_fixed_position":
                msg.remove_fixed_position = True
            else:
                raise ValueError(f"Unsupported node action '{action}'")

            self._send_admin_and_wait(node_num, msg, want_data_response=False, timeout=15.0)
            return {"ok": True, "action": action, "node_id": format_node_id(node_num)}

    # ------------------------------------------------------------------
    # Batch Fleet Actions & Multi-Section Force Parameter Push
    # ------------------------------------------------------------------
    def apply_batch_to_node(
        self,
        node_id: str,
        *,
        action: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        changes: Optional[List[Dict[str, Any]]] = None,
        blind_overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Push either a fleet action (e.g. ``shutdown``, ``reboot``, ``sync_time``) OR a list of
        multi-section parameter overrides to ``node_id`` inside a single edit transaction."""
        if action:
            return self.perform_action(node_id, action, params=params or {})

        items = [c for c in (changes or []) if isinstance(c, dict)]
        if not items:
            raise ValueError("No batch action or parameter overrides specified.")

        node_num = self._resolve_node_num(node_id)
        canonical_id = format_node_id(node_num)
        admin_num = self._get_admin_node_num()
        is_local = admin_num is not None and node_num == admin_num

        # Prepare protobuf payloads for every staged change before opening the edit transaction
        prepared_steps: List[Tuple[str, Any, Dict[str, Any]]] = []
        for item in items:
            kind = str(item.get("kind") or "").strip()
            values = item.get("values") if isinstance(item.get("values"), dict) else {}
            if not values:
                continue

            if kind in ("config", "module"):
                section = str(item.get("section") or "").strip()
                if kind == "config":
                    if section not in CONFIG_SECTION_ENUM:
                        raise ValueError(f"Unknown radio config section '{section}'")
                    wrapper = config_pb2.Config()
                else:
                    if section not in MODULE_SECTION_ENUM:
                        raise ValueError(f"Unknown module config section '{section}'")
                    wrapper = module_config_pb2.ModuleConfig()

                sub_pb = getattr(wrapper, section)
                if not blind_overwrite:
                    current_dict = self.get_config_section(canonical_id, kind, section, refresh=False)
                    apply_dict_to_protobuf(sub_pb, current_dict)
                    if kind == "config" and section == "security" and current_dict.get("public_key"):
                        try:
                            sub_pb.public_key = base64.b64decode(current_dict["public_key"])
                        except Exception:
                            pass
                elif kind == "config" and section == "security":
                    # Even in blind mode, preserve known public_key from NodeDB if present
                    with self._lock:
                        nd = (self._iface.nodesByNum or {}).get(node_num) if self._iface else None
                        pk_b64 = ((nd or {}).get("user") or {}).get("publicKey")
                        if pk_b64:
                            try:
                                sub_pb.public_key = base64.b64decode(pk_b64)
                            except Exception:
                                pass

                apply_dict_to_protobuf(sub_pb, values)
                prepared_steps.append((kind, (section, sub_pb), values))

            elif kind == "channel":
                index = int(item.get("index", 0))
                if not 0 <= index <= 7:
                    raise ValueError("Channel index must be between 0 and 7")
                ch_pb = channel_pb2.Channel()
                ch_pb.index = index
                if not blind_overwrite:
                    existing = self.get_channel(canonical_id, index, refresh=False)
                    apply_dict_to_channel(ch_pb, existing, index)
                apply_dict_to_channel(ch_pb, values, index)
                if index == 0 and ch_pb.role == channel_pb2.Channel.Role.DISABLED:
                    ch_pb.role = channel_pb2.Channel.Role.PRIMARY
                prepared_steps.append(("channel", (index, ch_pb), values))
            else:
                raise ValueError(f"Unsupported batch change kind '{kind}'")

        if not prepared_steps:
            raise ValueError("No non-empty parameter overrides provided.")

        with self._admin_op_lock:
            passkey = self._ensure_fresh_session_passkey(node_num)

            begin_msg = admin_pb2.AdminMessage()
            if passkey:
                begin_msg.session_passkey = passkey
            begin_msg.begin_edit_settings = True
            self._send_admin_and_wait(node_num, begin_msg, want_data_response=False, timeout=14.0)

            applied_sections: List[str] = []
            for kind, target_obj, values in prepared_steps:
                if kind in ("config", "module"):
                    section, sub_pb = target_obj
                    set_msg = admin_pb2.AdminMessage()
                    if passkey:
                        set_msg.session_passkey = passkey
                    if kind == "config":
                        getattr(set_msg.set_config, section).CopyFrom(sub_pb)
                    else:
                        getattr(set_msg.set_module_config, section).CopyFrom(sub_pb)
                    self._send_admin_and_wait(node_num, set_msg, want_data_response=False, timeout=15.0)

                    # Companion payloads if explicitly included in batch values
                    if kind == "config" and section == "position" and values.get("fixed_position"):
                        if any(k in values for k in ("_fixed_latitude", "_fixed_longitude", "_fixed_altitude")):
                            lat = float(values.get("_fixed_latitude") or 0.0)
                            lon = float(values.get("_fixed_longitude") or 0.0)
                            alt = int(float(values.get("_fixed_altitude") or 0))
                            if lat != 0.0 or lon != 0.0:
                                fp_msg = admin_pb2.AdminMessage()
                                if passkey:
                                    fp_msg.session_passkey = passkey
                                fp_msg.set_fixed_position.latitude_i = int(round(lat * 1e7))
                                fp_msg.set_fixed_position.longitude_i = int(round(lon * 1e7))
                                fp_msg.set_fixed_position.altitude = alt
                                self._send_admin_and_wait(node_num, fp_msg, want_data_response=False, timeout=14.0)

                    if kind == "module" and section == "external_notification" and "_ringtone" in values:
                        rt_msg = admin_pb2.AdminMessage()
                        if passkey:
                            rt_msg.session_passkey = passkey
                        rt_msg.set_ringtone_message = str(values.get("_ringtone") or "")[:230]
                        self._send_admin_and_wait(node_num, rt_msg, want_data_response=False, timeout=14.0)

                    if kind == "module" and section == "canned_message" and "_canned_messages" in values:
                        cm_msg = admin_pb2.AdminMessage()
                        if passkey:
                            cm_msg.session_passkey = passkey
                        cm_msg.set_canned_message_module_messages = str(values.get("_canned_messages") or "")[:200]
                        self._send_admin_and_wait(node_num, cm_msg, want_data_response=False, timeout=14.0)

                    saved_dict = protobuf_to_dict(sub_pb)
                    with self._lock:
                        self._remote_config_cache[(node_num, f"{kind}:{section}")] = dict(saved_dict)
                        if is_local and self._iface and self._iface.localNode:
                            local_container = (
                                self._iface.localNode.localConfig
                                if kind == "config"
                                else self._iface.localNode.moduleConfig
                            )
                            if hasattr(local_container, section):
                                getattr(local_container, section).CopyFrom(sub_pb)
                    applied_sections.append(f"{kind}.{section}")

                elif kind == "channel":
                    index, ch_pb = target_obj
                    set_msg = admin_pb2.AdminMessage()
                    if passkey:
                        set_msg.session_passkey = passkey
                    set_msg.set_channel.CopyFrom(ch_pb)
                    self._send_admin_and_wait(node_num, set_msg, want_data_response=False, timeout=15.0)
                    with self._lock:
                        self._remote_channels_cache.setdefault(node_num, {})[index] = ch_pb
                        if is_local and self._iface and self._iface.localNode and self._iface.localNode.channels:
                            self._iface.localNode.channels[index].CopyFrom(ch_pb)
                    applied_sections.append(f"channel.{index}")

            commit_msg = admin_pb2.AdminMessage()
            if passkey:
                commit_msg.session_passkey = passkey
            commit_msg.commit_edit_settings = True
            self._send_admin_and_wait(node_num, commit_msg, want_data_response=False, timeout=14.0)

            return {
                "ok": True,
                "node_id": canonical_id,
                "applied_sections": applied_sections,
                "blind_overwrite": bool(blind_overwrite),
            }

    def apply_batch(
        self,
        node_ids: List[str],
        *,
        action: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        changes: Optional[List[Dict[str, Any]]] = None,
        blind_overwrite: bool = False,
        max_attempts: int = 1,
        retry_delay: float = 1.5,
    ) -> Dict[str, Any]:
        """Execute a batch action or multi-parameter push across multiple nodes sequentially,
        automatically ordering remote peers before the paired local Admin Node and optionally
        retrying timed-out remote nodes across up to ``max_attempts`` passes."""
        cleaned_ids = [str(nid).strip() for nid in (node_ids or []) if str(nid).strip()]
        if not cleaned_ids:
            raise ValueError("Select at least one node for batch operation.")

        max_passes = max(1, int(max_attempts or 1))
        admin_num = self._get_admin_node_num()
        admin_id = format_node_id(admin_num) if admin_num is not None else None

        # Deduplicate while ensuring remote peers execute BEFORE the paired Admin Node
        seen: set[str] = set()
        remote_ids: List[str] = []
        include_local = False
        for nid in cleaned_ids:
            norm = format_node_id(to_node_num(nid))
            if norm in seen:
                continue
            seen.add(norm)
            if admin_id and norm == admin_id:
                include_local = True
            else:
                remote_ids.append(norm)
        ordered_ids = remote_ids + ([admin_id] if include_local and admin_id else [])

        # Ensure 'random256'/'random128' channel PSKs resolve once so all batch nodes share the same key
        resolved_changes = changes
        if changes:
            import copy
            import secrets

            resolved_changes = copy.deepcopy(changes)
            for chg in resolved_changes:
                if isinstance(chg, dict) and chg.get("kind") == "channel" and isinstance(chg.get("values"), dict):
                    psk_raw = str(chg["values"].get("psk_text") or "").strip().lower()
                    if psk_raw in ("random", "random256"):
                        chg["values"]["psk_text"] = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode("ascii")
                    elif psk_raw == "random128":
                        chg["values"]["psk_text"] = "base64:" + base64.b64encode(secrets.token_bytes(16)).decode("ascii")

        results_by_id: Dict[str, Dict[str, Any]] = {}
        pending_remote = list(remote_ids)
        pass_num = 0

        while pending_remote and pass_num < max_passes:
            pass_num += 1
            if pass_num > 1 and retry_delay > 0:
                time.sleep(retry_delay)
            next_pending: List[str] = []
            for nid in pending_remote:
                prev_attempts = int((results_by_id.get(nid) or {}).get("attempts") or 0)
                attempt_no = prev_attempts + 1
                try:
                    res = self.apply_batch_to_node(
                        nid,
                        action=action,
                        params=params,
                        changes=resolved_changes,
                        blind_overwrite=blind_overwrite,
                    )
                    results_by_id[nid] = {
                        "node_id": nid,
                        "ok": True,
                        "attempts": attempt_no,
                        "result": res,
                    }
                except TimeoutError as exc:
                    results_by_id[nid] = {
                        "node_id": nid,
                        "ok": False,
                        "attempts": attempt_no,
                        "error": str(exc),
                    }
                    if pass_num < max_passes:
                        next_pending.append(nid)
                except Exception as exc:
                    results_by_id[nid] = {
                        "node_id": nid,
                        "ok": False,
                        "attempts": attempt_no,
                        "error": str(exc),
                    }
            pending_remote = next_pending

        # Execute local Admin Node LAST after all remote passes finish
        if include_local and admin_id:
            try:
                res = self.apply_batch_to_node(
                    admin_id,
                    action=action,
                    params=params,
                    changes=resolved_changes,
                    blind_overwrite=blind_overwrite,
                )
                results_by_id[admin_id] = {
                    "node_id": admin_id,
                    "ok": True,
                    "attempts": 1,
                    "result": res,
                }
            except Exception as exc:
                results_by_id[admin_id] = {
                    "node_id": admin_id,
                    "ok": False,
                    "attempts": 1,
                    "error": str(exc),
                }

        results = [results_by_id[nid] for nid in ordered_ids if nid in results_by_id]
        succeeded = sum(1 for r in results if r.get("ok"))

        return {
            "total": len(ordered_ids),
            "succeeded": succeeded,
            "failed": len(ordered_ids) - succeeded,
            "results": results,
        }

    def send_text_message(
        self,
        text: str,
        destination_id: str = "^all",
        channel_index: int = 0,
    ) -> Dict[str, Any]:
        iface = self._iface
        if iface is None:
            raise RuntimeError("Not paired to an Admin Node.")
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Message text cannot be empty.")

        dest: Any = "^all" if destination_id in ("", "^all", "broadcast") else to_node_num(destination_id)
        sent = iface.sendText(cleaned, destinationId=dest, wantAck=True, channelIndex=int(channel_index))
        admin_num = self._get_admin_node_num() or 0
        now_ts = int(time.time())
        record = {
            "id": int(sent.id),
            "from_num": admin_num,
            "from_id": format_node_id(admin_num),
            "to_num": 0xFFFFFFFF if dest == "^all" else int(dest),
            "to_id": "^all" if dest == "^all" else format_node_id(int(dest)),
            "channel": int(channel_index),
            "text": cleaned,
            "timestamp": now_ts,
            "rx_snr": None,
            "rx_rssi": None,
            "hops_away": 0,
        }
        with self._lock:
            self._messages.append(record)
            if admin_num:
                self._ensure_live_entry(admin_num)["last_message"] = record
        return record

    # ------------------------------------------------------------------
    # Direct USB Serial Node Inspection & 1-Click Admin Key Enrollment
    # ------------------------------------------------------------------
    def _match_pubkey_owner_locked(self, pubkey_b64: str) -> Optional[Dict[str, str]]:
        """Check if a base64 public key belongs to the paired Admin Node or any known mesh node."""
        if not pubkey_b64:
            return None
        admin_summary = self._build_admin_node_summary_locked()
        if admin_summary and admin_summary.get("public_key") == pubkey_b64:
            return {
                "node_id": admin_summary["node_id"],
                "name": admin_summary["long_name"],
                "is_admin_node": "true",
            }
        if self._iface and self._iface.nodesByNum:
            for num, nd in self._iface.nodesByNum.items():
                u = nd.get("user") or {}
                if u.get("publicKey") == pubkey_b64:
                    return {
                        "node_id": u.get("id") or format_node_id(int(num)),
                        "name": str(u.get("longName") or u.get("shortName") or format_node_id(int(num))),
                    }
        return None

    def list_usb_ports(self) -> List[Dict[str, Any]]:
        """List serial COM ports on this computer, highlighting candidate Meshtastic USB radios."""
        import meshtastic.util
        import serial.tools.list_ports as list_ports

        detected_mesh_ports = set()
        try:
            detected_mesh_ports = set(meshtastic.util.findPorts(True) or [])
        except Exception:
            pass

        known_usb_vids = {"10C4", "1A86", "303A", "239A", "2886", "0403", "2E8A", "1915"}
        ports_out: List[Dict[str, Any]] = []
        for p in list_ports.comports():
            hwid_upper = (p.hwid or "").upper()
            is_bt = "BTHENUM" in hwid_upper or "BLUETOOTH" in (p.description or "").upper()
            has_known_vid = any(f"VID:PID={vid}:" in hwid_upper or f"VID_{vid}" in hwid_upper for vid in known_usb_vids)
            is_candidate = (not is_bt) and (p.device in detected_mesh_ports or has_known_vid)
            ports_out.append(
                {
                    "device": p.device,
                    "description": p.description or p.device,
                    "hwid": p.hwid or "",
                    "is_bluetooth": is_bt,
                    "is_meshtastic_candidate": is_candidate,
                    "auto_detected": p.device in detected_mesh_ports,
                }
            )

        ports_out.sort(
            key=lambda item: (
                0 if item["auto_detected"] else (1 if item["is_meshtastic_candidate"] else 2),
                item["device"],
            )
        )
        return ports_out

    @staticmethod
    def _open_serial_interface(port: str):
        import meshtastic.serial_interface

        return meshtastic.serial_interface.SerialInterface(
            devPath=port,
            noNodes=True,
            timeout=15,
        )

    @staticmethod
    def _fetch_serial_session_passkey(s_iface: Any, node_num: int, timeout: float = 8.0) -> bytes:
        done = threading.Event()
        holder: Dict[str, Any] = {}

        def _on_resp(pkt: dict) -> None:
            holder["pkt"] = pkt
            done.set()

        meta = getattr(s_iface, "metadata", None)
        fw_ver = str(getattr(meta, "firmware_version", "") or "")
        use_pki = not fw_ver.startswith(("2.0.", "2.1.", "2.2.", "2.3.", "2.4.", "2.5."))

        req = admin_pb2.AdminMessage()
        req.get_config_request = admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG
        sent = s_iface.sendData(
            req,
            destinationId=node_num,
            portNum=portnums_pb2.PortNum.ADMIN_APP,
            wantAck=True,
            wantResponse=True,
            onResponse=_on_resp,
            channelIndex=0,
            pkiEncrypted=use_pki,
        )
        if done.wait(timeout=timeout):
            adm_raw = holder.get("pkt", {}).get("decoded", {}).get("admin", {}).get("raw")
            if adm_raw is not None and adm_raw.session_passkey:
                return bytes(adm_raw.session_passkey)
        else:
            s_iface.responseHandlers.pop(sent.id, None)

        if s_iface.nodesByNum and node_num in s_iface.nodesByNum:
            pk = s_iface.nodesByNum[node_num].get("adminSessionPassKey")
            if pk:
                return bytes(pk)
        return b""

    def inspect_usb_node(self, port: str) -> Dict[str, Any]:
        """Connect over USB serial to ``port`` and inspect its identity and 3 admin_key slots."""
        cleaned_port = (port or "").strip()
        if not cleaned_port:
            raise ValueError("Please select a USB COM port.")

        with self._usb_lock:
            s_iface = self._open_serial_interface(cleaned_port)
            try:
                if s_iface.myInfo is None:
                    raise RuntimeError(f"Could not read Meshtastic node info on {cleaned_port}.")
                node_num = int(s_iface.myInfo.my_node_num)
                node_id = format_node_id(node_num)
                u = s_iface.getMyUser() or {}
                meta = s_iface.metadata
                sec = s_iface.localNode.localConfig.security

                pub_b64 = base64.b64encode(bytes(sec.public_key)).decode("ascii") if sec.public_key else (u.get("publicKey") or "")
                existing_keys_b64 = [
                    base64.b64encode(bytes(k)).decode("ascii")
                    for k in sec.admin_key
                    if k
                ]

                with self._lock:
                    admin_summary = self._build_admin_node_summary_locked()
                    paired_admin_pk = admin_summary.get("public_key") if admin_summary else ""
                    paired_admin_id = admin_summary.get("node_id") if admin_summary else None
                    slots = []
                    for idx, kb64 in enumerate(existing_keys_b64):
                        owner = self._match_pubkey_owner_locked(kb64)
                        slots.append(
                            {
                                "slot": idx + 1,
                                "key": kb64,
                                "matches_paired_admin": bool(paired_admin_pk and kb64 == paired_admin_pk),
                                "matched_node": owner,
                            }
                        )

                return {
                    "port": cleaned_port,
                    "node_num": node_num,
                    "node_id": node_id,
                    "long_name": u.get("longName") or f"Meshtastic {node_id[-4:]}",
                    "short_name": u.get("shortName") or node_id[-4:],
                    "hw_model": u.get("hwModel") or (
                        mesh_pb2.HardwareModel.Name(meta.hw_model)
                        if meta and meta.hw_model in mesh_pb2.HardwareModel.values()
                        else "UNKNOWN"
                    ),
                    "firmware_version": getattr(meta, "firmware_version", None) if meta else None,
                    "public_key": pub_b64,
                    "is_paired_admin_node": bool(paired_admin_id and node_id == paired_admin_id),
                    "already_has_paired_admin_key": bool(paired_admin_pk and paired_admin_pk in existing_keys_b64),
                    "paired_admin_public_key": paired_admin_pk,
                    "paired_admin_node_id": paired_admin_id,
                    "slots_used": len(slots),
                    "slots_max": 3,
                    "admin_keys": slots,
                }
            finally:
                try:
                    s_iface.close()
                except Exception:
                    pass

    def enroll_usb_node(
        self,
        port: str,
        *,
        admin_public_key: Optional[str] = None,
        keep_keys: Optional[List[str]] = None,
        mode: str = "smart",
    ) -> Dict[str, Any]:
        """Write the paired Admin Node's PKC public key into the USB node's ``security.admin_key``
        using a proper ``session_passkey`` + ``begin_edit_settings`` / ``commit_edit_settings``
        transaction while respecting Meshtastic firmware's 3-key slot limit."""
        cleaned_port = (port or "").strip()
        if not cleaned_port:
            raise ValueError("Please select a USB COM port.")

        with self._lock:
            admin_summary = self._build_admin_node_summary_locked()
            target_pk_b64 = (admin_public_key or (admin_summary.get("public_key") if admin_summary else "") or "").strip()
            if target_pk_b64.startswith("base64:"):
                target_pk_b64 = target_pk_b64[len("base64:") :].strip()

        if not target_pk_b64:
            raise ValueError(
                "No Admin Node public key available. Pair to the Admin Node via HTTP first or paste its Base64 public key."
            )

        target_pk_bytes = base64.b64decode(target_pk_b64)
        if len(target_pk_bytes) != 32:
            raise ValueError(f"Admin public key must be 32 bytes (got {len(target_pk_bytes)} bytes).")
        canonical_target_b64 = base64.b64encode(target_pk_bytes).decode("ascii")

        with self._usb_lock:
            s_iface = self._open_serial_interface(cleaned_port)
            try:
                if s_iface.myInfo is None:
                    raise RuntimeError(f"Could not read Meshtastic node info on {cleaned_port}.")
                node_num = int(s_iface.myInfo.my_node_num)
                node_id = format_node_id(node_num)
                u = s_iface.getMyUser() or {}
                sec = s_iface.localNode.localConfig.security

                existing_b64 = [
                    base64.b64encode(bytes(k)).decode("ascii")
                    for k in sec.admin_key
                    if k and len(bytes(k)) == 32
                ]

                # Build the new <= 3 key list with canonical_target_b64 guaranteed included
                retained: List[str] = []
                if keep_keys is not None:
                    for kb64 in keep_keys:
                        clean_k = kb64.strip()
                        if clean_k and clean_k != canonical_target_b64 and clean_k not in retained:
                            raw_k = base64.b64decode(clean_k)
                            if len(raw_k) == 32:
                                retained.append(base64.b64encode(raw_k).decode("ascii"))
                elif mode == "replace_all":
                    retained = []
                else:
                    # Smart mode: keep existing keys that match known nodes in our mesh (up to 2), drop stale keys!
                    with self._lock:
                        for kb64 in existing_b64:
                            if kb64 == canonical_target_b64:
                                continue
                            if self._match_pubkey_owner_locked(kb64) is not None and kb64 not in retained:
                                retained.append(kb64)

                final_b64_list = [canonical_target_b64, *retained[:2]]
                final_bytes_list = [base64.b64decode(x) for x in final_b64_list]

                meta = getattr(s_iface, "metadata", None)
                fw_ver = str(getattr(meta, "firmware_version", "") or "")
                use_pki = not fw_ver.startswith(("2.0.", "2.1.", "2.2.", "2.3.", "2.4.", "2.5."))

                # Fetch fresh session_passkey on the USB link (fixes meshtastic 2.7.11 writeConfig bug!)
                passkey = self._fetch_serial_session_passkey(s_iface, node_num)

                new_sec = config_pb2.Config.SecurityConfig()
                new_sec.CopyFrom(sec)
                del new_sec.admin_key[:]
                for b_key in final_bytes_list:
                    new_sec.admin_key.append(b_key)

                for step_name, build_fn in (
                    ("begin", lambda m: setattr(m, "begin_edit_settings", True)),
                    ("set_security", lambda m: m.set_config.security.CopyFrom(new_sec)),
                    ("commit", lambda m: setattr(m, "commit_edit_settings", True)),
                ):
                    adm_msg = admin_pb2.AdminMessage()
                    if passkey:
                        adm_msg.session_passkey = passkey
                    build_fn(adm_msg)
                    s_iface.sendData(
                        adm_msg,
                        destinationId=node_num,
                        portNum=portnums_pb2.PortNum.ADMIN_APP,
                        wantAck=True,
                        wantResponse=False,
                        channelIndex=0,
                        pkiEncrypted=use_pki,
                    )
                    time.sleep(0.6)

                # Allow final UART packet to flush & commit to LittleFS/NVS before closing serial port
                time.sleep(1.6)

                node_pub_b64 = (
                    base64.b64encode(bytes(sec.public_key)).decode("ascii")
                    if sec.public_key
                    else (u.get("publicKey") or "")
                )
            finally:
                try:
                    s_iface.close()
                except Exception:
                    pass

        # Sync the USB node's publicKey & user info into our paired Admin Node session and mark Admin Power!
        with self._lock:
            if self._iface is not None and self._iface.nodesByNum is not None:
                nd = self._iface._getOrCreateByNum(node_num)
                nd_user = nd.setdefault("user", {})
                nd_user["id"] = node_id
                if u.get("longName"):
                    nd_user["longName"] = u["longName"]
                if u.get("shortName"):
                    nd_user["shortName"] = u["shortName"]
                if u.get("hwModel"):
                    nd_user["hwModel"] = u["hwModel"]
                if node_pub_b64:
                    nd_user["publicKey"] = node_pub_b64
                if self._iface.nodes is not None:
                    self._iface.nodes[node_id] = nd

        if self._get_admin_node_num() is not None:
            self._mark_admin_verified(node_num)

        return {
            "port": cleaned_port,
            "node_id": node_id,
            "long_name": u.get("longName") or node_id,
            "short_name": u.get("shortName") or node_id[-4:],
            "previous_keys": existing_b64,
            "saved_keys": final_b64_list,
            "had_session_passkey": bool(passkey),
        }
