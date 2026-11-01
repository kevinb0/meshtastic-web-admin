"""Dynamic Protobuf schema introspection & serialization for Meshtastic 2.7+ node configuration.

Covers:
  - Identity / Owner (``User``: long_name, short_name, is_licensed, is_unmessagable)
  - All Radio Config sections (``Config``: device, position, power, network, display, lora,
    bluetooth, security, device_ui)
  - All Module Config sections (``ModuleConfig``: mqtt, serial, external_notification,
    store_forward, range_test, telemetry, canned_message, audio, remote_hardware,
    neighbor_info, ambient_lighting, detection_sensor, paxcounter, statusmessage,
    traffic_management, tak)
  - All 8 Channels (``Channel`` 0..7: role, settings, PSK helpers, module_settings)
"""

from __future__ import annotations

import base64
import secrets
import socket
import struct
from typing import Any, Dict, List, Optional

from google.protobuf.descriptor import Descriptor, FieldDescriptor
from google.protobuf.message import Message
from meshtastic.protobuf import admin_pb2, channel_pb2, config_pb2, module_config_pb2
from meshtastic.util import fromPSK, pskToString

# Explicit mapping from Config field name -> AdminMessage.ConfigType enum int
CONFIG_SECTION_ENUM: Dict[str, int] = {
    "device": admin_pb2.AdminMessage.ConfigType.DEVICE_CONFIG,
    "position": admin_pb2.AdminMessage.ConfigType.POSITION_CONFIG,
    "power": admin_pb2.AdminMessage.ConfigType.POWER_CONFIG,
    "network": admin_pb2.AdminMessage.ConfigType.NETWORK_CONFIG,
    "display": admin_pb2.AdminMessage.ConfigType.DISPLAY_CONFIG,
    "lora": admin_pb2.AdminMessage.ConfigType.LORA_CONFIG,
    "bluetooth": admin_pb2.AdminMessage.ConfigType.BLUETOOTH_CONFIG,
    "security": admin_pb2.AdminMessage.ConfigType.SECURITY_CONFIG,
}
if hasattr(admin_pb2.AdminMessage.ConfigType, "DEVICEUI_CONFIG"):
    CONFIG_SECTION_ENUM["device_ui"] = admin_pb2.AdminMessage.ConfigType.DEVICEUI_CONFIG

# Explicit mapping from ModuleConfig field name -> AdminMessage.ModuleConfigType enum int
MODULE_SECTION_ENUM: Dict[str, int] = {
    "mqtt": admin_pb2.AdminMessage.ModuleConfigType.MQTT_CONFIG,
    "serial": admin_pb2.AdminMessage.ModuleConfigType.SERIAL_CONFIG,
    "external_notification": admin_pb2.AdminMessage.ModuleConfigType.EXTNOTIF_CONFIG,
    "store_forward": admin_pb2.AdminMessage.ModuleConfigType.STOREFORWARD_CONFIG,
    "range_test": admin_pb2.AdminMessage.ModuleConfigType.RANGETEST_CONFIG,
    "telemetry": admin_pb2.AdminMessage.ModuleConfigType.TELEMETRY_CONFIG,
    "canned_message": admin_pb2.AdminMessage.ModuleConfigType.CANNEDMSG_CONFIG,
    "audio": admin_pb2.AdminMessage.ModuleConfigType.AUDIO_CONFIG,
    "remote_hardware": admin_pb2.AdminMessage.ModuleConfigType.REMOTEHARDWARE_CONFIG,
    "neighbor_info": admin_pb2.AdminMessage.ModuleConfigType.NEIGHBORINFO_CONFIG,
    "ambient_lighting": admin_pb2.AdminMessage.ModuleConfigType.AMBIENTLIGHTING_CONFIG,
    "detection_sensor": admin_pb2.AdminMessage.ModuleConfigType.DETECTIONSENSOR_CONFIG,
    "paxcounter": admin_pb2.AdminMessage.ModuleConfigType.PAXCOUNTER_CONFIG,
}
for _opt_mod, _opt_enum in (
    ("statusmessage", "STATUSMESSAGE_CONFIG"),
    ("traffic_management", "TRAFFICMANAGEMENT_CONFIG"),
    ("tak", "TAK_CONFIG"),
):
    if (
        _opt_mod in module_config_pb2.ModuleConfig.DESCRIPTOR.fields_by_name
        and hasattr(admin_pb2.AdminMessage.ModuleConfigType, _opt_enum)
    ):
        MODULE_SECTION_ENUM[_opt_mod] = getattr(admin_pb2.AdminMessage.ModuleConfigType, _opt_enum)

