"""Meshtastic Web Admin — Python Flask web application.

Run directly with:
    python app.py

Features:
  - Pairs to a Meshtastic "Admin Node" over HTTP (default: http://meshtastic.local)
  - Displays visible mesh nodes with Signal Strength (SNR/RSSI), Battery, Last Message,
    and Relay Routing details (hop count, resolved relay node, downstream relayed nodes, traceroute)
  - Highlights nodes the Admin Node has Admin Power over with a distinct clickable Admin icon
  - Clicking the Admin icon opens a full parameter configuration popup covering Identity,
    all Radio Config sections, all Module Config sections, Channels 0–7, and Node Actions
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def _ensure_project_venv() -> None:
    """Automatically re-exec into the project's .venv if launched from global Python
    where meshtastic < 2.5 (lacking PKC Remote Admin / session_passkey)."""
    if os.environ.get("MESHTASTIC_WEB_ADMIN_REEXEC") == "1":
        return
    try:
        from meshtastic.protobuf import admin_pb2  # type: ignore

        if "session_passkey" in admin_pb2.AdminMessage.DESCRIPTOR.fields_by_name:
            return
    except Exception:
        pass

    root = pathlib.Path(__file__).resolve().parent
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    ]
    for venv_py in candidates:
        if venv_py.exists() and venv_py.resolve() != pathlib.Path(sys.executable).resolve():
            env = dict(os.environ)
            env["MESHTASTIC_WEB_ADMIN_REEXEC"] = "1"
            sys.exit(subprocess.call([str(venv_py), *sys.argv], env=env))


_ensure_project_venv()

import argparse
import atexit
import logging
import webbrowser
from typing import Optional

from flask import Flask, jsonify, render_template, request

from mesh_manager import MeshAdminManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Silence noisy werkzeug per-poll request logs while keeping warnings/errors visible
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger("meshtastic_web_admin")


def create_app(manager: Optional[MeshAdminManager] = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    mesh = manager or MeshAdminManager()
    app.config["MESH_MANAGER"] = mesh

    def _error_response(exc: Exception, default_status: int = 400):
        if isinstance(exc, TimeoutError):
            status = 504
        elif isinstance(exc, PermissionError):
            status = 403
        elif isinstance(exc, ValueError):
            status = 400
        else:
            status = default_status
        return jsonify({"ok": False, "error": str(exc)}), status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify({"ok": True, **mesh.get_state()})

    @app.get("/api/schema")
    def api_schema():
        return jsonify({"ok": True, "schema": mesh.get_schema()})

    @app.post("/api/connect")
    def api_connect():
        body = request.get_json(silent=True) or {}
        target = str(body.get("target") or "http://meshtastic.local").strip()
        try:
            state = mesh.connect(target=target)
            return jsonify({"ok": True, **state})
        except Exception as exc:
            logger.warning("Connect failed for %s: %s", target, exc)
            return _error_response(exc, default_status=502)

    @app.post("/api/disconnect")
    def api_disconnect():
        mesh.disconnect()
        return jsonify({"ok": True, **mesh.get_state()})

    @app.post("/api/probe_admin")
    def api_probe_admin():
        body = request.get_json(silent=True) or {}
        if "auto_probe_admin" in body:
            mesh.auto_probe_admin = bool(body["auto_probe_admin"])
        include_stale = bool(body.get("include_stale", False))
        res = mesh.probe_all_nodes(include_stale=include_stale)
        return jsonify({"ok": True, **res, "auto_probe_admin": mesh.auto_probe_admin})

    @app.get("/api/node/<path:node_id>/owner")
    def api_get_owner(node_id: str):
        refresh = request.args.get("refresh", "0") in ("1", "true", "yes")
        try:
            owner = mesh.get_owner(node_id, refresh=refresh)
            return jsonify({"ok": True, "node_id": node_id, "values": owner})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/node/<path:node_id>/owner")
    def api_set_owner(node_id: str):
        body = request.get_json(silent=True) or {}
        values = body.get("values") if isinstance(body.get("values"), dict) else body
        try:
            saved = mesh.set_owner(node_id, values)
            return jsonify({"ok": True, "node_id": node_id, "values": saved})
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/node/<path:node_id>/config/<kind>/<section>")
    def api_get_config_section(node_id: str, kind: str, section: str):
        refresh = request.args.get("refresh", "0") in ("1", "true", "yes")
        try:
            values = mesh.get_config_section(node_id, kind, section, refresh=refresh)
            return jsonify(
                {
                    "ok": True,
                    "node_id": node_id,
                    "kind": kind,
                    "section": section,
                    "values": values,
                }
            )
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/node/<path:node_id>/config/<kind>/<section>")
    def api_set_config_section(node_id: str, kind: str, section: str):
        body = request.get_json(silent=True) or {}
        values = body.get("values") if isinstance(body.get("values"), dict) else body
        try:
            saved = mesh.set_config_section(node_id, kind, section, values)
            return jsonify(
                {
                    "ok": True,
                    "node_id": node_id,
                    "kind": kind,
                    "section": section,
                    "values": saved,
                }
            )
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/node/<path:node_id>/channels")
    def api_get_channels(node_id: str):
        refresh = request.args.get("refresh", "0") in ("1", "true", "yes")
        try:
            channels = mesh.get_all_channels(node_id, refresh=refresh)
            return jsonify({"ok": True, "node_id": node_id, "channels": channels})
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/node/<path:node_id>/channel/<int:index>")
    def api_get_channel(node_id: str, index: int):
        refresh = request.args.get("refresh", "0") in ("1", "true", "yes")
        try:
            ch = mesh.get_channel(node_id, index, refresh=refresh)
            return jsonify({"ok": True, "node_id": node_id, "channel": ch})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/node/<path:node_id>/channel/<int:index>")
    def api_set_channel(node_id: str, index: int):
        body = request.get_json(silent=True) or {}
        payload = body.get("channel") if isinstance(body.get("channel"), dict) else body
        try:
            saved = mesh.set_channel(node_id, index, payload)
            return jsonify({"ok": True, "node_id": node_id, "channel": saved})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/node/<path:node_id>/action")
    def api_node_action(node_id: str):
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip()
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        try:
            result = mesh.perform_action(node_id, action, params=params)
            return jsonify({"ok": True, **result})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/node/<path:node_id>/batch")
    def api_node_batch(node_id: str):
        body = request.get_json(silent=True) or {}
        action = str(body.get("action") or "").strip() or None
        params = body.get("params") if isinstance(body.get("params"), dict) else None
        changes = body.get("changes") if isinstance(body.get("changes"), list) else None
        blind_overwrite = bool(body.get("blind_overwrite", False))
        try:
            res = mesh.apply_batch_to_node(
                node_id=node_id,
                action=action,
                params=params,
                changes=changes,
                blind_overwrite=blind_overwrite,
            )
            return jsonify({"ok": True, **res})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/batch")
    def api_batch():
        body = request.get_json(silent=True) or {}
        node_ids = body.get("node_ids") if isinstance(body.get("node_ids"), list) else []
        action = str(body.get("action") or "").strip() or None
        params = body.get("params") if isinstance(body.get("params"), dict) else None
        changes = body.get("changes") if isinstance(body.get("changes"), list) else None
        blind_overwrite = bool(body.get("blind_overwrite", False))
        try:
            summary = mesh.apply_batch(
                node_ids=node_ids,
                action=action,
                params=params,
                changes=changes,
                blind_overwrite=blind_overwrite,
            )
            return jsonify({"ok": True, **summary})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/messages/send")
    def api_send_message():
        body = request.get_json(silent=True) or {}
        text = str(body.get("text") or "")
        destination_id = str(body.get("destination_id") or "^all")
        channel_index = int(body.get("channel_index") or 0)
        try:
            msg = mesh.send_text_message(text=text, destination_id=destination_id, channel_index=channel_index)
            return jsonify({"ok": True, "message": msg})
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/usb/ports")
    def api_usb_ports():
        try:
            ports = mesh.list_usb_ports()
            return jsonify({"ok": True, "ports": ports})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/usb/inspect")
    def api_usb_inspect():
        body = request.get_json(silent=True) or {}
        port = str(body.get("port") or "").strip()
        try:
            info = mesh.inspect_usb_node(port=port)
            return jsonify({"ok": True, "usb_node": info})
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/usb/enroll")
    def api_usb_enroll():
        body = request.get_json(silent=True) or {}
        port = str(body.get("port") or "").strip()
        admin_pk = body.get("admin_public_key")
        keep_keys = body.get("keep_keys") if isinstance(body.get("keep_keys"), list) else None
        mode = str(body.get("mode") or "smart").strip()
        try:
            res = mesh.enroll_usb_node(
                port=port,
                admin_public_key=admin_pk,
                keep_keys=keep_keys,
                mode=mode,
            )
            return jsonify({"ok": True, "enrolled": res})
        except Exception as exc:
            return _error_response(exc)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Meshtastic Web Admin Dashboard")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5050")), help="Bind port (default: 5050)")
    parser.add_argument(
        "--target",
        default=os.environ.get("MESHTASTIC_TARGET", "http://meshtastic.local"),
        help="Default Admin Node HTTP URL (default: http://meshtastic.local)",
    )
    parser.add_argument("--auto-connect", action="store_true", help="Automatically pair to --target on startup")
    parser.add_argument("--open-browser", action="store_true", help="Open dashboard in default browser on startup")
    args = parser.parse_args()

    manager = MeshAdminManager()
    atexit.register(manager.shutdown)

    if args.auto_connect:
        try:
            logger.info("Auto-pairing to Admin Node at %s ...", args.target)
            manager.connect(args.target)
        except Exception as exc:
            logger.warning("Startup auto-connect to %s failed: %s (you can pair from the web UI)", args.target, exc)

    app = create_app(manager=manager)
    url = f"http://{args.host}:{args.port}"
    print(f"\n  Meshtastic Web Admin running at: {url}")
    print(f"  Default Admin Node target:       {args.target}\n")

    if args.open_browser:
        webbrowser.open(url)

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
