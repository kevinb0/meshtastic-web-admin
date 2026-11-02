# Meshtastic Web Admin

A Python web dashboard for pairing to a Wi-Fi–connected Meshtastic **Admin Node** over HTTP (`http://meshtastic.local`), monitoring live mesh telemetry and multi-hop RF relays, identifying controllable nodes at a glance, enrolling USB nodes for Public Key Cryptography (PKC) remote administration, and configuring every radio, module, and channel parameter of remote nodes from a single browser UI.

---

## Key Features

### 1. Native HTTP Pairing to an Admin Node (`http://meshtastic.local`)
- Connects over Meshtastic's native protobuf HTTP transport (`PUT /api/v1/toradio` and `GET /api/v1/fromradio?all=false`) via `HTTPMeshInterface` in [`meshtastic_http.py`](meshtastic_http.py).
- Also supports `https://<host>` and Meshtastic TCP (`tcp://<host>:4403`).
- Displays the paired Admin Node's hardware model, firmware version, LoRa region & modem preset, battery/power status, and a 1-click **Copy Admin Public Key** button for PKC remote admin setup.

### 2. Live Mesh Status Dashboard
- **Signal Quality:** Live `SNR` (dB), `RSSI` (dBm), and 4-bar RF link indicator per node.
- **Battery & Power:** Battery `%` progress bar, external USB power badge, voltage (`V`), and channel utilization (`%`).
- **Last Message & Packet Activity:** Per-node latest text message preview, slide-out mesh text chat drawer (with broadcast & direct messaging), last heard packet type (`TEXT`, `POSITION`, `TELEMETRY`, `NODEINFO`, `ADMIN`, `TRACEROUTE`), and live relative `lastHeard` timestamps.
- **Instant Filtering & Sorting:** Filter the mesh view by **All Nodes**, **Admin Controllable**, **Direct RF**, or **Relayed (1+ Hops)**, sort ascending/descending by **Battery** (`Low→High` to immediately spot low batteries, or `High→Low`) or **Name** (`A→Z` / `Z→A` with natural numeric ordering), and search by node name, short name, hardware model, or `!hex` ID.

### 3. Multi-Hop Relay Topology & Live Traceroute
- Distinguishes **Direct RF (`0` hops)**, **Relayed (`1+` hops)**, and **Via MQTT** links.
- Combines `MeshPacket.relay_node` (lowest-byte node number matching), `NeighborInfo` broadcasts, and `TRACEROUTE_APP` paths to display:
  - **Upstream Relay:** Which intermediate node forwarded a remote node's packet (e.g., `Relayed via Squad 6 (!22222259)`).
  - **Downstream Dependents:** Which far nodes an intermediate relay is actively forwarding for (`Relaying for: ...`).
- Built-in 1-click **Trace** button on every node card to run live hop-by-hop `TRACEROUTE_APP` route discovery with forward and return SNR readings.

### 4. Distinct Admin Power Icon & Full Remote Node Configuration
- Automatically and politely verifies which mesh nodes grant **Remote Admin Power** to the paired Admin Node (via Meshtastic 2.5+ PKC `security.admin_key` + `session_passkey`, or legacy `admin` channel) and caches verified pairs locally in `admin_cache.json`.
- **Visual Distinction:**
  - **Controllable Nodes:** Highlighted with a clickable **Gold Shield-Key** button.
  - **Standard Peer Nodes:** Shown with a neutral slate radio icon.