SECTION_METADATA: Dict[str, Dict[str, str]] = {
    # Radio Config
    "device": {
        "title": "Device",
        "description": "Node role, rebroadcast behavior, NodeInfo interval, buzzer/button GPIOs, and timezone.",
    },
    "position": {
        "title": "Position & GPS",
        "description": "GPS mode, smart position broadcast intervals, position packet flags, and fixed coordinates.",
    },
    "power": {
        "title": "Power",
        "description": "Power saving sleep timers, battery shutdown thresholds, and INA/ADC calibration.",
    },
    "network": {
        "title": "Network (Wi-Fi / Ethernet)",
        "description": "Wi-Fi SSID/PSK, Ethernet toggle, NTP server, DHCP/Static IPv4, and rsyslog.",
        "warning": "Changing Wi-Fi or IP settings on the paired Admin Node may change its HTTP address.",
    },
    "display": {
        "title": "Display",
        "description": "OLED type, screen timeout, metric/imperial units, orientation, and clock format.",
    },
    "lora": {
        "title": "LoRa Radio",
        "description": "Region, modem preset, hop limit, TX power, frequency slot/offset, and MQTT flags.",
        "warning": "Changing region, preset, or frequency slot on a remote node without matching the Admin Node can make it unreachable over RF.",
    },
    "bluetooth": {
        "title": "Bluetooth",
        "description": "BLE enable state, pairing mode, and fixed PIN.",
    },
    "security": {
        "title": "Security & Remote Admin Keys",
        "description": "PKC public/private keys, authorized Remote Admin public keys (up to 3), managed mode, and legacy admin channel.",
    },
    "device_ui": {
        "title": "Device UI",
        "description": "On-device screen UI preferences, brightness, theme, and node filters.",
    },
    # Module Config
    "mqtt": {
        "title": "MQTT Module",
        "description": "MQTT broker address, credentials, encryption/JSON/TLS toggles, root topic, and map reporting.",
        "warning": "Take care before modifying MQTT settings on active gateway nodes so TAK/OTS broker traffic is not disrupted.",
    },
    "telemetry": {
        "title": "Telemetry Module",
        "description": "Device, environment, air quality, and power metrics broadcast intervals.",
    },
    "neighbor_info": {
        "title": "Neighbor Info Module",
        "description": "Periodic neighbor link broadcast interval and LoRa/MQTT neighbor forwarding.",
    },
    "tak": {
        "title": "TAK / ATAK Module",
        "description": "ATAK team color and squad role metadata broadcast by TAK-enabled firmware.",
    },
    "serial": {
        "title": "Serial Module",
        "description": "External UART serial bridge pins, baud rate, timeout, and protocol mode.",
    },
    "external_notification": {
        "title": "External Notification",
        "description": "LED/buzzer/vibra alert GPIOs, durations, bell behavior, and RTTTL ringtone.",
    },
    "store_forward": {
        "title": "Store & Forward",
        "description": "Message history caching and replay server settings (PSRAM-equipped ESP32 nodes).",
    },
    "range_test": {
        "title": "Range Test",
        "description": "Automated periodic range-test packet sender and CSV storage.",
    },
    "canned_message": {
        "title": "Canned Messages",
        "description": "Rotary encoder / button input configuration and preloaded quick-send messages.",
    },
    "audio": {
        "title": "Audio (Codec2)",
        "description": "Codec2 voice-over-LoRa PTT and I2S audio pin configuration.",
    },
    "remote_hardware": {
        "title": "Remote Hardware",
        "description": "Remote GPIO pin read/write access and exposed pin definitions.",
    },
    "ambient_lighting": {
        "title": "Ambient Lighting",
        "description": "RGB LED state, current, and color levels.",
    },
    "detection_sensor": {
        "title": "Detection Sensor",
        "description": "GPIO trigger sensor monitoring, alert names, and broadcast cadence.",
    },
    "paxcounter": {
        "title": "Paxcounter",
        "description": "Wi-Fi/BLE crowd-density beacon counting interval and RSSI thresholds.",
    },
    "statusmessage": {
        "title": "Status Message",
        "description": "Persistent node status text broadcast.",
    },
    "traffic_management": {
        "title": "Traffic Management",
        "description": "Mesh congestion, rate limiting, deduplication, and router hop conservation controls.",
    },
}

