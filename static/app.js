(() => {
  "use strict";

  const state = {
    schema: null,
    snapshot: null,
    filter: "all",
    search: "",
    sortBy: "default", // 'default' | 'battery' | 'name'
    sortDir: "asc", // 'asc' | 'desc'
    pollTimer: null,
    modal: {
      open: false,
      nodeId: null,
      activeTab: null, // { type: 'owner'|'config'|'module'|'channels'|'actions', key: string }
      currentValues: null,
      channelIndex: 0,
      loading: false,
    },
  };

  // DOM Elements
  const el = {
    pairForm: document.getElementById("pair-form"),
    targetInput: document.getElementById("target-input"),
    pairBtn: document.getElementById("pair-btn"),
    disconnectBtn: document.getElementById("disconnect-btn"),
    connBadge: document.getElementById("conn-badge"),
    alertBanner: document.getElementById("alert-banner"),
    adminCard: document.getElementById("admin-node-card"),
    configureLocalBtn: document.getElementById("configure-local-btn"),
    adminName: document.getElementById("admin-name"),
    adminShort: document.getElementById("admin-short"),
    adminId: document.getElementById("admin-id"),
    adminMeta: document.getElementById("admin-meta"),
    openUsbEnrollBtn: document.getElementById("open-usb-enroll-btn"),
    copyPubkeyBtn: document.getElementById("copy-pubkey-btn"),
    probeAllBtn: document.getElementById("probe-all-btn"),
    toggleMessagesBtn: document.getElementById("toggle-messages-btn"),
    msgCountBadge: document.getElementById("msg-count-badge"),
    unpairedEmpty: document.getElementById("unpaired-empty"),
    dashboardMain: document.getElementById("dashboard-main"),
    kpiTotal: document.getElementById("kpi-total"),
    kpiAdmin: document.getElementById("kpi-admin"),
    kpiDirect: document.getElementById("kpi-direct"),
    kpiRelayed: document.getElementById("kpi-relayed"),
    nodeSortBy: document.getElementById("node-sort-by"),
    nodeSortDir: document.getElementById("node-sort-dir"),
    thSortName: document.getElementById("th-sort-name"),
    thSortBattery: document.getElementById("th-sort-battery"),
    nodeSearch: document.getElementById("node-search"),
    probeStatusBar: document.getElementById("probe-status-bar"),
    nodesTbody: document.getElementById("nodes-tbody"),
    messagesPanel: document.getElementById("messages-panel"),
    closeMessagesBtn: document.getElementById("close-messages-btn"),
    messagesList: document.getElementById("messages-list"),
    sendMsgForm: document.getElementById("send-msg-form"),
    msgDestSelect: document.getElementById("msg-dest-select"),
    msgChSelect: document.getElementById("msg-ch-select"),
    msgTextInput: document.getElementById("msg-text-input"),
    // Modal
    configModal: document.getElementById("config-modal"),
    modalCloseBtn: document.getElementById("modal-close-btn"),
    modalNodeTitle: document.getElementById("modal-node-title"),
    modalNodeShort: document.getElementById("modal-node-short"),
    modalNodeId: document.getElementById("modal-node-id"),
    modalNodeSubtitle: document.getElementById("modal-node-subtitle"),
    modalNav: document.getElementById("modal-nav"),
    sectionTitle: document.getElementById("section-title"),
    sectionDesc: document.getElementById("section-desc"),
    sectionWarning: document.getElementById("section-warning"),
    sectionStatus: document.getElementById("section-status"),
    reloadSectionBtn: document.getElementById("reload-section-btn"),
    saveSectionBtn: document.getElementById("save-section-btn"),
    sectionFormContainer: document.getElementById("section-form-container"),
    // Trace modal
    traceModal: document.getElementById("trace-modal"),
    traceModalTitle: document.getElementById("trace-modal-title"),
    traceModalBody: document.getElementById("trace-modal-body"),
    traceCloseBtn: document.getElementById("trace-close-btn"),
    // USB Enrollment modal
    usbModal: document.getElementById("usb-modal"),
    usbCloseBtn: document.getElementById("usb-close-btn"),
    usbPortSelect: document.getElementById("usb-port-select"),
    usbRefreshPortsBtn: document.getElementById("usb-refresh-ports-btn"),
    usbInspectBtn: document.getElementById("usb-inspect-btn"),
    usbStatusBanner: document.getElementById("usb-status-banner"),
    usbNodeDetails: document.getElementById("usb-node-details"),
    usbEnrollFooter: document.getElementById("usb-enroll-footer"),
    usbWriteKeyBtn: document.getElementById("usb-write-key-btn"),
    toastContainer: document.getElementById("toast-container"),
  };

  // Icons
  const ADMIN_SHIELD_SVG = `
    <svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="1.95">
      <path d="M12 2l7 3v6c0 5-3.2 9.4-7 11-3.8-1.6-7-6-7-11V5l7-3z"/>
      <circle cx="10.2" cy="11.2" r="2.1"/>
      <path d="M12.1 11.2H16v2.2M14.2 11.2v1.8" stroke-linecap="round"/>
    </svg>`;

  const STANDARD_NODE_SVG = `
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.85">
      <circle cx="12" cy="12" r="2.3"/>
      <path d="M8.2 8.2a5.4 5.4 0 0 0 0 7.6M15.8 8.2a5.4 5.4 0 0 1 0 7.6" stroke-linecap="round"/>
      <path d="M5.2 5.2a9.6 9.6 0 0 0 0 13.6M18.8 5.2a9.6 9.6 0 0 1 0 13.6" stroke-linecap="round"/>
    </svg>`;

  function escapeHtml(val) {
    return String(val ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function showToast(msg, kind = "ok") {
    const t = document.createElement("div");
    t.className = `toast ${kind}`;
    t.textContent = msg;
    el.toastContainer.appendChild(t);
    setTimeout(() => t.remove(), 4200);
  }

  async function apiJson(url, options = {}) {
    const opts = {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    };
    const resp = await fetch(url, opts);
    const data = await resp.json().catch(() => ({ ok: false, error: `HTTP ${resp.status}` }));
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || `Request failed (${resp.status})`);
    }
    return data;
  }

  function formatAgo(epochSec, nowSec) {
    if (!epochSec) return "Never";
    const diff = Math.max(0, (nowSec || Math.floor(Date.now() / 1000)) - epochSec);
    if (diff < 5) return "just now";
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  // ------------------------------------------------------------------
  // Dashboard Rendering
  // ------------------------------------------------------------------
  async function loadSchema() {
    if (state.schema) return state.schema;
    const res = await apiJson("/api/schema");
    state.schema = res.schema;
    return state.schema;
  }

  async function refreshState() {
    try {
      const snap = await apiJson("/api/state");
      state.snapshot = snap;
      renderSnapshot(snap);
    } catch (err) {
      console.warn("State poll error:", err);
    }
  }

  function renderSnapshot(snap) {
    const conn = snap.connection || {};
    const admin = snap.admin_node;
    const now = snap.server_time;

    // Connection badge & controls
    if (conn.paired && conn.connected) {
      el.connBadge.className = "status-pill connected";
      el.connBadge.textContent = `Paired · ${conn.target}`;
      el.pairBtn.textContent = "Re-Pair";
      el.disconnectBtn.classList.remove("hidden");
    } else if (conn.paired || conn.connecting) {
      el.connBadge.className = "status-pill syncing";
      el.connBadge.textContent = "Syncing NodeDB...";
      el.disconnectBtn.classList.remove("hidden");
    } else {
      el.connBadge.className = "status-pill disconnected";
      el.connBadge.textContent = "Not Paired";
      el.pairBtn.textContent = "Pair via HTTP";
      el.disconnectBtn.classList.add("hidden");
    }

    if (conn.error) {
      el.alertBanner.textContent = conn.error;
      el.alertBanner.classList.remove("hidden");
    } else {
      el.alertBanner.classList.add("hidden");
    }

    if (!conn.paired) {
      el.adminCard.classList.add("hidden");
      el.dashboardMain.classList.add("hidden");
      el.unpairedEmpty.classList.remove("hidden");
      return;
    }

    el.unpairedEmpty.classList.add("hidden");
    el.dashboardMain.classList.remove("hidden");

    if (admin) {
      el.adminCard.classList.remove("hidden");
      el.adminName.textContent = admin.long_name;
      el.adminShort.textContent = admin.short_name;
      el.adminId.textContent = admin.node_id;
      el.configureLocalBtn.onclick = () => openConfigModal(admin.node_id);

      const metaBits = [];
      if (admin.hw_model) metaBits.push(`<span>HW: <strong>${escapeHtml(admin.hw_model)}</strong></span>`);
      if (admin.firmware_version) metaBits.push(`<span>FW: <strong>${escapeHtml(admin.firmware_version)}</strong></span>`);
      if (admin.role) metaBits.push(`<span>Role: <strong>${escapeHtml(admin.role)}</strong></span>`);
      if (admin.lora_region) metaBits.push(`<span>LoRa: <strong>${escapeHtml(admin.lora_region)} / ${escapeHtml(admin.lora_preset || "")}</strong></span>`);
      if (admin.is_powered) {
        metaBits.push(`<span>Power: <strong>External USB ⚡${admin.voltage ? ` (${admin.voltage}V)` : ""}</strong></span>`);
      } else if (admin.battery_level !== null && admin.battery_level !== undefined) {
        metaBits.push(`<span>Battery: <strong>${admin.battery_level}%${admin.voltage ? ` (${admin.voltage}V)` : ""}</strong></span>`);
      }
      el.adminMeta.innerHTML = metaBits.join("");
    } else {
      el.adminCard.classList.add("hidden");
    }

    // KPIs
    el.kpiTotal.textContent = snap.counts.total;
    el.kpiAdmin.textContent = snap.counts.admin_controllable;
    el.kpiDirect.textContent = snap.counts.direct;
    el.kpiRelayed.textContent = snap.counts.relayed;
    el.msgCountBadge.textContent = (snap.messages || []).length;

    // Probing banner
    if (conn.probing_node_id || conn.probe_queue_len > 0) {
      const targetTxt = conn.probing_node_id ? `checking ${conn.probing_node_id}` : "queued";
      el.probeStatusBar.textContent = `Verifying Remote Admin Power over LoRa (${targetTxt}, ${conn.probe_queue_len} remaining in queue)...`;
      el.probeStatusBar.classList.remove("hidden");
    } else {
      el.probeStatusBar.classList.add("hidden");
    }

    renderNodesTable(snap.nodes || [], now);
    renderMessagesPanel(snap.messages || [], snap.nodes || [], now);
  }

  function getBatterySortInfo(node) {
    const b = node.battery || {};
    if (b.is_powered) {
      return { known: true, val: 101, voltage: Number(b.voltage ?? 999) };
    }
    if (b.level !== null && b.level !== undefined) {
      return { known: true, val: Number(b.level), voltage: Number(b.voltage ?? 0) };
    }
    return { known: false, val: null, voltage: 0 };
  }

  function compareNodeNames(a, b) {
    const na = String(a.long_name || a.short_name || a.node_id || "");
    const nb = String(b.long_name || b.short_name || b.node_id || "");
    const cmp = na.localeCompare(nb, undefined, { numeric: true, sensitivity: "base" });
    return cmp !== 0 ? cmp : String(a.node_id || "").localeCompare(String(b.node_id || ""));
  }

  function syncSortControlsUI() {
    if (el.nodeSortBy) el.nodeSortBy.value = state.sortBy;
    if (el.nodeSortDir) {
      const isDefault = state.sortBy === "default";
      el.nodeSortDir.disabled = isDefault;
      if (isDefault) {
        el.nodeSortDir.innerHTML = "&#8593; Asc";
      } else if (state.sortBy === "battery") {
        el.nodeSortDir.innerHTML =
          state.sortDir === "asc" ? "&#8593; Low&rarr;High" : "&#8595; High&rarr;Low";
      } else {
        el.nodeSortDir.innerHTML =
          state.sortDir === "asc" ? "&#8593; A&rarr;Z" : "&#8595; Z&rarr;A";
      }
    }
    document.querySelectorAll(".nodes-table th.sortable-th").forEach((th) => {
      th.classList.toggle("active-sort", th.dataset.sort === state.sortBy);
    });
    if (el.thSortName) {
      el.thSortName.textContent =
        state.sortBy === "name" ? (state.sortDir === "asc" ? "▲" : "▼") : "";
    }
    if (el.thSortBattery) {
      el.thSortBattery.textContent =
        state.sortBy === "battery" ? (state.sortDir === "asc" ? "▲" : "▼") : "";
    }
  }

  function renderNodesTable(nodes, nowSec) {
    syncSortControlsUI();
    const q = state.search.trim().toLowerCase();
    const filtered = nodes.filter((n) => {
      if (state.filter === "admin" && !n.has_admin) return false;
      if (state.filter === "direct" && (n.is_admin_node || n.relay.link_type !== "direct")) return false;
      if (state.filter === "relayed" && n.relay.link_type !== "relayed") return false;
      if (!q) return true;
      const viaName = n.relay?.via_node?.name || "";
      const hay = `${n.long_name} ${n.short_name} ${n.node_id} ${n.hw_model} ${n.role} ${viaName}`.toLowerCase();
      return hay.includes(q);
    });

    if (state.sortBy !== "default") {
      const dir = state.sortDir === "desc" ? -1 : 1;
      filtered.sort((a, b) => {
        if (state.sortBy === "battery") {
          const ba = getBatterySortInfo(a);
          const bb = getBatterySortInfo(b);
          // Keep unknown battery readings at the bottom regardless of asc/desc
          if (ba.known !== bb.known) return ba.known ? -1 : 1;
          if (ba.known && bb.known) {
            if (ba.val !== bb.val) return (ba.val - bb.val) * dir;
            if (ba.voltage !== bb.voltage) return (ba.voltage - bb.voltage) * dir;
          }
          return compareNodeNames(a, b);
        }
        if (state.sortBy === "name") {
          return compareNodeNames(a, b) * dir;
        }
        return 0;
      });
    }

    if (filtered.length === 0) {
      el.nodesTbody.innerHTML = `
        <tr>
          <td colspan="7" style="text-align:center; padding: 32px; color: var(--text-muted);">
            No matching mesh nodes found.
          </td>
        </tr>`;
      return;
    }

    el.nodesTbody.innerHTML = filtered
      .map((n) => {
        const iconCell = n.has_admin
          ? `<button type="button" class="node-icon-btn admin-power js-open-admin" data-node-id="${escapeHtml(n.node_id)}"
               title="Admin Power (${escapeHtml(n.admin_source || "verified")}) — Click to configure all parameters of ${escapeHtml(n.long_name)}">
               ${ADMIN_SHIELD_SVG}
             </button>
             <span class="icon-caption">ADMIN</span>`
          : `<div class="node-icon-static non-admin" title="Standard Mesh Node (Admin Power not verified)">
               ${STANDARD_NODE_SVG}
             </div>
             <span class="icon-caption muted">${n.admin_probe_state === "probing" ? "CHECKING" : "PEER"}</span>`;

        const localBadge = n.is_admin_node ? `<span class="eyebrow-badge">ADMIN NODE</span>` : "";

        // Routing / Relay column
        const r = n.relay || {};
        let routeBadge = "";
        let viaHtml = "";
        if (r.link_type === "local") {
          routeBadge = `<span class="route-badge local">Paired via HTTP</span>`;
        } else if (r.link_type === "direct") {
          routeBadge = `<span class="route-badge direct">Direct RF &middot; 0 hops</span>`;
        } else if (r.link_type === "relayed") {
          const hopWord = r.hops_away === 1 ? "1 hop" : `${r.hops_away} hops`;
          routeBadge = `<span class="route-badge relayed">Relayed &middot; ${hopWord}</span>`;
          if (r.via_node) {
            viaHtml = `<div class="relay-via-line">&#8627; Relayed via <strong>${escapeHtml(r.via_node.name)}</strong> <code>${escapeHtml(r.via_node.node_id)}</code></div>`;
          } else if (r.relay_byte) {
            viaHtml = `<div class="relay-via-line">&#8627; Relay node byte <code>${escapeHtml(r.relay_byte)}</code> (run Trace for full path)</div>`;
          } else {
            viaHtml = `<div class="relay-via-line">&#8627; Multi-hop path (click Trace to inspect relay nodes)</div>`;
          }
        } else if (r.link_type === "mqtt") {
          routeBadge = `<span class="route-badge mqtt">Via MQTT${r.hops_away ? ` &middot; ${r.hops_away}h` : ""}</span>`;
        } else {
          routeBadge = `<span class="route-badge unknown">Hops unknown</span>`;
        }

        let relayingForHtml = "";
        if (r.relaying_for && r.relaying_for.length > 0) {
          const names = r.relaying_for.map((c) => `${escapeHtml(c.name)} (${escapeHtml(c.node_id)})`).join(", ");
          relayingForHtml = `<div class="relay-children-line">&#9741; Relaying for: <strong>${names}</strong></div>`;
        }

        let trBtnHtml = "";
        if (r.traceroute) {
          trBtnHtml = `<div style="margin-top:4px;">
            <button type="button" class="btn btn-ghost btn-xs js-view-trace" data-node-id="${escapeHtml(n.node_id)}">
              View Traced Route (${r.traceroute.relay_hops} relay hop${r.traceroute.relay_hops === 1 ? "" : "s"})
            </button>
          </div>`;
        }

        // Signal column
        const sig = n.signal || {};
        let sigHtml = "";
        if (n.is_admin_node) {
          sigHtml = `<span class="muted-sm">Local HTTP Link</span>`;
        } else if (sig.snr === null && sig.rssi === null) {
          sigHtml = `<span class="muted-sm">No RF packet yet</span>`;
        } else {
          const snrTxt = sig.snr !== null && sig.snr !== undefined ? `${sig.snr > 0 ? "+" : ""}${sig.snr} dB SNR` : "";
          const rssiTxt = sig.rssi !== null && sig.rssi !== undefined ? `${sig.rssi} dBm` : "";
          const sep = snrTxt && rssiTxt ? " &middot; " : "";
          sigHtml = `
            <div class="signal-wrap">
              <div class="sig-bars ${escapeHtml(sig.quality || "")}" title="Signal: ${escapeHtml(sig.quality || "")}">
                <span></span><span></span><span></span><span></span>
              </div>
              <div>
                <div class="sig-metrics">${snrTxt}${sep}${rssiTxt}</div>
                <div class="sig-sub">${sig.is_last_hop ? "Last relay link RF" : "Direct RF"}</div>
              </div>
            </div>`;
        }

        // Battery column
        const b = n.battery || {};
        let battHtml = "";
        if (b.is_powered) {
          battHtml = `<div><strong>Powered ⚡</strong>${b.voltage ? ` <span class="muted-sm">(${b.voltage}V)</span>` : ""}</div>`;
        } else if (b.level !== null && b.level !== undefined) {
          const fillCls = b.level <= 20 ? "low" : b.level <= 45 ? "warn" : "";
          battHtml = `
            <div class="batt-row">
              <div class="batt-bar"><div class="batt-fill ${fillCls}" style="width:${b.level}%"></div></div>
              <strong>${b.level}%</strong>
              ${b.voltage ? `<span class="muted-sm">${b.voltage}V</span>` : ""}
            </div>`;
        } else {
          battHtml = `<span class="muted-sm">&mdash;</span>`;
        }
        if (b.channel_utilization !== null && b.channel_utilization !== undefined) {
          battHtml += `<div class="sig-sub">Ch Util: ${b.channel_utilization}%</div>`;
        }

        // Last Message & Activity column
        let msgHtml = "";
        if (n.last_message && n.last_message.text) {
          msgHtml += `<div class="last-msg-bubble" title="${escapeHtml(n.last_message.text)}">
            &ldquo;${escapeHtml(n.last_message.text)}&rdquo;
            <span class="muted-sm">(${formatAgo(n.last_message.timestamp, nowSec)})</span>
          </div>`;
        } else {
          msgHtml += `<div class="sig-sub" style="margin-bottom:3px;">No text message yet</div>`;
        }
        msgHtml += `
          <div class="last-heard-row">
            <span>Heard: <strong>${formatAgo(n.last_heard, nowSec)}</strong></span>
            ${n.last_packet_type ? `<span class="pkt-tag">${escapeHtml(n.last_packet_type)}</span>` : ""}
          </div>`;

        // Tools column
        let toolsHtml = "";
        if (!n.is_admin_node) {
          toolsHtml = `
            <div style="display:flex; gap:5px; justify-content:flex-end; flex-wrap:wrap;">
              <button type="button" class="btn btn-secondary btn-xs js-run-trace" data-node-id="${escapeHtml(n.node_id)}" title="Run live multi-hop Traceroute">
                Trace
              </button>
              ${
                !n.has_admin
                  ? `<button type="button" class="btn btn-ghost btn-xs js-check-admin" data-node-id="${escapeHtml(n.node_id)}" title="Test whether the Admin Node has remote admin access to this node">
                       Verify Admin
                     </button>`
                  : ""
              }
            </div>`;
        } else {
          toolsHtml = `
            <button type="button" class="btn btn-secondary btn-xs js-open-admin" data-node-id="${escapeHtml(n.node_id)}">
              Configure
            </button>`;
        }

        return `
          <tr>
            <td class="col-icon">${iconCell}</td>
            <td>
              <div class="node-cell-title">
                <span>${escapeHtml(n.long_name)}</span>
                <span class="short-pill">${escapeHtml(n.short_name)}</span>
                ${localBadge}
              </div>
              <div class="node-cell-sub">
                <code>${escapeHtml(n.node_id)}</code>
                <span>${escapeHtml(n.hw_model)}</span>
                <span>Role: ${escapeHtml(n.role)}</span>
              </div>
            </td>
            <td>
              ${routeBadge}
              ${viaHtml}
              ${relayingForHtml}
              ${trBtnHtml}
            </td>
            <td>${sigHtml}</td>
            <td>${battHtml}</td>
            <td>${msgHtml}</td>
            <td class="col-actions">${toolsHtml}</td>
          </tr>`;
      })
      .join("");
  }

  function renderMessagesPanel(messages, nodes, nowSec) {
    // Keep destination dropdown synced without losing current choice
    const prevVal = el.msgDestSelect.value || "^all";
    const opts = [`<option value="^all">Broadcast (^all)</option>`];
    for (const n of nodes) {
      if (!n.is_admin_node) {
        opts.push(`<option value="${escapeHtml(n.node_id)}">${escapeHtml(n.long_name)} (${escapeHtml(n.node_id)})</option>`);
      }
    }
    el.msgDestSelect.innerHTML = opts.join("");
    if ([...el.msgDestSelect.options].some((o) => o.value === prevVal)) {
      el.msgDestSelect.value = prevVal;
    }

    if (!messages.length) {
      el.messagesList.innerHTML = `<div class="muted-sm" style="padding:12px; text-align:center;">No text messages received in this session yet.</div>`;
      return;
    }

    el.messagesList.innerHTML = messages
      .map(
        (m) => `
        <div class="msg-item">
          <div class="msg-item-meta">
            <strong>${escapeHtml(m.from_name || m.from_id)}</strong>
            <code>(${escapeHtml(m.from_id)})</code> &rarr;
            <span>${escapeHtml(m.to_name || m.to_id)}</span>
            &middot; Ch ${m.channel} &middot; ${formatAgo(m.timestamp, nowSec)}
          </div>
          <div>${escapeHtml(m.text)}</div>
        </div>`
      )
      .join("");
  }

  // ------------------------------------------------------------------
  // Full Node Configuration Popup Modal
  // ------------------------------------------------------------------
  async function openConfigModal(nodeId) {
    await loadSchema();
    const node = (state.snapshot?.nodes || []).find((n) => n.node_id === nodeId);
    state.modal.open = true;
    state.modal.nodeId = nodeId;
    state.modal.channelIndex = 0;

    el.modalNodeTitle.textContent = node ? node.long_name : nodeId;
    el.modalNodeShort.textContent = node ? node.short_name : "";
    el.modalNodeId.textContent = nodeId;
    if (node) {
      const linkDesc = node.is_admin_node
        ? "Paired Local Admin Node (HTTP)"
        : node.relay?.link_type === "relayed"
        ? `Remote Node · Relayed (${node.relay.hops_away} hop${node.relay.hops_away === 1 ? "" : "s"}${node.relay.via_node ? ` via ${node.relay.via_node.name}` : ""})`
        : "Remote Node · Direct RF (0 hops)";
      el.modalNodeSubtitle.textContent = `${linkDesc} · Full Parameter Configuration`;
    } else {
      el.modalNodeSubtitle.textContent = "Full Parameter Configuration";
    }

    buildModalSidebar();
    el.configModal.classList.remove("hidden");
    await selectModalTab({ type: "owner", key: "owner" });
  }

  function closeConfigModal() {
    state.modal.open = false;
    state.modal.nodeId = null;
    el.configModal.classList.add("hidden");
  }

  function buildModalSidebar() {
    const sc = state.schema;
    const groups = [
      {
        label: "Identity",
        items: [{ type: "owner", key: "owner", title: "Owner & Name" }],
      },
      {
        label: "Radio Config",
        items: (sc.config || []).map((s) => ({ type: "config", key: s.key, title: s.title })),
      },
      {
        label: "Module Config",
        items: (sc.module || []).map((s) => ({ type: "module", key: s.key, title: s.title })),
      },
      {
        label: "Channels",
        items: [{ type: "channels", key: "channels", title: "Channels (0–7)" }],
      },
      {
        label: "Maintenance",
        items: [{ type: "actions", key: "actions", title: "Node Actions" }],
      },
    ];

    el.modalNav.innerHTML = groups
      .map(
        (g) => `
        <div class="nav-group-label">${escapeHtml(g.label)}</div>
        ${g.items
          .map(
            (item) => `
          <button type="button" class="nav-item-btn" data-tab-type="${item.type}" data-tab-key="${item.key}">
            <span>${escapeHtml(item.title)}</span>
          </button>`
          )
          .join("")}`
      )
      .join("");

    el.modalNav.querySelectorAll(".nav-item-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectModalTab({ type: btn.dataset.tabType, key: btn.dataset.tabKey });
      });
    });
  }

  function setSectionStatus(msg, isError = false) {
    if (!msg) {
      el.sectionStatus.classList.add("hidden");
      return;
    }
    el.sectionStatus.textContent = msg;
    el.sectionStatus.style.borderColor = isError ? "rgba(248,113,113,0.5)" : "rgba(56,189,248,0.32)";
    el.sectionStatus.style.color = isError ? "#fecaca" : "var(--accent)";
    el.sectionStatus.classList.remove("hidden");
  }

  async function selectModalTab(tab, forceRefresh = false) {
    state.modal.activeTab = tab;
    el.modalNav.querySelectorAll(".nav-item-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tabType === tab.type && b.dataset.tabKey === tab.key);
    });

    const nodeId = state.modal.nodeId;
    if (!nodeId) return;

    el.sectionWarning.classList.add("hidden");
    setSectionStatus(null);

    if (tab.type === "actions") {
      el.sectionTitle.textContent = "Node Actions & Maintenance";
      el.sectionDesc.textContent = "Execute administrative commands on this node (clock sync, traceroute, reboot, shutdown, NodeDB reset).";
      el.reloadSectionBtn.classList.add("hidden");
      el.saveSectionBtn.classList.add("hidden");
      renderNodeActionsPane(nodeId);
      return;
    }

    el.reloadSectionBtn.classList.remove("hidden");
    el.saveSectionBtn.classList.remove("hidden");

    const sectionMeta = getSectionSchema(tab.type, tab.key);
    el.sectionTitle.textContent = sectionMeta?.title || tab.key;
    el.sectionDesc.textContent = sectionMeta?.description || "";
    if (sectionMeta?.warning) {
      el.sectionWarning.textContent = sectionMeta.warning;
      el.sectionWarning.classList.remove("hidden");
    }

    el.sectionFormContainer.innerHTML = `<div class="muted-sm" style="padding:18px 0;">Loading current settings from ${escapeHtml(nodeId)}...</div>`;
    setSectionStatus(`Reading ${sectionMeta?.title || tab.key} from ${nodeId}...`);

    const q = forceRefresh ? "?refresh=1" : "";
    try {
      if (tab.type === "owner") {
        const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/owner${q}`);
        state.modal.currentValues = res.values;
        setSectionStatus(null);
        renderSchemaForm(sectionMeta.fields, res.values);
      } else if (tab.type === "config" || tab.type === "module") {
        const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/config/${tab.type}/${tab.key}${q}`);
        state.modal.currentValues = res.values;
        setSectionStatus(null);
        renderSchemaForm(sectionMeta.fields, res.values);
      } else if (tab.type === "channels") {
        await loadChannelIntoForm(nodeId, state.modal.channelIndex, forceRefresh);
      }
    } catch (err) {
      setSectionStatus(`Failed loading section: ${err.message}`, true);
      el.sectionFormContainer.innerHTML = `
        <div class="muted-sm">
          Could not load parameters from node. Click <strong>Reload from Node</strong> to retry.
        </div>`;
    }
  }

  function getSectionSchema(type, key) {
    if (!state.schema) return null;
    if (type === "owner") return state.schema.owner;
    if (type === "channels") return state.schema.channels;
    if (type === "config") return (state.schema.config || []).find((s) => s.key === key);
    if (type === "module") return (state.schema.module || []).find((s) => s.key === key);
    return null;
  }

  // ------------------------------------------------------------------
  // Dynamic Schema Form Renderer & Collector
  // ------------------------------------------------------------------
  function renderSchemaForm(fields, values) {
    el.sectionFormContainer.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "form-grid";
    for (const f of fields) {
      grid.appendChild(createFieldElement(f, values?.[f.name], ""));
    }
    el.sectionFormContainer.appendChild(grid);
  }

  function createFieldElement(field, value, prefix) {
    const path = prefix ? `${prefix}.${field.name}` : field.name;
    const card = document.createElement("div");
    card.className = "field-card";
    if (["message", "message_list", "bytes_list", "bitflags"].includes(field.type) || field.helper === "admin_key") {
      card.classList.add("full-span");
    }

    if (field.type === "bool") {
      card.innerHTML = `
        <div class="toggle-row">
          <label class="field-label" style="margin:0;" for="fld-${escapeHtml(path)}">
            ${escapeHtml(field.label)}
            <code class="field-name-code">${escapeHtml(field.name)}</code>
          </label>
          <input id="fld-${escapeHtml(path)}" class="toggle-switch" type="checkbox"
            data-path="${escapeHtml(path)}" data-ftype="bool" ${value ? "checked" : ""} />
        </div>`;
      return card;
    }

    const labelHtml = `
      <label class="field-label" for="fld-${escapeHtml(path)}">
        ${escapeHtml(field.label)}
        <code class="field-name-code">${escapeHtml(field.name)}</code>
      </label>`;

    if (field.type === "enum") {
      const opts = (field.options || [])
        .map((o) => `<option value="${o.value}" ${Number(value) === Number(o.value) ? "selected" : ""}>${escapeHtml(o.name)} (${o.value})</option>`)
        .join("");
      card.innerHTML = `${labelHtml}
        <select id="fld-${escapeHtml(path)}" class="field-select" data-path="${escapeHtml(path)}" data-ftype="enum">
          ${opts}
        </select>`;
      return card;
    }

    if (field.type === "bitflags") {
      const mask = Number(value || 0);
      const checks = (field.flags || [])
        .map(
          (fl) => `
          <label class="flag-check">
            <input type="checkbox" data-flag-bit="${fl.value}" ${(mask & fl.value) !== 0 ? "checked" : ""} />
            <span>${escapeHtml(fl.name)}</span>
          </label>`
        )
        .join("");
      card.innerHTML = `
        ${labelHtml}
        <input id="fld-${escapeHtml(path)}" class="field-input" type="number" min="0"
          data-path="${escapeHtml(path)}" data-ftype="uint" value="${mask}" />
        <div class="bitflags-grid">${checks}</div>`;
      const numInput = card.querySelector("input[type='number']");
      const boxes = card.querySelectorAll("input[data-flag-bit]");
      boxes.forEach((cb) => {
        cb.addEventListener("change", () => {
          let total = 0;
          boxes.forEach((b) => {
            if (b.checked) total |= Number(b.dataset.flagBit);
          });
          numInput.value = String(total);
        });
      });
      numInput.addEventListener("input", () => {
        const cur = Number(numInput.value || 0);
        boxes.forEach((b) => {
          b.checked = (cur & Number(b.dataset.flagBit)) !== 0;
        });
      });
      return card;
    }

    if (field.type === "bytes_list") {
      const items = Array.isArray(value) ? [...value] : [];
      card.innerHTML = `
        ${labelHtml}
        <div class="array-list" data-bytes-list="${escapeHtml(path)}"></div>
        <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap;">
          <button type="button" class="btn btn-secondary btn-xs js-add-key">+ Add Key Slot</button>
          ${
            field.helper === "admin_key" && state.snapshot?.admin_node?.public_key
              ? `<button type="button" class="btn btn-primary btn-xs js-insert-admin-pk">
                   + Insert Paired Admin Node Public Key
                 </button>`
              : ""
          }
        </div>`;
      const listEl = card.querySelector(".array-list");
      const addRow = (val = "") => {
        if (field.maxItems && listEl.children.length >= field.maxItems) {
          showToast(`Maximum ${field.maxItems} keys supported by firmware`, "err");
          return;
        }
        const row = document.createElement("div");
        row.className = "array-row";
        row.innerHTML = `
          <input type="text" class="field-input mono-id" data-bytes-item="${escapeHtml(path)}"
            placeholder="Base64 32-byte public key" value="${escapeHtml(val)}" />
          <button type="button" class="btn btn-ghost btn-xs">Remove</button>`;
        row.querySelector("button").onclick = () => row.remove();
        listEl.appendChild(row);
      };
      items.forEach((it) => addRow(it));
      card.querySelector(".js-add-key").onclick = () => addRow("");
      const insertBtn = card.querySelector(".js-insert-admin-pk");
      if (insertBtn) {
        insertBtn.onclick = () => {
          const pk = state.snapshot?.admin_node?.public_key || "";
          const existing = [...listEl.querySelectorAll("input")].map((i) => i.value.trim());
          if (existing.includes(pk)) {
            showToast("Admin Node public key is already in admin_key list");
            return;
          }
          addRow(pk);
        };
      }
      return card;
    }

    if (field.type === "nodenum_list") {
      const strVal = Array.isArray(value) ? value.join(", ") : String(value || "");
      card.innerHTML = `${labelHtml}
        <input id="fld-${escapeHtml(path)}" class="field-input mono-id" type="text"
          placeholder="${escapeHtml(field.placeholder || "!12345678, !87654321")}"
          data-path="${escapeHtml(path)}" data-ftype="nodenum_list" value="${escapeHtml(strVal)}" />`;
      return card;
    }

    if (field.type === "message") {
      card.innerHTML = `${labelHtml}<div class="submessage-box form-grid"></div>`;
      const box = card.querySelector(".submessage-box");
      for (const sf of field.fields || []) {
        box.appendChild(createFieldElement(sf, value?.[sf.name], path));
      }
      return card;
    }

    if (field.type === "message_list") {
      const arr = Array.isArray(value) ? value : [];
      card.innerHTML = `
        ${labelHtml}
        <div class="array-list" data-msg-list="${escapeHtml(path)}"></div>
        <button type="button" class="btn btn-secondary btn-xs" style="margin-top:8px;">+ Add Entry</button>`;
      const listEl = card.querySelector(".array-list");
      const addMsgItem = (itemVal = {}) => {
        const idx = listEl.children.length;
        const wrap = document.createElement("div");
        wrap.className = "submessage-box";
        wrap.dataset.msgListItem = path;
        const innerGrid = document.createElement("div");
        innerGrid.className = "form-grid";
        for (const sf of field.fields || []) {
          innerGrid.appendChild(createFieldElement(sf, itemVal?.[sf.name], `${path}[${idx}]`));
        }
        const rmBtn = document.createElement("button");
        rmBtn.type = "button";
        rmBtn.className = "btn btn-ghost btn-xs";
        rmBtn.style.marginTop = "8px";
        rmBtn.textContent = "Remove Entry";
        rmBtn.onclick = () => wrap.remove();
        wrap.appendChild(innerGrid);
        wrap.appendChild(rmBtn);
        listEl.appendChild(wrap);
      };
      arr.forEach((it) => addMsgItem(it));
      card.querySelector("button").onclick = () => addMsgItem({});
      return card;
    }

    if (field.type === "password") {
      card.innerHTML = `
        ${labelHtml}
        <div class="array-row">
          <input id="fld-${escapeHtml(path)}" class="field-input" type="password"
            data-path="${escapeHtml(path)}" data-ftype="string" value="${escapeHtml(value ?? "")}" />
          <button type="button" class="btn btn-ghost btn-xs">Show</button>
        </div>`;
      const inp = card.querySelector("input");
      const toggleBtn = card.querySelector("button");
      toggleBtn.onclick = () => {
        const show = inp.type === "password";
        inp.type = show ? "text" : "password";
        toggleBtn.textContent = show ? "Hide" : "Show";
      };
      return card;
    }

    if (field.type === "bytes") {
      card.innerHTML = `
        ${labelHtml}
        <div class="array-row">
          <input id="fld-${escapeHtml(path)}" class="field-input mono-id" type="text"
            ${field.readOnly ? "readonly" : ""}
            placeholder="Base64 bytes"
            data-path="${escapeHtml(path)}" data-ftype="bytes" data-readonly="${field.readOnly ? "1" : "0"}"
            value="${escapeHtml(value ?? "")}" />
          ${value ? `<button type="button" class="btn btn-ghost btn-xs js-copy-val">Copy</button>` : ""}
        </div>`;
      const cp = card.querySelector(".js-copy-val");
      if (cp) {
        cp.onclick = () => {
          navigator.clipboard.writeText(String(value || ""));
          showToast("Copied key to clipboard");
        };
      }
      return card;
    }

    const inputType = ["int", "uint", "float"].includes(field.type) ? "number" : "text";
    const stepAttr = field.type === "float" ? 'step="any"' : field.type === "uint" ? 'min="0" step="1"' : 'step="1"';
    const maxLenAttr = field.maxLength ? `maxlength="${field.maxLength}"` : "";
    card.innerHTML = `
      ${labelHtml}
      <input id="fld-${escapeHtml(path)}" class="field-input" type="${inputType}"
        ${ inputType === "number" ? stepAttr : "" }
        ${maxLenAttr}
        placeholder="${escapeHtml(field.placeholder || "")}"
        data-path="${escapeHtml(path)}" data-ftype="${escapeHtml(field.type)}"
        value="${escapeHtml(value ?? "")}" />`;
    return card;
  }

  function collectSchemaValues(fields, prefix = "") {
    const out = {};
    for (const f of fields) {
      const path = prefix ? `${prefix}.${f.name}` : f.name;
      if (f.readOnly) continue;

      if (f.type === "message") {
        out[f.name] = collectSchemaValues(f.fields || [], path);
        continue;
      }

      if (f.type === "message_list") {
        const listEl = el.sectionFormContainer.querySelector(`[data-msg-list="${CSS.escape(path)}"]`);
        const items = [];
        if (listEl) {
          [...listEl.children].forEach((wrap) => {
            const itemObj = {};
            for (const sf of f.fields || []) {
              const input = wrap.querySelector(`[data-path$=".${CSS.escape(sf.name)}"]`);
              if (input) {
                itemObj[sf.name] = readSingleInput(input);
              }
            }
            items.push(itemObj);
          });
        }
        out[f.name] = items;
        continue;
      }

      if (f.type === "bytes_list") {
        const inputs = el.sectionFormContainer.querySelectorAll(`[data-bytes-item="${CSS.escape(path)}"]`);
        out[f.name] = [...inputs].map((i) => i.value.trim()).filter(Boolean);
        continue;
      }

      const input = el.sectionFormContainer.querySelector(`[data-path="${CSS.escape(path)}"]`);
      if (!input) continue;
      out[f.name] = readSingleInput(input);
    }
    return out;
  }

  function readSingleInput(input) {
    const ftype = input.dataset.ftype;
    if (ftype === "bool") return Boolean(input.checked);
    if (ftype === "enum" || ftype === "int" || ftype === "uint") {
      return input.value === "" ? 0 : parseInt(input.value, 10);
    }
    if (ftype === "float") {
      return input.value === "" ? 0.0 : parseFloat(input.value);
    }
    if (ftype === "nodenum_list") {
      return input.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }
    return input.value;
  }

  // ------------------------------------------------------------------
  // Channels (0..7) Editor inside Modal
  // ------------------------------------------------------------------
  async function loadChannelIntoForm(nodeId, index, forceRefresh = false) {
    state.modal.channelIndex = index;
    setSectionStatus(`Reading Channel ${index} from ${nodeId}...`);
    const q = forceRefresh ? "?refresh=1" : "";
    const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/channel/${index}${q}`);
    const ch = res.channel;
    state.modal.currentValues = ch;
    setSectionStatus(null);

    const chSchema = state.schema.channels;
    const tabsHtml = Array.from({ length: 8 }, (_, i) => {
      const label = i === 0 ? "Ch 0 (Primary)" : `Ch ${i}`;
      return `<button type="button" class="ch-tab-btn ${i === index ? "active" : ""}" data-ch-idx="${i}">${label}</button>`;
    }).join("");

    const roleOpts = (chSchema.roles || [])
      .filter((r) => !(index === 0 && r.name !== "PRIMARY"))
      .map((r) => `<option value="${r.value}" ${Number(ch.role) === Number(r.value) ? "selected" : ""}>${escapeHtml(r.name)}</option>`)
      .join("");

    el.sectionFormContainer.innerHTML = `
      <div class="channel-tabs">${tabsHtml}</div>
      <div class="form-grid" id="ch-custom-grid">
        <div class="field-card">
          <label class="field-label">Channel Role <code class="field-name-code">role</code></label>
          <select id="ch-role-select" class="field-select">${roleOpts}</select>
        </div>
        <div class="field-card full-span">
          <label class="field-label">Encryption Key (PSK) <code class="field-name-code">psk</code></label>
          <div class="array-row" style="flex-wrap:wrap;">
            <input id="ch-psk-input" type="text" class="field-input mono-id" style="flex:1; min-width:240px;"
              value="${escapeHtml(ch.psk_text || "default")}"
              placeholder="default | none | random256 | base64:..." />
            <button type="button" class="btn btn-secondary btn-xs" data-psk-preset="default">Default Key</button>
            <button type="button" class="btn btn-secondary btn-xs" data-psk-preset="none">No Encryption</button>
            <button type="button" class="btn btn-primary btn-xs" data-psk-preset="random256">Random 256-bit</button>
          </div>
          <p class="sig-sub" style="margin:5px 0 0;">Use <code>default</code> (standard Aq== key), <code>none</code>, <code>random256</code>, or a <code>base64:...</code> key.</p>
        </div>
      </div>`;

    el.sectionFormContainer.querySelectorAll(".ch-tab-btn").forEach((btn) => {
      btn.onclick = () => loadChannelIntoForm(nodeId, Number(btn.dataset.chIdx), false);
    });
    el.sectionFormContainer.querySelectorAll("[data-psk-preset]").forEach((btn) => {
      btn.onclick = () => {
        document.getElementById("ch-psk-input").value = btn.dataset.pskPreset;
      };
    });

    const grid = document.getElementById("ch-custom-grid");
    for (const sf of chSchema.fields || []) {
      if (sf.name === "psk") continue; // Handled by friendly PSK editor above
      grid.appendChild(createFieldElement(sf, ch.settings?.[sf.name], "ch_settings"));
    }
  }

  // ------------------------------------------------------------------
  // Node Maintenance Actions Pane inside Modal
  // ------------------------------------------------------------------
  function renderNodeActionsPane(nodeId) {
    const isLocal = state.snapshot?.admin_node?.node_id === nodeId;
    el.sectionFormContainer.innerHTML = `
      <div class="actions-grid">
        <div class="action-box">
          <div>
            <h4>Sync Node Clock</h4>
            <p class="muted-sm">Set this node's RTC clock to your computer's current UTC timestamp.</p>
          </div>
          <button type="button" class="btn btn-secondary btn-sm" data-node-act="sync_time">Sync Clock Now</button>
        </div>
        ${
          !isLocal
            ? `<div class="action-box">
                 <div>
                   <h4>Live Traceroute</h4>
                   <p class="muted-sm">Discover the exact forward &amp; return multi-hop relay route and per-hop SNR.</p>
                 </div>
                 <button type="button" class="btn btn-secondary btn-sm" data-node-act="traceroute">Run Traceroute</button>
               </div>`
            : ""
        }
        <div class="action-box">
          <div>
            <h4>Clear Fixed Position</h4>
            <p class="muted-sm">Remove stored fixed GPS coordinates and disable fixed_position on this node.</p>
          </div>
          <button type="button" class="btn btn-secondary btn-sm" data-node-act="remove_fixed_position">Remove Fixed Position</button>
        </div>
        <div class="action-box">
          <div>
            <h4>Reboot Node</h4>
            <p class="muted-sm">Gracefully restart the radio firmware in 5 seconds.</p>
          </div>
          <button type="button" class="btn btn-danger btn-sm" data-node-act="reboot">Reboot Node</button>
        </div>
        <div class="action-box">
          <div>
            <h4>Reset NodeDB</h4>
            <p class="muted-sm">Clear cached peer nodes from this device's NodeDB (preserves radio &amp; channel settings).</p>
          </div>
          <button type="button" class="btn btn-danger btn-sm" data-node-act="reset_nodedb">Reset NodeDB</button>
        </div>
        <div class="action-box">
          <div>
            <h4>Shutdown Node</h4>
            <p class="muted-sm">Power down the radio hardware in 5 seconds.</p>
          </div>
          <button type="button" class="btn btn-danger btn-sm" data-node-act="shutdown">Shutdown Node</button>
        </div>
      </div>`;

    el.sectionFormContainer.querySelectorAll("[data-node-act]").forEach((btn) => {
      btn.onclick = async () => {
        const act = btn.dataset.nodeAct;
        if (["reboot", "reset_nodedb", "shutdown"].includes(act)) {
          if (!confirm(`Send '${act}' command to ${nodeId}?`)) return;
        }
        btn.disabled = true;
        setSectionStatus(`Sending '${act}' command to ${nodeId}...`);
        try {
          const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/action`, {
            method: "POST",
            body: JSON.stringify({ action: act }),
          });
          setSectionStatus(`Action '${act}' succeeded on ${nodeId}.`);
          showToast(`Action '${act}' completed`);
          if (act === "traceroute" && res.traceroute) {
            showTracerouteModal(nodeId, res.traceroute);
          }
          await refreshState();
        } catch (err) {
          setSectionStatus(`Action failed: ${err.message}`, true);
          showToast(err.message, "err");
        } finally {
          btn.disabled = false;
        }
      };
    });
  }

  // ------------------------------------------------------------------
  // Save Active Section Handler
  // ------------------------------------------------------------------
  async function saveActiveSection() {
    const { nodeId, activeTab, channelIndex } = state.modal;
    if (!nodeId || !activeTab) return;

    el.saveSectionBtn.disabled = true;
    setSectionStatus(`Writing settings to ${nodeId} (acquiring session passkey & committing)...`);

    try {
      if (activeTab.type === "owner") {
        const meta = getSectionSchema("owner", "owner");
        const values = collectSchemaValues(meta.fields);
        const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/owner`, {
          method: "POST",
          body: JSON.stringify({ values }),
        });
        state.modal.currentValues = res.values;
        el.modalNodeTitle.textContent = res.values.long_name;
        el.modalNodeShort.textContent = res.values.short_name;
        setSectionStatus("Identity (Owner) saved to node!");
        showToast(`Saved Owner settings on ${nodeId}`);
      } else if (activeTab.type === "config" || activeTab.type === "module") {
        const meta = getSectionSchema(activeTab.type, activeTab.key);
        const values = collectSchemaValues(meta.fields);
        const res = await apiJson(
          `/api/node/${encodeURIComponent(nodeId)}/config/${activeTab.type}/${activeTab.key}`,
          {
            method: "POST",
            body: JSON.stringify({ values }),
          }
        );
        state.modal.currentValues = res.values;
        setSectionStatus(`${meta.title} saved and committed on ${nodeId}!`);
        showToast(`Saved ${meta.title} on ${nodeId}`);
      } else if (activeTab.type === "channels") {
        const chSchema = state.schema.channels;
        const fieldsNoPsk = (chSchema.fields || []).filter((f) => f.name !== "psk");
        const settingsVals = collectSchemaValues(fieldsNoPsk, "ch_settings");
        const payload = {
          role: parseInt(document.getElementById("ch-role-select").value, 10),
          psk_text: document.getElementById("ch-psk-input").value.trim(),
          settings: settingsVals,
        };
        const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/channel/${channelIndex}`, {
          method: "POST",
          body: JSON.stringify({ channel: payload }),
        });
        document.getElementById("ch-psk-input").value = res.channel.psk_text;
        setSectionStatus(`Channel ${channelIndex} saved and committed on ${nodeId}!`);
        showToast(`Saved Channel ${channelIndex} on ${nodeId}`);
      }
      await refreshState();
    } catch (err) {
      setSectionStatus(`Save failed: ${err.message}`, true);
      showToast(err.message, "err");
    } finally {
      el.saveSectionBtn.disabled = false;
    }
  }

  // ------------------------------------------------------------------
  // Traceroute Popup
  // ------------------------------------------------------------------
  function showTracerouteModal(nodeId, tr) {
    el.traceModalTitle.textContent = `Traceroute — ${nodeId}`;
    const renderChain = (hops) =>
      (hops || [])
        .map(
          (h, idx) => `
          <div class="hop-step">
            <span><strong>#${idx}</strong> ${escapeHtml(h.name)} <code>${escapeHtml(h.node_id)}</code></span>
            <span>${idx === 0 ? "Origin" : h.snr !== null && h.snr !== undefined ? `${h.snr > 0 ? "+" : ""}${h.snr} dB SNR` : "? dB"}</span>
          </div>`
        )
        .join("");

    el.traceModalBody.innerHTML = `
      <h4 style="margin:0 0 6px;">Route Towards Node (${tr.relay_hops} relay hop${tr.relay_hops === 1 ? "" : "s"})</h4>
      <div class="hop-chain">${renderChain(tr.forward)}</div>
      ${
        tr.back && tr.back.length
          ? `<h4 style="margin:12px 0 6px;">Return Route Back to Admin Node</h4>
             <div class="hop-chain">${renderChain(tr.back)}</div>`
          : ""
      }`;
    el.traceModal.classList.remove("hidden");
  }

  // ------------------------------------------------------------------
  // Event Listeners & Initialization
  // ------------------------------------------------------------------
  el.pairForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const target = el.targetInput.value.trim() || "http://meshtastic.local";
    el.pairBtn.disabled = true;
    el.pairBtn.textContent = "Pairing...";
    try {
      const snap = await apiJson("/api/connect", {
        method: "POST",
        body: JSON.stringify({ target }),
      });
      state.snapshot = snap;
      renderSnapshot(snap);
      showToast(`Paired to Admin Node at ${snap.connection.target}`);
    } catch (err) {
      showToast(err.message, "err");
      el.alertBanner.textContent = err.message;
      el.alertBanner.classList.remove("hidden");
    } finally {
      el.pairBtn.disabled = false;
      await refreshState();
    }
  });

  el.disconnectBtn.addEventListener("click", async () => {
    await apiJson("/api/disconnect", { method: "POST" });
    showToast("Disconnected from Admin Node");
    await refreshState();
  });

  el.copyPubkeyBtn.addEventListener("click", () => {
    const pk = state.snapshot?.admin_node?.public_key;
    if (!pk) {
      showToast("Admin Node public key not available", "err");
      return;
    }
    navigator.clipboard.writeText(pk);
    showToast("Copied Admin Node public key to clipboard");
  });

  el.probeAllBtn.addEventListener("click", async () => {
    try {
      const res = await apiJson("/api/probe_admin", {
        method: "POST",
        body: JSON.stringify({ include_stale: false }),
      });
      showToast(`Queued Admin Power check for ${res.queued} mesh node(s)`);
      await refreshState();
    } catch (err) {
      showToast(err.message, "err");
    }
  });

  el.toggleMessagesBtn.addEventListener("click", () => {
    el.messagesPanel.classList.toggle("hidden");
  });
  el.closeMessagesBtn.addEventListener("click", () => {
    el.messagesPanel.classList.add("hidden");
  });

  el.sendMsgForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = el.msgTextInput.value.trim();
    if (!text) return;
    try {
      await apiJson("/api/messages/send", {
        method: "POST",
        body: JSON.stringify({
          text,
          destination_id: el.msgDestSelect.value,
          channel_index: Number(el.msgChSelect.value),
        }),
      });
      el.msgTextInput.value = "";
      showToast("Message transmitted to mesh");
      await refreshState();
    } catch (err) {
      showToast(err.message, "err");
    }
  });

  document.querySelectorAll(".filter-pills .pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-pills .pill").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filter = btn.dataset.filter;
      if (state.snapshot) renderSnapshot(state.snapshot);
    });
  });

  el.nodeSearch.addEventListener("input", () => {
    state.search = el.nodeSearch.value;
    if (state.snapshot) renderSnapshot(state.snapshot);
  });

  if (el.nodeSortBy) {
    el.nodeSortBy.addEventListener("change", () => {
      state.sortBy = el.nodeSortBy.value;
      // Default to ascending (Lowest Battery first / A->Z first) when choosing a field
      state.sortDir = "asc";
      syncSortControlsUI();
      if (state.snapshot) renderSnapshot(state.snapshot);
    });
  }

  if (el.nodeSortDir) {
    el.nodeSortDir.addEventListener("click", () => {
      if (state.sortBy === "default") return;
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      syncSortControlsUI();
      if (state.snapshot) renderSnapshot(state.snapshot);
    });
  }

  document.querySelectorAll(".nodes-table th.sortable-th").forEach((th) => {
    th.addEventListener("click", () => {
      const targetSort = th.dataset.sort;
      if (!targetSort) return;
      if (state.sortBy === targetSort) {
        state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      } else {
        state.sortBy = targetSort;
        state.sortDir = "asc";
      }
      syncSortControlsUI();
      if (state.snapshot) renderSnapshot(state.snapshot);
    });
  });

  // Delegated clicks inside Nodes Table
  el.nodesTbody.addEventListener("click", async (e) => {
    const adminBtn = e.target.closest(".js-open-admin");
    if (adminBtn) {
      openConfigModal(adminBtn.dataset.nodeId);
      return;
    }

    const checkBtn = e.target.closest(".js-check-admin");
    if (checkBtn) {
      const nodeId = checkBtn.dataset.nodeId;
      checkBtn.disabled = true;
      checkBtn.textContent = "Probing...";
      try {
        const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/action`, {
          method: "POST",
          body: JSON.stringify({ action: "probe_admin" }),
        });
        if (res.has_admin) {
          showToast(`Admin Power confirmed on ${nodeId}! Click the gold shield icon to configure.`);
        } else {
          showToast(`No Admin Power on ${nodeId} (${res.status})`, "err");
        }
        await refreshState();
      } catch (err) {
        showToast(err.message, "err");
      } finally {
        checkBtn.disabled = false;
        checkBtn.textContent = "Verify Admin";
      }
      return;
    }

    const traceBtn = e.target.closest(".js-run-trace");
    if (traceBtn) {
      const nodeId = traceBtn.dataset.nodeId;
      traceBtn.disabled = true;
      traceBtn.textContent = "Tracing...";
      try {
        const res = await apiJson(`/api/node/${encodeURIComponent(nodeId)}/action`, {
          method: "POST",
          body: JSON.stringify({ action: "traceroute" }),
        });
        await refreshState();
        if (res.traceroute) showTracerouteModal(nodeId, res.traceroute);
      } catch (err) {
        showToast(err.message, "err");
      } finally {
        traceBtn.disabled = false;
        traceBtn.textContent = "Trace";
      }
      return;
    }

    const viewTraceBtn = e.target.closest(".js-view-trace");
    if (viewTraceBtn) {
      const nodeId = viewTraceBtn.dataset.nodeId;
      const node = (state.snapshot?.nodes || []).find((n) => n.node_id === nodeId);
      if (node?.relay?.traceroute) {
        showTracerouteModal(nodeId, node.relay.traceroute);
      }
    }
  });

  // ------------------------------------------------------------------
  // USB Node Admin Key Enrollment Modal
  // ------------------------------------------------------------------
  function setUsbStatus(msg, isErr = false) {
    if (!msg) {
      el.usbStatusBanner.classList.add("hidden");
      return;
    }
    el.usbStatusBanner.textContent = msg;
    el.usbStatusBanner.style.borderColor = isErr ? "rgba(248,113,113,0.5)" : "rgba(56,189,248,0.32)";
    el.usbStatusBanner.style.color = isErr ? "#fecaca" : "var(--accent)";
    el.usbStatusBanner.classList.remove("hidden");
  }

  async function refreshUsbPorts(autoInspect = false) {
    setUsbStatus("Scanning connected USB serial ports...");
    el.usbNodeDetails.classList.add("hidden");
    el.usbEnrollFooter.classList.add("hidden");
    try {
      const res = await apiJson("/api/usb/ports");
      const ports = res.ports || [];
      if (!ports.length) {
        el.usbPortSelect.innerHTML = `<option value="">No serial ports detected</option>`;
        setUsbStatus("No USB serial ports found. Plug a Meshtastic node into USB and click Refresh Ports.", true);
        return;
      }
      el.usbPortSelect.innerHTML = ports
        .map((p) => {
          const tag = p.auto_detected
            ? " [Meshtastic Detected]"
            : p.is_meshtastic_candidate
            ? " [USB Serial]"
            : p.is_bluetooth
            ? " [Bluetooth]"
            : "";
          return `<option value="${escapeHtml(p.device)}">${escapeHtml(p.device)} — ${escapeHtml(p.description)}${tag}</option>`;
        })
        .join("");

      const best = ports.find((p) => p.auto_detected) || ports.find((p) => p.is_meshtastic_candidate);
      if (best) {
        el.usbPortSelect.value = best.device;
      }
      setUsbStatus(null);
      if (autoInspect && best) {
        await inspectSelectedUsbNode();
      }
    } catch (err) {
      setUsbStatus(`Failed listing USB ports: ${err.message}`, true);
    }
  }

  async function inspectSelectedUsbNode() {
    const port = el.usbPortSelect.value;
    if (!port) {
      setUsbStatus("Please select a USB port first.", true);
      return;
    }
    el.usbInspectBtn.disabled = true;
    setUsbStatus(`Reading node identity & security.admin_key slots on ${port}...`);
    el.usbNodeDetails.classList.add("hidden");
    el.usbEnrollFooter.classList.add("hidden");

    try {
      const res = await apiJson("/api/usb/inspect", {
        method: "POST",
        body: JSON.stringify({ port }),
      });
      renderUsbNodeDetails(res.usb_node);
      setUsbStatus(null);
    } catch (err) {
      setUsbStatus(`USB inspect failed on ${port}: ${err.message}`, true);
    } finally {
      el.usbInspectBtn.disabled = false;
    }
  }

  function renderUsbNodeDetails(info) {
    let preCheckedCount = 0;
    const slotsHtml = (info.admin_keys || []).length
      ? info.admin_keys
          .map((s) => {
            if (s.matches_paired_admin) {
              return `
                <div class="hop-step" style="border-color: var(--green);">
                  <span><strong>Slot ${s.slot}:</strong> <code>${escapeHtml(s.key)}</code></span>
                  <span class="route-badge direct">Paired Admin Node ✓</span>
                </div>`;
            }
            const shouldKeep = Boolean(s.matched_node) && preCheckedCount < 2;
            if (shouldKeep) preCheckedCount += 1;
            const ownerLabel = s.matched_node
              ? `<span class="route-badge relayed">Matches ${escapeHtml(s.matched_node.name)} (${escapeHtml(s.matched_node.node_id)})</span>`
              : `<span class="route-badge unknown">Stale / Unknown Key</span>`;
            return `
              <label class="hop-step" style="cursor:pointer; align-items:center; gap:10px;">
                <span style="display:flex; align-items:center; gap:8px; overflow:hidden;">
                  <input type="checkbox" class="js-keep-usb-key" value="${escapeHtml(s.key)}" ${shouldKeep ? "checked" : ""} />
                  <span><strong>Keep Slot ${s.slot}:</strong> <code style="font-size:0.75rem;">${escapeHtml(s.key)}</code></span>
                </span>
                ${ownerLabel}
              </label>`;
          })
          .join("")
      : `<div class="muted-sm">No existing admin_key entries on this node (0 / 3 slots used).</div>`;

    const sameNodeWarn = info.is_paired_admin_node
      ? `<div class="section-warning" style="margin-bottom:10px;">
           Warning: ${escapeHtml(info.port)} is your paired Admin Node itself (${escapeHtml(info.node_id)}). Switch to the target radio's COM port!
         </div>`
      : "";

    const alreadyBadge = info.already_has_paired_admin_key
      ? `<span class="route-badge direct">Admin Key Already Enrolled</span>`
      : info.slots_used >= 3
      ? `<span class="route-badge local">3 / 3 Slots Full — Unchecked Stale Keys Will Be Replaced</span>`
      : `<span class="route-badge relayed">${info.slots_used} / 3 Slots Used</span>`;

    el.usbNodeDetails.innerHTML = `
      ${sameNodeWarn}
      <div style="display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:8px; margin-bottom:10px;">
        <div class="node-cell-title">
          <strong>2. Connected Radio: ${escapeHtml(info.long_name)}</strong>
          <span class="short-pill">${escapeHtml(info.short_name)}</span>
          <code>${escapeHtml(info.node_id)}</code>
          <span class="muted-sm">(${escapeHtml(info.hw_model)}${info.firmware_version ? ` · FW ${escapeHtml(info.firmware_version)}` : ""})</span>
        </div>
        ${alreadyBadge}
      </div>
      <p class="muted-sm" style="margin-bottom:8px;">
        Firmware allows at most <strong>3</strong> admin keys. Your Paired Admin Node key (<code>${escapeHtml(info.paired_admin_node_id || "")}</code>) will be written to Slot 1, plus up to <strong>2</strong> checked keys below:
      </p>
      <div class="hop-chain" style="margin-bottom:4px;">${slotsHtml}</div>`;

    // Enforce max 2 kept keys in checkboxes
    const boxes = el.usbNodeDetails.querySelectorAll(".js-keep-usb-key");
    boxes.forEach((cb) => {
      cb.addEventListener("change", () => {
        const checked = [...boxes].filter((b) => b.checked);
        if (checked.length > 2) {
          cb.checked = false;
          showToast("You can keep at most 2 existing keys alongside the Paired Admin Node key (3 max)", "err");
        }
      });
    });

    el.usbNodeDetails.classList.remove("hidden");
    el.usbEnrollFooter.classList.toggle("hidden", Boolean(info.is_paired_admin_node));
  }

  async function enrollSelectedUsbNode() {
    const port = el.usbPortSelect.value;
    if (!port) return;
    const keepKeys = [...el.usbNodeDetails.querySelectorAll(".js-keep-usb-key:checked")].map((cb) => cb.value);

    el.usbWriteKeyBtn.disabled = true;
    setUsbStatus(`Writing Admin Key to ${port} (acquiring session passkey & committing to flash)...`);
    try {
      const res = await apiJson("/api/usb/enroll", {
        method: "POST",
        body: JSON.stringify({ port, keep_keys: keepKeys }),
      });
      const enr = res.enrolled;
      setUsbStatus(
        `Saved Admin Key onto ${enr.long_name} (${enr.node_id}) [${enr.saved_keys.length}/3 slots used]! Node is rebooting (~5–10s) — you can unplug it or plug in the next node.`
      );
      showToast(`Enrolled ${enr.long_name} (${enr.node_id}) for Remote Admin!`);
      await refreshState();
    } catch (err) {
      setUsbStatus(`USB enrollment failed: ${err.message}`, true);
      showToast(err.message, "err");
    } finally {
      el.usbWriteKeyBtn.disabled = false;
    }
  }

  function openUsbModal() {
    el.usbModal.classList.remove("hidden");
    refreshUsbPorts(true);
  }

  // Modal controls
  el.openUsbEnrollBtn.addEventListener("click", openUsbModal);
  el.usbCloseBtn.addEventListener("click", () => el.usbModal.classList.add("hidden"));
  el.usbModal.addEventListener("click", (e) => {
    if (e.target === el.usbModal) el.usbModal.classList.add("hidden");
  });
  el.usbRefreshPortsBtn.addEventListener("click", () => refreshUsbPorts(false));
  el.usbInspectBtn.addEventListener("click", inspectSelectedUsbNode);
  el.usbPortSelect.addEventListener("change", inspectSelectedUsbNode);
  el.usbWriteKeyBtn.addEventListener("click", enrollSelectedUsbNode);

  el.modalCloseBtn.addEventListener("click", closeConfigModal);
  el.configModal.addEventListener("click", (e) => {
    if (e.target === el.configModal) closeConfigModal();
  });
  el.reloadSectionBtn.addEventListener("click", () => {
    if (state.modal.activeTab) selectModalTab(state.modal.activeTab, true);
  });
  el.saveSectionBtn.addEventListener("click", saveActiveSection);

  el.traceCloseBtn.addEventListener("click", () => el.traceModal.classList.add("hidden"));
  el.traceModal.addEventListener("click", (e) => {
    if (e.target === el.traceModal) el.traceModal.classList.add("hidden");
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!el.usbModal.classList.contains("hidden")) {
        el.usbModal.classList.add("hidden");
      } else if (!el.traceModal.classList.contains("hidden")) {
        el.traceModal.classList.add("hidden");
      } else if (state.modal.open) {
        closeConfigModal();
      }
    }
  });

  // Boot
  (async () => {
    await loadSchema().catch(() => {});
    await refreshState();
    state.pollTimer = setInterval(refreshState, 2500);
  })();
})();