- Clicking a node's **Gold Shield-Key** opens a schema-driven configuration modal generated dynamically from Meshtastic's protobuf descriptors ([`schema_utils.py`](schema_utils.py)):
  - **Identity (Owner):** Long Name, Short Name, Licensed Amateur (HAM) mode, Unmessagable flag.
  - **Radio Config (All 9 Sections):** `Device`, `Position & GPS` (fixed Lat/Lon/Alt + `position_flags` bitmask editor), `Power`, `Network` (Wi-Fi, Ethernet, IPv4), `Display`, `LoRa`, `Bluetooth`, `Security & Admin Keys` (with 1-click *Insert Paired Admin Node Public Key*), and `Device UI`.
  - **Module Config (All 16 Sections):** `MQTT`, `Telemetry`, `Neighbor Info`, `TAK / ATAK`, `Serial`, `External Notification` (+ Ringtone), `Store & Forward`, `Range Test`, `Canned Messages`, `Audio`, `Remote Hardware`, `Ambient Lighting`, `Detection Sensor`, `Paxcounter`, `Status Message`, and `Traffic Management`.
  - **Channels (`0–7`):** Primary & Secondary channel editor with PSK presets (`default`, `none`, `random256`, or custom Base64), MQTT uplink/downlink toggles, and per-channel position precision.
  - **Remote Actions:** Sync Node Clock, Live Traceroute, Clear Fixed Position, Reboot, Reset NodeDB, and Shutdown.

### 5. 1-Click USB Node Admin Key Enrollment
- Plug any fleet Meshtastic node into USB and click **Enroll USB Node** in the top bar to grant your paired HTTP Admin Node remote PKC admin control over it.
- **Why this exists (solving the 3-slot nanopb & `writeConfig` pitfalls):**
  1. Meshtastic firmware enforces a hard nanopb limit of **`max_count = 3`** entries in `security.admin_key`. Running `meshtastic --set security.admin_key "base64:..."` appends a 4th key when all 3 slots are occupied, causing the firmware decoder to reject the write even though the CLI prints success.
  2. `meshtastic` Python SDK `Node.writeConfig()` can omit `ensureSessionKey()` on standalone serial writes, causing PKC-enabled firmware to ignore the config update.
  3. Meshtastic `2.5.x` vs `2.7.x` firmware handle local USB serial `pki_encrypted` flags and LittleFS commit timing differently.
- **Smart Slot Management:** Inspects all 3 `admin_key` slots on the USB node, cross-references existing keys against known nodes in your mesh, automatically drops stale/unrecognized keys if all 3 slots are full, writes a properly `session_passkey`–authenticated `begin_edit_settings` / `commit_edit_settings` transaction across both `2.5.x` and `2.7.x` firmware, and syncs the enrolled node's public key straight into the active Admin Node session.

### 6. Batch Fleet Actions & Multi-Node Force Parameter Push
- Check multiple nodes in the mesh table (or use the header checkbox to select visible remote peers) and click **Edit Selected Nodes (N)** in the toolbar.
- **Zero Pre-Read Mode:** Opens immediately (`0 ms`) without polling or reading current parameters from any target node.
- **Fleet Power & Maintenance Actions:** Run **Shutdown Selected Nodes** (turn off all selected radios), **Reboot Selected Nodes**, **Sync All Clocks**, **Clear Fixed Positions**, or **Reset NodeDB** sequentially across the mesh — automatically ordering remote peers first and the local paired Admin Node last so your gateway never powers off before transmitting commands to the rest of the fleet.
- **Multi-Section Force Parameter Override:** Check **Force parameter** on any combination of fields across all 9 Radio Config sections, all 16 Module Config sections, and Channels `0–7` (editing any field checks it automatically). Pushes all staged overrides per node inside a single `begin_edit_settings` / `commit_edit_settings` transaction with live per-node progress and an optional **Stop Remaining** cancel control.

---

## Architecture

```
Browser UI (templates/index.html, static/app.js, static/styles.css)
       │
       │  JSON REST API (Flask - app.py)
       ▼
MeshAdminManager (mesh_manager.py)  ◄──── Dynamic Protobuf Schema (schema_utils.py)
       │                     │
       │ HTTP / HTTPS / TCP  │ USB Serial (COMx / /dev/ttyUSBx)
       ▼                     ▼
HTTPMeshInterface      SerialInterface
(meshtastic_http.py)   (1-Click USB Key Enrollment)
       │
       ▼
Paired "Admin Node" (http://meshtastic.local)
       │
       │  LoRa Mesh RF (Direct & Multi-Hop Relays, PKC Remote Admin)
       ▼
Fleet Mesh Nodes (!xxxxxxxx)
```

---

## Installation & Quick Start