PASSWORD_FIELDS = {"wifi_psk", "psk"}
READONLY_SECURITY_FIELDS = {"public_key"}
IPV4_SUBMESSAGE_NAMES = {"IpV4Config"}

INTEGER_TYPES = {
    FieldDescriptor.TYPE_INT32,
    FieldDescriptor.TYPE_SINT32,
    FieldDescriptor.TYPE_SFIXED32,
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_SFIXED64,
    FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_FIXED32,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_FIXED64,
}
UNSIGNED_TYPES = {
    FieldDescriptor.TYPE_UINT32,
    FieldDescriptor.TYPE_FIXED32,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_FIXED64,
}
FLOAT_TYPES = {
    FieldDescriptor.TYPE_FLOAT,
    FieldDescriptor.TYPE_DOUBLE,
}


def humanize_field_name(snake: str) -> str:
    """Turn protobuf snake_case field names into clean UI labels."""
    replacements = {
        "gpio": "GPIO",
        "gps": "GPS",
        "mqtt": "MQTT",
        "psk": "PSK",
        "ssid": "SSID",
        "ntp": "NTP",
        "ble": "BLE",
        "tx": "TX",
        "rx": "RX",
        "led": "LED",
        "oled": "OLED",
        "tls": "TLS",
        "json": "JSON",
        "ip": "IP",
        "ipv4": "IPv4",
        "dns": "DNS",
        "id": "ID",
        "ui": "UI",
        "adc": "ADC",
        "ina": "INA",
        "sds": "Super Deep Sleep",
        "ls": "Light Sleep",
        "tzdef": "POSIX Timezone (tzdef)",
        "lna": "LNA",
        "fem": "FEM",
        "pa": "PA",
        "atak": "ATAK",
        "tak": "TAK",
        "cw": "CW",
        "ptt": "PTT",
        "i2s": "I2S",
        "ws": "WS",
        "sd": "SD",
        "sck": "SCK",
        "mosi": "MOSI",
        "miso": "MISO",
        "rssi": "RSSI",
        "snr": "SNR",
    }
    if snake in replacements:
        return replacements[snake]
    parts = snake.split("_")
    out: List[str] = []
    for part in parts:
        if part in replacements:
            out.append(replacements[part])
        elif part == "secs":
            out.append("(sec)")
        else:
            out.append(part.capitalize())
    return " ".join(out)


def ipv4_int_to_str(val: int) -> str:
    """Convert Meshtastic ESP32 little-endian fixed32 IPv4 integer to dotted quad."""
    if not val:
        return "0.0.0.0"
    try:
        return socket.inet_ntoa(struct.pack("<I", int(val) & 0xFFFFFFFF))
    except Exception:
        return "0.0.0.0"


def ipv4_str_to_int(val: Any) -> int:
    """Convert dotted-quad IPv4 string (or raw int) into little-endian fixed32 integer."""
    if isinstance(val, int):
        return val & 0xFFFFFFFF
    text = str(val or "").strip()
    if not text or text == "0.0.0.0":
        return 0
    if text.isdigit():
        return int(text) & 0xFFFFFFFF
    packed = socket.inet_aton(text)
    return struct.unpack("<I", packed)[0]


