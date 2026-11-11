"""End-to-end tests for Meshtastic Web Admin:
- Mock ESP32 HTTP /api/v1/toradio & /api/v1/fromradio protobuf server
- HTTP Admin Node pairing, signal/battery/last-message tracking, and relay-node resolution
- Admin Power verification (PKC + session_passkey) vs non-admin rejection
- Full parameter schema generation and remote Owner / Radio Config / Module Config / Channel / Traceroute operations
- Global Python -> .venv auto-reexec check
"""

from __future__ import annotations

import collections
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Deque, List
from unittest import mock

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2, mesh_pb2, module_config_pb2, portnums_pb2

from app import create_app
from mesh_manager import MeshAdminManager

ADMIN_NUM = 0x11111101
RELAY_NUM = 0x22222259  # Lowest byte 0x59 — grants remote admin!
FAR_NUM = 0x333333A4    # Relayed 1 hop via 0x59 — denies remote admin!

ADMIN_PUBKEY = b"\xaa" * 32
RELAY_PUBKEY = b"\xbb" * 32
FAR_PUBKEY = b"\xcc" * 32
REMOTE_PASSKEY = b"PASSKEY8"


class FakeMeshtasticRadioState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fromradio_queue: Deque[bytes] = collections.deque()
        self.received_toradio: List[mesh_pb2.ToRadio] = []
        self.received_admin_msgs: List[tuple[int, admin_pb2.AdminMessage, mesh_pb2.MeshPacket]] = []
        self.remote_lora_hop_limit = 3
        self.remote_long_name = "Squad 6 Relay"
        self.remote_short_name = "SQD6"

    def push_fromradio(self, fr: mesh_pb2.FromRadio) -> None:
        with self.lock:
            self.fromradio_queue.append(fr.SerializeToString())

    def pop_fromradio(self) -> bytes:
        with self.lock:
            if self.fromradio_queue:
                return self.fromradio_queue.popleft()
            return b""

    def handle_toradio(self, raw: bytes) -> None:
        tr = mesh_pb2.ToRadio()
        tr.ParseFromString(raw)
        with self.lock:
            self.received_toradio.append(tr)

        if tr.want_config_id:
            self._enqueue_initial_config(tr.want_config_id)
        elif tr.HasField("packet"):
            self._handle_mesh_packet(tr.packet)

    def _enqueue_initial_config(self, config_id: int) -> None:
        now = int(time.time())

        # 1. MyNodeInfo
        fr = mesh_pb2.FromRadio()
        fr.my_info.my_node_num = ADMIN_NUM
        self.push_fromradio(fr)

        # 2. Metadata
        fr = mesh_pb2.FromRadio()
        fr.metadata.firmware_version = "2.7.24.abcdef"
        fr.metadata.hw_model = mesh_pb2.HardwareModel.HELTEC_V3
        fr.metadata.hasPKC = True
        self.push_fromradio(fr)

        # 3. Local Config (device, lora, security) & ModuleConfig (mqtt)
        fr = mesh_pb2.FromRadio()
        fr.config.device.role = config_pb2.Config.DeviceConfig.Role.CLIENT
        self.push_fromradio(fr)

        fr = mesh_pb2.FromRadio()
        fr.config.lora.use_preset = True
        fr.config.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
        fr.config.lora.modem_preset = config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST
        fr.config.lora.hop_limit = 3
        self.push_fromradio(fr)

        fr = mesh_pb2.FromRadio()
        fr.config.security.public_key = ADMIN_PUBKEY
        self.push_fromradio(fr)

        fr = mesh_pb2.FromRadio()
        fr.moduleConfig.mqtt.address = "mqtt.example.local"
        fr.moduleConfig.mqtt.enabled = True
        self.push_fromradio(fr)

        # 4. Primary Channel 0
        fr = mesh_pb2.FromRadio()
        fr.channel.index = 0
        fr.channel.role = channel_pb2.Channel.Role.PRIMARY
        fr.channel.settings.psk = b"\x01"
        fr.channel.settings.name = ""
        self.push_fromradio(fr)

        # 5. NodeDB entries: Admin Node, Direct Relay Node, and Far Relayed Node
        fr = mesh_pb2.FromRadio()
        fr.node_info.num = ADMIN_NUM
        fr.node_info.user.id = f"!{ADMIN_NUM:08x}"
        fr.node_info.user.long_name = "HQ Admin Node"
        fr.node_info.user.short_name = "HQAD"
        fr.node_info.user.public_key = ADMIN_PUBKEY
        fr.node_info.device_metrics.battery_level = 101
        fr.node_info.device_metrics.voltage = 5.05
        fr.node_info.hops_away = 0
        fr.node_info.last_heard = now
        self.push_fromradio(fr)

        fr = mesh_pb2.FromRadio()
        fr.node_info.num = RELAY_NUM
        fr.node_info.user.id = f"!{RELAY_NUM:08x}"
        fr.node_info.user.long_name = self.remote_long_name
        fr.node_info.user.short_name = self.remote_short_name
        fr.node_info.user.public_key = RELAY_PUBKEY
        fr.node_info.snr = 9.5
        fr.node_info.hops_away = 0
        fr.node_info.last_heard = now - 15
        fr.node_info.device_metrics.battery_level = 84
        fr.node_info.device_metrics.voltage = 4.08
        self.push_fromradio(fr)

        fr = mesh_pb2.FromRadio()
        fr.node_info.num = FAR_NUM
        fr.node_info.user.id = f"!{FAR_NUM:08x}"
        fr.node_info.user.long_name = "Recon Outpost"
        fr.node_info.user.short_name = "RCON"
        fr.node_info.user.public_key = FAR_PUBKEY
        fr.node_info.hops_away = 1
        fr.node_info.last_heard = now - 30
        fr.node_info.device_metrics.battery_level = 47
        fr.node_info.device_metrics.voltage = 3.74
        self.push_fromradio(fr)

        # 6. Config complete
        fr = mesh_pb2.FromRadio()
        fr.config_complete_id = config_id
        self.push_fromradio(fr)

        # 7. Live relayed text packet from FAR_NUM relayed via RELAY_NUM (lowest byte 0x59)
        fr = mesh_pb2.FromRadio()
        setattr(fr.packet, "from", FAR_NUM)
        fr.packet.to = 0xFFFFFFFF
        fr.packet.id = 999111
        fr.packet.rx_time = now - 4
        fr.packet.rx_snr = 6.25
        fr.packet.rx_rssi = -94
        fr.packet.hop_start = 3
        fr.packet.hop_limit = 2  # 3 - 2 == 1 hop!
        fr.packet.relay_node = RELAY_NUM & 0xFF
        fr.packet.decoded.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        fr.packet.decoded.payload = b"All clear at checkpoint 2"
        self.push_fromradio(fr)

    def _handle_mesh_packet(self, mp: mesh_pb2.MeshPacket) -> None:
        dest = int(mp.to)
        if mp.decoded.portnum == portnums_pb2.PortNum.TRACEROUTE_APP:
            rd = mesh_pb2.RouteDiscovery()
            rd.route.append(RELAY_NUM)
            rd.snr_towards.extend([38, 22])  # +9.5 dB, +5.5 dB
            rd.route_back.append(RELAY_NUM)
            rd.snr_back.extend([24, 36])     # +6.0 dB, +9.0 dB

            fr = mesh_pb2.FromRadio()
            setattr(fr.packet, "from", dest)
            fr.packet.to = ADMIN_NUM
            fr.packet.id = 777001
            fr.packet.decoded.portnum = portnums_pb2.PortNum.TRACEROUTE_APP
            fr.packet.decoded.request_id = mp.id
            fr.packet.decoded.payload = rd.SerializeToString()
            self.push_fromradio(fr)
            return

        if mp.decoded.portnum != portnums_pb2.PortNum.ADMIN_APP:
            return

        adm = admin_pb2.AdminMessage()
        adm.ParseFromString(mp.decoded.payload)
        with self.lock:
            self.received_admin_msgs.append((dest, adm, mp))

        # FAR_NUM rejects remote admin with NAK
        if dest == FAR_NUM:
            routing = mesh_pb2.Routing()
            routing.error_reason = mesh_pb2.Routing.Error.PKI_FAILED
            fr = mesh_pb2.FromRadio()
            setattr(fr.packet, "from", FAR_NUM)
            fr.packet.to = ADMIN_NUM
            fr.packet.id = 880001
            fr.packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
            fr.packet.decoded.request_id = mp.id
            fr.packet.decoded.payload = routing.SerializeToString()
            self.push_fromradio(fr)
            return

        # RELAY_NUM (remote controllable) & ADMIN_NUM (local)
        variant = adm.WhichOneof("payload_variant") or ""
        if variant == "get_owner_request":
            resp = admin_pb2.AdminMessage()
            resp.session_passkey = REMOTE_PASSKEY
            resp.get_owner_response.long_name = self.remote_long_name if dest == RELAY_NUM else "HQ Admin Node"
            resp.get_owner_response.short_name = self.remote_short_name if dest == RELAY_NUM else "HQAD"
            self._send_admin_reply(dest, mp.id, resp)

        elif variant == "get_config_request":
            resp = admin_pb2.AdminMessage()
            resp.session_passkey = REMOTE_PASSKEY
            if adm.get_config_request == admin_pb2.AdminMessage.ConfigType.SESSIONKEY_CONFIG:
                resp.get_config_response.sessionkey.SetInParent()
            elif adm.get_config_request == admin_pb2.AdminMessage.ConfigType.LORA_CONFIG:
                resp.get_config_response.lora.use_preset = True
                resp.get_config_response.lora.region = config_pb2.Config.LoRaConfig.RegionCode.US
                resp.get_config_response.lora.hop_limit = self.remote_lora_hop_limit
            else:
                resp.get_config_response.device.role = config_pb2.Config.DeviceConfig.Role.ROUTER
            self._send_admin_reply(dest, mp.id, resp)

        elif variant == "get_module_config_request":
            resp = admin_pb2.AdminMessage()
            resp.session_passkey = REMOTE_PASSKEY
            if adm.get_module_config_request == admin_pb2.AdminMessage.ModuleConfigType.MQTT_CONFIG:
                resp.get_module_config_response.mqtt.address = "mqtt.example.local"
                resp.get_module_config_response.mqtt.enabled = True
            else:
                resp.get_module_config_response.telemetry.device_update_interval = 900
            self._send_admin_reply(dest, mp.id, resp)

        elif variant == "get_channel_request":
            idx = max(0, int(adm.get_channel_request) - 1)
            resp = admin_pb2.AdminMessage()
            resp.session_passkey = REMOTE_PASSKEY
            resp.get_channel_response.index = idx
            resp.get_channel_response.role = (
                channel_pb2.Channel.Role.PRIMARY if idx == 0 else channel_pb2.Channel.Role.DISABLED
            )
            resp.get_channel_response.settings.psk = b"\x01" if idx == 0 else b""
            self._send_admin_reply(dest, mp.id, resp)

        else:
            # State-changing write: enforce session_passkey on remote RELAY_NUM!
            if dest == RELAY_NUM and adm.session_passkey != REMOTE_PASSKEY:
                routing = mesh_pb2.Routing()
                routing.error_reason = mesh_pb2.Routing.Error.UNAUTHORIZED
                fr = mesh_pb2.FromRadio()
                setattr(fr.packet, "from", dest)
                fr.packet.to = ADMIN_NUM
                fr.packet.id = 880099
                fr.packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
                fr.packet.decoded.request_id = mp.id
                fr.packet.decoded.payload = routing.SerializeToString()
                self.push_fromradio(fr)
                return

            if variant == "set_config" and adm.set_config.HasField("lora"):
                self.remote_lora_hop_limit = int(adm.set_config.lora.hop_limit)
            elif variant == "set_owner":
                self.remote_long_name = adm.set_owner.long_name
                self.remote_short_name = adm.set_owner.short_name

            if dest != ADMIN_NUM:
                routing = mesh_pb2.Routing()
                routing.error_reason = mesh_pb2.Routing.Error.NONE
                fr = mesh_pb2.FromRadio()
                setattr(fr.packet, "from", dest)
                fr.packet.to = ADMIN_NUM
                fr.packet.id = 880055
                fr.packet.decoded.portnum = portnums_pb2.PortNum.ROUTING_APP
                fr.packet.decoded.request_id = mp.id
                fr.packet.decoded.payload = routing.SerializeToString()
                self.push_fromradio(fr)

    def _send_admin_reply(self, sender_num: int, req_id: int, adm_resp: admin_pb2.AdminMessage) -> None:
        fr = mesh_pb2.FromRadio()
        setattr(fr.packet, "from", sender_num)
        fr.packet.to = ADMIN_NUM
        fr.packet.id = int(time.time() * 1000) & 0xFFFFFFFF
        fr.packet.pki_encrypted = True
        fr.packet.decoded.portnum = portnums_pb2.PortNum.ADMIN_APP
        fr.packet.decoded.request_id = req_id
        fr.packet.decoded.payload = adm_resp.SerializeToString()
        self.push_fromradio(fr)