### Prerequisites
- Python **3.10+**
- A Meshtastic **Admin Node** reachable over Wi-Fi/HTTP (e.g., `http://meshtastic.local` or its LAN IP) or TCP (`tcp://<ip>:4403`).

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/kevinb0/meshtastic-web-admin.git
cd meshtastic-web-admin

python -m venv .venv
```

Activate the virtual environment and install dependencies:

- **Windows (PowerShell):**
  ```powershell
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```
- **Linux / macOS:**
  ```bash
  source .venv/bin/activate
  pip install -r requirements.txt
  ```

### 2. Run the Web Interface

```bash
python app.py
```

*(Note: If a `.venv` directory is present in the project root, `app.py` will automatically re-exec into `.venv` if launched from a global Python interpreter with an older `meshtastic` package.)*

Open **http://127.0.0.1:5050** in your browser, enter `http://meshtastic.local` (or your Admin Node's IP address such as `http://192.168.1.50`), and click **Pair via HTTP**.

> **Important:** Meshtastic's built-in HTTP `/api/v1/fromradio` queue is single-consumer. Close any open `http://meshtastic.local` browser tabs while paired so the built-in web client and this dashboard do not compete for incoming radio packets.

### CLI Options

```bash
python app.py --host 127.0.0.1 --port 5050 --target http://meshtastic.local --auto-connect --open-browser
```

| Flag | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | `HOST` | `127.0.0.1` | Local interface address to bind the Flask server |
| `--port` | `PORT` | `5050` | HTTP port for the web dashboard |
| `--target` | `MESHTASTIC_TARGET` | `http://meshtastic.local` | Default Admin Node address (`http://`, `https://`, or `tcp://`) |
| `--auto-connect` | — | `false` | Pair to `--target` automatically on startup |
| `--open-browser` | — | `false` | Launch the dashboard in your default browser on startup |

---

## Enrolling Remote Nodes for Admin Power

To configure fleet nodes over the air without pairing to each device individually:

1. **Via the Built-In USB Enroller (Recommended):**
   - Pair the dashboard to your HTTP Admin Node first (or paste its Base64 public key).
   - Plug a fleet node into USB on the computer running `app.py`.
   - Click **Enroll USB Node** in the header, select the COM/serial port, click **Inspect**, and click **Enroll Admin Key**.
2. **Via Meshtastic App / Web Client:**
   - Click **Copy Admin Key** in the Admin Node summary banner.
   - Paste that Base64 public key into **Radio Configuration -> Security -> Admin Key** on each target node.
   - Click **Verify Admin** in the dashboard to confirm remote admin access and unlock the **Gold Shield-Key** configuration modal.

---

## Project Structure

| File | Purpose |
| :--- | :--- |
| [`app.py`](app.py) | Entry point, `.venv` auto-bootstrap, and Flask JSON REST endpoints |
| [`meshtastic_http.py`](meshtastic_http.py) | `HTTPMeshInterface` (`MeshInterface` subclass over `/api/v1/toradio` & `/api/v1/fromradio`) and `HookedTCPInterface` |
| [`mesh_manager.py`](mesh_manager.py) | `MeshAdminManager` — node state, signal/battery/message tracking, multi-hop relay resolution, PKC remote admin transactions, and USB enrollment |
| [`schema_utils.py`](schema_utils.py) | Protobuf descriptor introspection and bidirectional JSON <-> Protobuf conversion for all `Config`, `ModuleConfig`, and `Channel` messages |
| [`templates/index.html`](templates/index.html) | Main dashboard layout and modals (Full Node Config, Traceroute, USB Enrollment, Message Drawer) |
| [`static/app.js`](static/app.js) | Frontend state polling, relay badges, dynamic form renderer, channel & admin key editors |
| [`static/styles.css`](static/styles.css) | Dark responsive dashboard styling |
| [`tests/test_web_admin.py`](tests/test_web_admin.py) | End-to-end unit test suite with mock Meshtastic HTTP & USB protobuf radio interfaces |

---

## Running Tests

```bash
python -m unittest discover -s tests -v
```