def _field_schema(field: FieldDescriptor, parent_desc: Descriptor) -> Dict[str, Any]:
    is_repeated = field.label == FieldDescriptor.LABEL_REPEATED
    info: Dict[str, Any] = {
        "name": field.name,
        "label": humanize_field_name(field.name),
        "repeated": is_repeated,
    }

    if parent_desc.name in IPV4_SUBMESSAGE_NAMES and field.type == FieldDescriptor.TYPE_FIXED32:
        info["type"] = "ipv4"
        info["placeholder"] = "0.0.0.0"
        return info

    if field.name == "position_flags" and parent_desc.name == "PositionConfig":
        flags_enum = parent_desc.enum_types_by_name.get("PositionFlags")
        info["type"] = "bitflags"
        info["flags"] = [
            {"name": v.name, "value": v.number}
            for v in (flags_enum.values if flags_enum else [])
            if v.number != 0
        ]
        return info

    if field.name == "ignore_incoming" and is_repeated:
        info["type"] = "nodenum_list"
        info["placeholder"] = "!12345678, !87654321"
        return info

    if field.type == FieldDescriptor.TYPE_BOOL:
        info["type"] = "bool"
    elif field.type == FieldDescriptor.TYPE_ENUM:
        info["type"] = "enum"
        info["options"] = [
            {"name": v.name, "value": v.number}
            for v in field.enum_type.values
        ]
    elif field.type == FieldDescriptor.TYPE_STRING:
        info["type"] = "password" if field.name in PASSWORD_FIELDS else "string"
    elif field.type == FieldDescriptor.TYPE_BYTES:
        info["type"] = "bytes_list" if is_repeated else "bytes"
        if parent_desc.name == "SecurityConfig" and field.name in READONLY_SECURITY_FIELDS:
            info["readOnly"] = True
        if parent_desc.name == "SecurityConfig" and field.name == "admin_key":
            info["maxItems"] = 3
            info["helper"] = "admin_key"
    elif field.type in INTEGER_TYPES:
        info["type"] = "uint" if field.type in UNSIGNED_TYPES else "int"
    elif field.type in FLOAT_TYPES:
        info["type"] = "float"
    elif field.type == FieldDescriptor.TYPE_MESSAGE:
        info["type"] = "message_list" if is_repeated else "message"
        info["fields"] = descriptor_to_schema(field.message_type)
    else:
        info["type"] = "string"

    return info


def descriptor_to_schema(desc: Descriptor) -> List[Dict[str, Any]]:
    """Build UI field schema list for a protobuf ``Descriptor``."""
    return [_field_schema(f, desc) for f in desc.fields]