import threading


class _FakeHTTPResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeRequestsSession:
    def __init__(self, radio: FakeMeshtasticRadioState) -> None:
        self._radio = radio

    def get(self, url: str, **kwargs):
        if "/api/v1/fromradio" in url:
            return _FakeHTTPResponse(self._radio.pop_fromradio(), 200)
        return _FakeHTTPResponse(b"", 404)

    def put(self, url: str, data: bytes = b"", **kwargs):
        if "/api/v1/toradio" in url:
            self._radio.handle_toradio(bytes(data))
            return _FakeHTTPResponse(b"", 200)
        return _FakeHTTPResponse(b"", 404)

    def close(self) -> None:
        return


class MeshtasticWebAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.radio = FakeMeshtasticRadioState()
        self.session_patcher = mock.patch(
            "meshtastic_http.requests.Session",
            side_effect=lambda: _FakeRequestsSession(self.radio),
        )
        self.session_patcher.start()

        self.tmpdir = tempfile.TemporaryDirectory()
        cache_file = pathlib.Path(self.tmpdir.name) / "admin_cache.json"
        self.manager = MeshAdminManager(cache_path=cache_file, auto_probe_admin=False)
        self.app = create_app(manager=self.manager)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.session_patcher.stop()
        self.tmpdir.cleanup()

    def test_full_http_pairing_relay_tracking_admin_icon_and_remote_config(self) -> None:
        # 1. Index HTML loads cleanly
        idx = self.client.get("/")
        self.assertEqual(idx.status_code, 200)
        self.assertIn(b"Meshtastic Web Admin", idx.data)

        # 2. Schema exposes Owner, all Radio Config sections, all Module Config sections, and Channels
        sc_resp = self.client.get("/api/schema")
        self.assertEqual(sc_resp.status_code, 200)
        schema = sc_resp.get_json()["schema"]
        cfg_keys = {s["key"] for s in schema["config"]}
        mod_keys = {s["key"] for s in schema["module"]}
        self.assertTrue({"device", "position", "power", "network", "display", "lora", "bluetooth", "security"}.issubset(cfg_keys))
        self.assertTrue({"mqtt", "telemetry", "neighbor_info", "serial", "tak"}.issubset(mod_keys))

        # 3. Pair via HTTP to default http://meshtastic.local Admin Node
        pair_resp = self.client.post("/api/connect", json={"target": "http://meshtastic.local"})
        self.assertEqual(pair_resp.status_code, 200)
        # Allow queued live relayed packet to drain
        time.sleep(0.25)

        state = self.client.get("/api/state").get_json()
        self.assertTrue(state["connection"]["connected"])
        self.assertEqual(state["admin_node"]["node_id"], f"!{ADMIN_NUM:08x}")
        self.assertEqual(state["counts"]["total"], 3)
        self.assertEqual(state["counts"]["direct"], 1)
        self.assertEqual(state["counts"]["relayed"], 1)

        nodes_by_id = {n["node_id"]: n for n in state["nodes"]}
        admin_node = nodes_by_id[f"!{ADMIN_NUM:08x}"]
        relay_node = nodes_by_id[f"!{RELAY_NUM:08x}"]
        far_node = nodes_by_id[f"!{FAR_NUM:08x}"]

        # Verify signal, battery, last message, and relay resolution!
        self.assertTrue(admin_node["has_admin"])
        self.assertEqual(far_node["signal"]["snr"], 6.25)
        self.assertEqual(far_node["signal"]["rssi"], -94)
        self.assertEqual(far_node["battery"]["level"], 47)
        self.assertEqual(far_node["last_message"]["text"], "All clear at checkpoint 2")
        self.assertEqual(far_node["relay"]["link_type"], "relayed")
        self.assertEqual(far_node["relay"]["hops_away"], 1)
        self.assertEqual(far_node["relay"]["via_node"]["node_id"], f"!{RELAY_NUM:08x}")
        self.assertEqual(far_node["relay"]["via_node"]["name"], "Squad 6 Relay")
        self.assertEqual(relay_node["relay"]["relaying_for"][0]["node_id"], f"!{FAR_NUM:08x}")

        # 4. Probe Admin Power: RELAY_NUM succeeds (has_admin=True), FAR_NUM fails (has_admin=False)
        p_ok = self.client.post(f"/api/node/!{RELAY_NUM:08x}/action", json={"action": "probe_admin"}).get_json()
        self.assertTrue(p_ok["has_admin"])

        p_deny = self.client.post(f"/api/node/!{FAR_NUM:08x}/action", json={"action": "probe_admin"}).get_json()
        self.assertFalse(p_deny["has_admin"])

        state2 = self.client.get("/api/state").get_json()
        nodes2 = {n["node_id"]: n for n in state2["nodes"]}
        self.assertTrue(nodes2[f"!{RELAY_NUM:08x}"]["has_admin"])
        self.assertFalse(nodes2[f"!{FAR_NUM:08x}"]["has_admin"])

        # 5. Read & Modify Remote LoRa Config on RELAY_NUM (verifying PKC + session_passkey transaction)
        lora_get = self.client.get(f"/api/node/!{RELAY_NUM:08x}/config/config/lora").get_json()
        self.assertTrue(lora_get["ok"])
        self.assertEqual(lora_get["values"]["hop_limit"], 3)

        lora_vals = dict(lora_get["values"])
        lora_vals["hop_limit"] = 5
        lora_save = self.client.post(
            f"/api/node/!{RELAY_NUM:08x}/config/config/lora",
            json={"values": lora_vals},
        )
        self.assertEqual(lora_save.status_code, 200)
        self.assertEqual(self.radio.remote_lora_hop_limit, 5)

        # Verify begin_edit_settings -> set_config -> commit_edit_settings carried session_passkey & pki_encrypted
        relay_writes = [
            (adm, mp)
            for dest, adm, mp in self.radio.received_admin_msgs
            if dest == RELAY_NUM and adm.WhichOneof("payload_variant") in ("begin_edit_settings", "set_config", "commit_edit_settings")
        ]
        self.assertEqual(len(relay_writes), 3)
        for adm, mp in relay_writes:
            self.assertEqual(adm.session_passkey, REMOTE_PASSKEY)
            self.assertTrue(mp.pki_encrypted)

        # 6. Modify Remote Owner Name & Channel 1 on RELAY_NUM
        own_save = self.client.post(
            f"/api/node/!{RELAY_NUM:08x}/owner",
            json={"values": {"long_name": "Squad 6 Alpha", "short_name": "SQ6A", "is_licensed": False, "is_unmessagable": False}},
        )
        self.assertEqual(own_save.status_code, 200)
        self.assertEqual(self.radio.remote_long_name, "Squad 6 Alpha")

        ch_save = self.client.post(
            f"/api/node/!{RELAY_NUM:08x}/channel/1",
            json={"channel": {"role": 2, "psk_text": "random256", "settings": {"name": "TacOps", "uplink_enabled": False, "downlink_enabled": False}}},
        )
        self.assertEqual(ch_save.status_code, 200)
        self.assertEqual(ch_save.get_json()["channel"]["role_name"], "SECONDARY")

        # 7. Run Traceroute to FAR_NUM
        tr_resp = self.client.post(
            f"/api/node/!{FAR_NUM:08x}/action",
            json={"action": "traceroute"},
        ).get_json()
        self.assertTrue(tr_resp["ok"])
        self.assertEqual(tr_resp["traceroute"]["relay_hops"], 1)
        self.assertEqual(tr_resp["traceroute"]["forward"][1]["node_id"], f"!{RELAY_NUM:08x}")

    def test_usb_inspect_and_enroll_with_3_full_slots(self) -> None:
        import base64
        from meshtastic.protobuf import localonly_pb2

        self.client.post("/api/connect", json={"target": "http://meshtastic.local"})

        stale_key_1 = b"\xd1" * 32
        stale_key_2 = b"\xd2" * 32
        sent_usb_admin: List[admin_pb2.AdminMessage] = []

        class FakeUSBSerialInterface:
            def __init__(self) -> None:
                self.myInfo = mesh_pb2.MyNodeInfo(my_node_num=FAR_NUM)
                self.metadata = mesh_pb2.DeviceMetadata(
                    firmware_version="2.7.24",
                    hw_model=mesh_pb2.HardwareModel.RAK4631,
                    hasPKC=True,
                )
                self.localNode = mock.Mock()
                self.localNode.localConfig = localonly_pb2.LocalConfig()
                self.localNode.localConfig.security.public_key = FAR_PUBKEY
                # 3/3 full slots: 1 known mesh peer (RELAY_PUBKEY) + 2 stale keys!
                self.localNode.localConfig.security.admin_key.extend([RELAY_PUBKEY, stale_key_1, stale_key_2])
                self.nodesByNum = {FAR_NUM: {}}
                self.responseHandlers = {}

            def getMyUser(self):
                return {
                    "id": f"!{FAR_NUM:08x}",
                    "longName": "squad 7",
                    "shortName": "7",
                    "hwModel": "RAK4631",
                    "publicKey": base64.b64encode(FAR_PUBKEY).decode("ascii"),
                }

            def sendData(self, data, onResponse=None, **kwargs):
                adm = admin_pb2.AdminMessage()
                adm.ParseFromString(data.SerializeToString() if hasattr(data, "SerializeToString") else data)
                sent_usb_admin.append(adm)
                if adm.WhichOneof("payload_variant") == "get_config_request" and onResponse:
                    resp = admin_pb2.AdminMessage(session_passkey=b"USBPASS8")
                    resp.get_config_response.sessionkey.SetInParent()
                    onResponse({"decoded": {"admin": {"raw": resp}}})
                pkt = mesh_pb2.MeshPacket(id=1234)
                return pkt

            def close(self):
                return

        with mock.patch.object(MeshAdminManager, "_open_serial_interface", side_effect=lambda port: FakeUSBSerialInterface()):
            insp = self.client.post("/api/usb/inspect", json={"port": "COM12"}).get_json()
            self.assertTrue(insp["ok"])
            usb_info = insp["usb_node"]
            self.assertEqual(usb_info["slots_used"], 3)
            self.assertFalse(usb_info["already_has_paired_admin_key"])
            # Slot 1 matches RELAY_NUM, Slots 2 & 3 are unknown/stale
            self.assertIsNotNone(usb_info["admin_keys"][0]["matched_node"])
            self.assertIsNone(usb_info["admin_keys"][1]["matched_node"])
            self.assertIsNone(usb_info["admin_keys"][2]["matched_node"])

            enr = self.client.post("/api/usb/enroll", json={"port": "COM12"}).get_json()
            self.assertTrue(enr["ok"])
            saved = enr["enrolled"]["saved_keys"]
            # Must keep RELAY_PUBKEY + add ADMIN_PUBKEY = 2 keys (<= 3 limit!), dropping the 2 stale keys!
            self.assertEqual(
                saved,
                [
                    base64.b64encode(ADMIN_PUBKEY).decode("ascii"),
                    base64.b64encode(RELAY_PUBKEY).decode("ascii"),
                ],
            )
            # Verify session_passkey was attached to begin_edit_settings, set_config, and commit_edit_settings!
            writes = [m for m in sent_usb_admin if m.WhichOneof("payload_variant") != "get_config_request"]
            self.assertEqual(len(writes), 3)
            for m in writes:
                self.assertEqual(m.session_passkey, b"USBPASS8")

            # FAR_NUM is now marked has_admin=True in dashboard state!
            st = self.client.get("/api/state").get_json()
            far_state = next(n for n in st["nodes"] if n["node_id"] == f"!{FAR_NUM:08x}")
            self.assertTrue(far_state["has_admin"])

    def test_batch_shutdown_and_multi_section_force_push(self) -> None:
        self.client.post("/api/connect", json={"target": "http://meshtastic.local"})
        admin_id = f"!{ADMIN_NUM:08x}"
        relay_id = f"!{RELAY_NUM:08x}"
        far_id = f"!{FAR_NUM:08x}"

        # 1. Batch Shutdown across [admin_id, relay_id, far_id]:
        #    Must execute remote peers (relay_id, far_id) BEFORE paired local admin_id!
        self.radio.received_admin_msgs.clear()
        batch_act = self.client.post(
            "/api/batch",
            json={"node_ids": [admin_id, relay_id, far_id], "action": "shutdown"},
        ).get_json()
        self.assertTrue(batch_act["ok"])
        self.assertEqual(batch_act["total"], 3)
        self.assertEqual(batch_act["succeeded"], 2)
        self.assertEqual(batch_act["failed"], 1)
        result_order = [r["node_id"] for r in batch_act["results"]]
        self.assertEqual(result_order, [relay_id, far_id, admin_id])

        seen_dests: List[int] = []
        for dest, _, _ in self.radio.received_admin_msgs:
            if dest not in seen_dests:
                seen_dests.append(dest)
        # Remote peers were contacted BEFORE ADMIN_NUM, and RELAY_NUM received shutdown_seconds before ADMIN_NUM!
        self.assertEqual(seen_dests, [RELAY_NUM, FAR_NUM, ADMIN_NUM])
        shutdown_dests = [
            dest
            for dest, adm, _ in self.radio.received_admin_msgs
            if adm.WhichOneof("payload_variant") == "shutdown_seconds"
        ]
        self.assertEqual(shutdown_dests, [RELAY_NUM, ADMIN_NUM])

        # 2. Multi-section batch parameter force push (lora.hop_limit + mqtt.enabled + channel 2 shared random256 key)
        #    in a single begin_edit_settings -> ... -> commit_edit_settings transaction per node!
        self.radio.received_admin_msgs.clear()
        changes = [
            {"kind": "config", "section": "lora", "values": {"hop_limit": 6}},
            {"kind": "module", "section": "mqtt", "values": {"enabled": False}},
            {
                "kind": "channel",
                "index": 2,
                "values": {"role": 2, "psk_text": "random256", "settings": {"name": "OpsBatch"}},
            },
        ]
        batch_cfg = self.client.post(
            "/api/batch",
            json={"node_ids": [admin_id, relay_id], "changes": changes, "blind_overwrite": False},
        ).get_json()
        self.assertTrue(batch_cfg["ok"])
        self.assertEqual(batch_cfg["succeeded"], 2)

        # Verify RELAY_NUM executed inside ONE edit transaction while preserving lora.region == US & mqtt.address!
        relay_msgs = [adm for dest, adm, _ in self.radio.received_admin_msgs if dest == RELAY_NUM]
        relay_variants = [m.WhichOneof("payload_variant") for m in relay_msgs]
        self.assertEqual(relay_variants.count("begin_edit_settings"), 1)
        self.assertEqual(relay_variants.count("commit_edit_settings"), 1)

        relay_set_lora = next(
            m.set_config.lora for m in relay_msgs if m.WhichOneof("payload_variant") == "set_config"
        )
        self.assertEqual(relay_set_lora.hop_limit, 6)
        self.assertEqual(relay_set_lora.region, config_pb2.Config.LoRaConfig.RegionCode.US)

        relay_set_mqtt = next(
            m.set_module_config.mqtt
            for m in relay_msgs
            if m.WhichOneof("payload_variant") == "set_module_config"
        )
        self.assertFalse(relay_set_mqtt.enabled)
        self.assertEqual(relay_set_mqtt.address, "mqtt.example.local")

        # Verify both RELAY_NUM and ADMIN_NUM received the EXACT SAME 32-byte random256 PSK on Channel 2!
        ch2_psks = [
            bytes(m.set_channel.settings.psk)
            for _, m, _ in self.radio.received_admin_msgs
            if m.WhichOneof("payload_variant") == "set_channel" and m.set_channel.index == 2
        ]
        self.assertEqual(len(ch2_psks), 2)
        self.assertEqual(len(ch2_psks[0]), 32)
        self.assertEqual(ch2_psks[0], ch2_psks[1])

        # 3. Blind overwrite mode skips reading existing section fields and writes raw values directly
        self.radio.received_admin_msgs.clear()
        blind_res = self.client.post(
            f"/api/node/{relay_id}/batch",
            json={
                "changes": [{"kind": "config", "section": "lora", "values": {"hop_limit": 4}}],
                "blind_overwrite": True,
            },
        ).get_json()
        self.assertTrue(blind_res["ok"])
        blind_msgs = [adm for dest, adm, _ in self.radio.received_admin_msgs if dest == RELAY_NUM]
        # No get_config_request needed (session_passkey already fresh, blind_overwrite=True)!
        self.assertNotIn("get_config_request", [m.WhichOneof("payload_variant") for m in blind_msgs])
        blind_lora = next(m.set_config.lora for m in blind_msgs if m.WhichOneof("payload_variant") == "set_config")
        self.assertEqual(blind_lora.hop_limit, 4)
        self.assertEqual(blind_lora.region, config_pb2.Config.LoRaConfig.RegionCode.UNSET)

        # 4. Retry loop on transient timeout: RELAY_NUM fails twice with TimeoutError then succeeds on attempt #3,
        #    while ADMIN_NUM waits until RELAY_NUM succeeds!
        call_counts = {relay_id: 0, admin_id: 0}
        orig_apply = self.manager.apply_batch_to_node

        def _flaky_apply(nid: str, **kwargs):
            call_counts[nid] = call_counts.get(nid, 0) + 1
            if nid == relay_id and call_counts[nid] <= 2:
                raise TimeoutError(f"Timed out after 12s waiting for response from {nid}")
            return orig_apply(nid, **kwargs)

        with mock.patch.object(self.manager, "apply_batch_to_node", side_effect=_flaky_apply):
            retry_res = self.client.post(
                "/api/batch",
                json={
                    "node_ids": [admin_id, relay_id],
                    "action": "reboot",
                    "max_attempts": 4,
                    "retry_delay": 0.01,
                },
            ).get_json()
        self.assertTrue(retry_res["ok"])
        self.assertEqual(retry_res["succeeded"], 2)
        self.assertEqual(retry_res["failed"], 0)
        by_nid = {r["node_id"]: r for r in retry_res["results"]}
        self.assertEqual(by_nid[relay_id]["attempts"], 3)
        self.assertEqual(by_nid[admin_id]["attempts"], 1)

    def test_global_python_auto_reexec_into_venv(self) -> None:
        proc = subprocess.run(
            ["python", str(ROOT / "app.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Meshtastic Web Admin Dashboard", proc.stdout)


if __name__ == "__main__":
    unittest.main()