def build_full_schema() -> Dict[str, Any]:
    """Return the complete schema catalog for Owner, Radio Config, Module Config, and Channels."""
    config_sections: List[Dict[str, Any]] = []
    for name in CONFIG_SECTION_ENUM:
        f_desc = config_pb2.Config.DESCRIPTOR.fields_by_name.get(name)
        if f_desc is None or f_desc.message_type is None:
            continue
        meta = SECTION_METADATA.get(name, {})
        fields = descriptor_to_schema(f_desc.message_type)
        extra_fields: List[Dict[str, Any]] = []
        if name == "position":
            extra_fields = [
                {
                    "name": "_fixed_latitude",
                    "label": "Fixed Latitude (°)",
                    "type": "float",
                    "virtual": True,
                    "placeholder": "e.g. 38.8977",
                },
                {
                    "name": "_fixed_longitude",
                    "label": "Fixed Longitude (°)",
                    "type": "float",
                    "virtual": True,
                    "placeholder": "e.g. -77.0365",
                },
                {
                    "name": "_fixed_altitude",
                    "label": "Fixed Altitude (m)",
                    "type": "int",
                    "virtual": True,
                    "placeholder": "e.g. 120",
                },
            ]
        config_sections.append(
            {
                "key": name,
                "kind": "config",
                "title": meta.get("title", humanize_field_name(name)),
                "description": meta.get("description", ""),
                "warning": meta.get("warning", ""),
                "fields": fields + extra_fields,
            }
        )

    module_sections: List[Dict[str, Any]] = []
    for name in MODULE_SECTION_ENUM:
        f_desc = module_config_pb2.ModuleConfig.DESCRIPTOR.fields_by_name.get(name)
        if f_desc is None or f_desc.message_type is None:
            continue
        meta = SECTION_METADATA.get(name, {})
        fields = descriptor_to_schema(f_desc.message_type)
        if name == "external_notification":
            fields.append(
                {
                    "name": "_ringtone",
                    "label": "RTTTL Ringtone",
                    "type": "string",
                    "virtual": True,
                    "placeholder": "24:d=4,o=5,b=120:...",
                }
            )
        elif name == "canned_message":
            fields.append(
                {
                    "name": "_canned_messages",
                    "label": "Canned Messages (pipe '|' separated, max 200 chars)",
                    "type": "string",
                    "virtual": True,
                    "placeholder": "Copy|On my way|Need assistance|All clear",
                }
            )
        module_sections.append(
            {
                "key": name,
                "kind": "module",
                "title": meta.get("title", humanize_field_name(name)),
                "description": meta.get("description", ""),
                "warning": meta.get("warning", ""),
                "fields": fields,
            }
        )

    owner_schema = {
        "key": "owner",
        "kind": "owner",
        "title": "Identity (Owner)",
        "description": "Node display name (Long Name), 4-character badge (Short Name), ham license flag, and unmessagable flag.",
        "fields": [
            {"name": "long_name", "label": "Long Name", "type": "string", "maxLength": 39},
            {"name": "short_name", "label": "Short Name (max 4 chars)", "type": "string", "maxLength": 4},
            {"name": "is_licensed", "label": "Licensed Amateur Radio Operator (disables encryption)", "type": "bool"},
            {"name": "is_unmessagable", "label": "Unmessagable (infrastructure / sensor node)", "type": "bool"},
        ],
    }

    channel_schema = {
        "key": "channels",
        "kind": "channels",
        "title": "Channels (0–7)",
        "description": "Configure Primary (0) and Secondary (1–7) LoRa channels, encryption PSKs, MQTT uplink/downlink, and position precision.",
        "roles": [
            {"name": v.name, "value": v.number}
            for v in channel_pb2.Channel.Role.DESCRIPTOR.values
        ],
        "fields": descriptor_to_schema(channel_pb2.ChannelSettings.DESCRIPTOR),
    }

    return {
        "owner": owner_schema,
        "config": config_sections,
        "module": module_sections,
        "channels": channel_schema,
    }


def protobuf_to_dict(msg: Message) -> Dict[str, Any]:
    """Convert ANY protobuf message into a complete dict INCLUDING default/zero values
    so every parameter in the UI displays its exact current value."""
    desc = msg.DESCRIPTOR
    result: Dict[str, Any] = {}
    for field in desc.fields:
        val = getattr(msg, field.name)
        is_repeated = field.label == FieldDescriptor.LABEL_REPEATED

        if desc.name in IPV4_SUBMESSAGE_NAMES and field.type == FieldDescriptor.TYPE_FIXED32:
            result[field.name] = ipv4_int_to_str(int(val))
            continue

        if field.name == "ignore_incoming" and is_repeated:
            result[field.name] = [f"!{int(x):08x}" for x in val]
            continue

        if field.type == FieldDescriptor.TYPE_MESSAGE:
            if is_repeated:
                result[field.name] = [protobuf_to_dict(item) for item in val]
            else:
                result[field.name] = protobuf_to_dict(val)
        elif field.type == FieldDescriptor.TYPE_BYTES:
            if is_repeated:
                result[field.name] = [
                    base64.b64encode(bytes(b)).decode("ascii")
                    for b in val
                    if b
                ]
            else:
                result[field.name] = base64.b64encode(bytes(val)).decode("ascii") if val else ""
        elif field.type == FieldDescriptor.TYPE_FLOAT:
            # Avoid float32 repr noise like 0.10000000149011612
            result[field.name] = round(float(val), 6)
        elif field.type == FieldDescriptor.TYPE_DOUBLE:
            result[field.name] = float(val)
        elif field.type == FieldDescriptor.TYPE_BOOL:
            result[field.name] = bool(val)
        elif field.type in INTEGER_TYPES or field.type == FieldDescriptor.TYPE_ENUM:
            if is_repeated:
                result[field.name] = [int(x) for x in val]
            else:
                result[field.name] = int(val)
        else:
            if is_repeated:
                result[field.name] = [str(x) for x in val]
            else:
                result[field.name] = str(val)
    return result


def _decode_bytes_input(raw_val: Any) -> bytes:
    """Decode base64 or hex key string into raw bytes."""
    if isinstance(raw_val, (bytes, bytearray)):
        return bytes(raw_val)
    text = str(raw_val or "").strip()
    if not text:
        return b""
    if text.startswith("base64:"):
        text = text[len("base64:") :].strip()
    if text.startswith("0x") or text.startswith("0X"):
        return bytes.fromhex(text[2:])
    # Pad URL-safe or standard base64 if needed
    normalized = text.replace("-", "+").replace("_", "/")
    pad = len(normalized) % 4
    if pad:
        normalized += "=" * (4 - pad)
    return base64.b64decode(normalized)


def _parse_nodenum_item(item: Any) -> int:
    if isinstance(item, int):
        return item & 0xFFFFFFFF
    s = str(item or "").strip()
    if not s:
        raise ValueError("Empty node number")
    if s.startswith("!"):
        return int(s[1:], 16) & 0xFFFFFFFF
    if s.lower().startswith("0x"):
        return int(s[2:], 16) & 0xFFFFFFFF
    return int(s) & 0xFFFFFFFF


def apply_dict_to_protobuf(msg: Message, values: Dict[str, Any]) -> Message:
    """Apply user-edited JSON dict values onto ``msg`` in-place with strict type conversion."""
    desc = msg.DESCRIPTOR
    for field in desc.fields:
        if field.name not in values:
            continue
        raw_val = values[field.name]
        if raw_val is None:
            continue

        # Skip read-only public_key so users don't accidentally clobber a node's Curve25519 identity
        if desc.name == "SecurityConfig" and field.name in READONLY_SECURITY_FIELDS:
            continue

        is_repeated = field.label == FieldDescriptor.LABEL_REPEATED

        if desc.name in IPV4_SUBMESSAGE_NAMES and field.type == FieldDescriptor.TYPE_FIXED32:
            setattr(msg, field.name, ipv4_str_to_int(raw_val))
            continue

        if field.name == "ignore_incoming" and is_repeated:
            container = getattr(msg, field.name)
            del container[:]
            items = raw_val if isinstance(raw_val, list) else [x.strip() for x in str(raw_val).split(",") if x.strip()]
            for item in items:
                container.append(_parse_nodenum_item(item))
            continue

        if field.type == FieldDescriptor.TYPE_MESSAGE:
            sub_target = getattr(msg, field.name)
            if is_repeated:
                del sub_target[:]
                if isinstance(raw_val, list):
                    for item_dict in raw_val:
                        if isinstance(item_dict, dict):
                            elem = sub_target.add()
                            apply_dict_to_protobuf(elem, item_dict)
            else:
                if isinstance(raw_val, dict):
                    apply_dict_to_protobuf(sub_target, raw_val)
        elif field.type == FieldDescriptor.TYPE_BYTES:
            if is_repeated:
                container = getattr(msg, field.name)
                del container[:]
                items = raw_val if isinstance(raw_val, list) else [raw_val]
                for item in items:
                    b = _decode_bytes_input(item)
                    if b:
                        container.append(b)
            else:
                setattr(msg, field.name, _decode_bytes_input(raw_val))
        elif field.type == FieldDescriptor.TYPE_BOOL:
            if isinstance(raw_val, str):
                setattr(msg, field.name, raw_val.strip().lower() in ("1", "true", "yes", "on"))
            else:
                setattr(msg, field.name, bool(raw_val))
        elif field.type == FieldDescriptor.TYPE_ENUM:
            if isinstance(raw_val, str) and not raw_val.lstrip("-").isdigit():
                enum_val = field.enum_type.values_by_name[raw_val].number
                setattr(msg, field.name, enum_val)
            else:
                setattr(msg, field.name, int(raw_val))
        elif field.type in INTEGER_TYPES:
            if is_repeated:
                container = getattr(msg, field.name)
                del container[:]
                for item in (raw_val if isinstance(raw_val, list) else []):
                    container.append(int(item))
            else:
                if raw_val == "":
                    setattr(msg, field.name, 0)
                else:
                    setattr(msg, field.name, int(float(raw_val)))
        elif field.type in FLOAT_TYPES:
            if raw_val == "":
                setattr(msg, field.name, 0.0)
            else:
                setattr(msg, field.name, float(raw_val))
        else:
            if is_repeated:
                container = getattr(msg, field.name)
                del container[:]
                for item in (raw_val if isinstance(raw_val, list) else []):
                    container.append(str(item))
            else:
                setattr(msg, field.name, str(raw_val))

    return msg


def channel_to_dict(ch: channel_pb2.Channel) -> Dict[str, Any]:
    """Serialize a ``Channel`` protobuf to UI-friendly JSON including readable PSK representation."""
    settings_dict = protobuf_to_dict(ch.settings)
    psk_bytes = bytes(ch.settings.psk)
    if psk_bytes == b"":
        psk_mode = "none"
        psk_display = "none"
    elif psk_bytes == b"\x01":
        psk_mode = "default"
        psk_display = "default"
    elif len(psk_bytes) == 1:
        psk_mode = "simple"
        psk_display = pskToString(psk_bytes)
    else:
        psk_mode = "custom"
        psk_display = f"base64:{base64.b64encode(psk_bytes).decode('ascii')}"

    return {
        "index": int(ch.index),
        "role": int(ch.role),
        "role_name": channel_pb2.Channel.Role.Name(ch.role),
        "psk_mode": psk_mode,
        "psk_text": psk_display,
        "psk_bits": len(psk_bytes) * 8 if len(psk_bytes) in (16, 32) else (8 if len(psk_bytes) == 1 else 0),
        "settings": settings_dict,
    }


def apply_dict_to_channel(ch: channel_pb2.Channel, data: Dict[str, Any], index: int) -> channel_pb2.Channel:
    """Apply edited channel fields (including friendly PSK strings like 'default', 'none', 'random256',
    'base64:...', or '0x...') onto ``Channel`` protobuf."""
    ch.index = index
    role_val = data.get("role", ch.role)
    if isinstance(role_val, str) and not role_val.isdigit():
        ch.role = channel_pb2.Channel.Role.Value(role_val)
    else:
        ch.role = int(role_val)

    settings_in = dict(data.get("settings") or {})
    psk_text = data.get("psk_text")
    # Remove raw psk from settings_in if psk_text was supplied so psk_text takes precedence
    if psk_text is not None:
        settings_in.pop("psk", None)

    apply_dict_to_protobuf(ch.settings, settings_in)

    if psk_text is not None:
        cleaned = str(psk_text).strip()
        lower = cleaned.lower()
        if lower in ("", "none", "off"):
            ch.settings.psk = b""
        elif lower == "default":
            ch.settings.psk = b"\x01"
        elif lower in ("random", "random256"):
            ch.settings.psk = secrets.token_bytes(32)
        elif lower == "random128":
            ch.settings.psk = secrets.token_bytes(16)
        elif lower.startswith("simple"):
            ch.settings.psk = fromPSK(cleaned)
        elif cleaned.startswith("base64:") or cleaned.startswith("0x") or cleaned.startswith("0X"):
            ch.settings.psk = _decode_bytes_input(cleaned)
        else:
            # Accept raw base64 (16 or 32 byte keys) or meshtastic fromPSK
            try:
                decoded = _decode_bytes_input(cleaned)
                if len(decoded) in (0, 1, 16, 32):
                    ch.settings.psk = decoded
                else:
                    ch.settings.psk = fromPSK(cleaned)
            except Exception:
                ch.settings.psk = fromPSK(cleaned)

    return ch
