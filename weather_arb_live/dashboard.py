from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, config


LOG_PATH = config.LOG_DIR / "live_bot.log"
LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+) (?P<logger>\S+) (?P<message>.*)$"
)

REQUIRED_LIVE_CREDENTIALS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY",
)

OPTIONAL_RUNTIME_ENV = (
    "POLYMARKET_RECONCILE_USER_ADDRESS",
    "POLYMARKET_FUNDER_ADDRESS",
    "POLYMARKET_PROXY_ADDRESS",
    "POLYMARKET_WALLET_ADDRESS",
    "POLYMARKET_CLOB_HOST",
    "POLYMARKET_CHAIN_ID",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_TICK_SIZE",
)

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Weather Arb Live Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f4;
      --panel: #ffffff;
      --ink: #202321;
      --muted: #6a706b;
      --line: #dfe3dd;
      --teal: #14746f;
      --green: #2f8f46;
      --amber: #b36b00;
      --red: #b42318;
      --soft-teal: #e4f3f0;
      --soft-green: #e7f5ea;
      --soft-amber: #fff1d8;
      --soft-red: #fde7e4;
      --shadow: 0 1px 2px rgba(32, 35, 33, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }

    button, input, select {
      font: inherit;
      letter-spacing: 0;
    }

    button {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
      height: 34px;
      padding: 0 12px;
    }

    button:hover {
      border-color: var(--teal);
    }

    input, select {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--panel);
      color: var(--ink);
      padding: 0 10px;
      min-width: 0;
    }

    input[type="checkbox"] {
      height: auto;
      width: 16px;
      min-width: 16px;
      margin: 0 6px 0 0;
      vertical-align: middle;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 22px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
      position: sticky;
      top: 0;
      z-index: 2;
      backdrop-filter: blur(8px);
    }

    .brand {
      display: flex;
      align-items: baseline;
      gap: 10px;
      min-width: 0;
    }

    .brand h1 {
      font-size: 18px;
      line-height: 1.2;
      margin: 0;
      white-space: nowrap;
    }

    .brand span {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }

    .actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .shell {
      width: min(1480px, 100%);
      margin: 0 auto;
      padding: 18px 22px 28px;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }

    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-height: 84px;
      padding: 12px;
    }

    .metric .label {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      margin-bottom: 10px;
    }

    .metric .value {
      font-size: 22px;
      line-height: 1.15;
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .metric .sub {
      color: var(--muted);
      margin-top: 6px;
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.65fr) minmax(320px, 0.9fr);
      gap: 16px;
      align-items: start;
    }

    .section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
      margin-bottom: 16px;
    }

    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      min-height: 58px;
    }

    .section-head h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.2;
    }

    .section-tools {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .table-wrap {
      overflow: auto;
      max-height: 520px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 880px;
    }

    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #fbfcfa;
      position: sticky;
      top: 0;
      z-index: 1;
    }

    td {
      font-size: 13px;
    }

    .num {
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }

    .question {
      max-width: 360px;
      line-height: 1.35;
    }

    .muted {
      color: var(--muted);
    }

    .badge {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 22px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.1;
      white-space: nowrap;
    }

    .badge.neutral {
      color: var(--teal);
      background: var(--soft-teal);
    }

    .badge.good {
      color: var(--green);
      background: var(--soft-green);
    }

    .badge.warn {
      color: var(--amber);
      background: var(--soft-amber);
    }

    .badge.bad {
      color: var(--red);
      background: var(--soft-red);
    }

    .details {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0;
    }

    .kv {
      display: grid;
      grid-template-columns: minmax(120px, 0.52fr) minmax(0, 1fr);
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
    }

    .kv:last-child {
      border-bottom: 0;
    }

    .kv .key {
      color: var(--muted);
      font-size: 12px;
    }

    .kv .val {
      overflow-wrap: anywhere;
      min-width: 0;
    }

    .env-list, .artifact-list {
      display: grid;
      grid-template-columns: 1fr;
    }

    .list-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
    }

    .list-row:last-child {
      border-bottom: 0;
    }

    .list-row code {
      overflow-wrap: anywhere;
      font-size: 12px;
    }

    .log-list {
      display: grid;
      grid-template-columns: 1fr;
      max-height: 420px;
      overflow: auto;
      background: #101211;
    }

    .log-entry {
      display: grid;
      grid-template-columns: 168px 66px minmax(0, 1fr);
      gap: 10px;
      padding: 8px 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      color: #eef2ef;
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: 12px;
      line-height: 1.35;
    }

    .log-entry .message {
      overflow-wrap: anywhere;
    }

    .empty {
      padding: 28px 14px;
      color: var(--muted);
      text-align: center;
    }

    .error {
      color: var(--red);
      background: var(--soft-red);
      border-bottom: 1px solid #f6c7c1;
      padding: 10px 14px;
      display: none;
    }

    @media (max-width: 1100px) {
      .metric-grid {
        grid-template-columns: repeat(3, minmax(130px, 1fr));
      }

      .layout {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 700px) {
      .topbar {
        align-items: flex-start;
        flex-direction: column;
        padding: 14px;
      }

      .brand {
        align-items: flex-start;
        flex-direction: column;
        gap: 4px;
      }

      .brand h1, .brand span {
        white-space: normal;
      }

      .shell {
        padding: 14px;
      }

      .metric-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .metric .value {
        font-size: 19px;
      }

      .section-head {
        align-items: flex-start;
        flex-direction: column;
      }

      .section-tools {
        width: 100%;
        justify-content: flex-start;
      }

      .section-tools input, .section-tools select {
        flex: 1 1 140px;
      }

      .kv {
        grid-template-columns: 1fr;
        gap: 4px;
      }

      .log-entry {
        grid-template-columns: 1fr;
        gap: 4px;
      }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <h1>Polymarket Weather Live</h1>
      <span id="generatedAt">Loading runtime state</span>
    </div>
    <div class="actions">
      <label class="muted"><input id="autoRefresh" type="checkbox" checked> Auto refresh</label>
      <button id="refreshButton" type="button">Refresh</button>
    </div>
  </header>

  <main class="shell">
    <div id="errorBanner" class="error"></div>

    <section class="metric-grid" aria-label="Runtime metrics">
      <div class="metric">
        <div class="label">Mode</div>
        <div class="value" id="modeMetric">-</div>
        <div class="sub" id="modeSub">-</div>
      </div>
      <div class="metric">
        <div class="label">Positions</div>
        <div class="value" id="positionsMetric">-</div>
        <div class="sub" id="positionsSub">-</div>
      </div>
      <div class="metric">
        <div class="label">Exposure</div>
        <div class="value" id="exposureMetric">-</div>
        <div class="sub" id="exposureSub">-</div>
      </div>
      <div class="metric">
        <div class="label">Manual Review</div>
        <div class="value" id="reviewMetric">-</div>
        <div class="sub" id="reviewSub">-</div>
      </div>
      <div class="metric">
        <div class="label">Log Activity</div>
        <div class="value" id="activityMetric">-</div>
        <div class="sub" id="activitySub">-</div>
      </div>
      <div class="metric">
        <div class="label">Credentials</div>
        <div class="value" id="credentialsMetric">-</div>
        <div class="sub" id="credentialsSub">-</div>
      </div>
    </section>

    <div class="layout">
      <div>
        <section class="section">
          <div class="section-head">
            <h2>Positions</h2>
            <div class="section-tools">
              <input id="positionSearch" type="search" placeholder="Filter positions">
              <select id="positionMode">
                <option value="all">All modes</option>
                <option value="dry">Dry run</option>
                <option value="live">Live</option>
              </select>
            </div>
          </div>
          <div id="positionsError" class="error"></div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>Forecast</th>
                  <th>Edge</th>
                  <th>USD</th>
                  <th>Shares</th>
                  <th>Status</th>
                  <th>Time</th>
                </tr>
              </thead>
              <tbody id="positionsBody"></tbody>
            </table>
          </div>
          <div id="positionsEmpty" class="empty" style="display:none">No positions recorded.</div>
        </section>

        <section class="section">
          <div class="section-head">
            <h2>Logs</h2>
            <div class="section-tools">
              <select id="logLevel">
                <option value="all">All levels</option>
                <option value="warning">Warnings</option>
                <option value="error">Errors</option>
              </select>
            </div>
          </div>
          <div id="logsError" class="error"></div>
          <div id="logList" class="log-list"></div>
          <div id="logsEmpty" class="empty" style="display:none">No log lines available.</div>
        </section>
      </div>

      <aside>
        <section class="section">
          <div class="section-head">
            <h2>Runtime</h2>
          </div>
          <div id="runtimeDetails" class="details"></div>
        </section>

        <section class="section">
          <div class="section-head">
            <h2>Artifacts</h2>
          </div>
          <div id="artifactList" class="artifact-list"></div>
        </section>

        <section class="section">
          <div class="section-head">
            <h2>Environment</h2>
          </div>
          <div id="envList" class="env-list"></div>
        </section>
      </aside>
    </div>
  </main>

  <script>
    const state = { data: null };
    const $ = (id) => document.getElementById(id);

    function formatDate(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString();
    }

    function formatCompactDate(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }

    function formatMoney(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
    }

    function formatDecimal(value, digits = 3) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toFixed(digits);
    }

    function formatPct(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return `${(Number(value) * 100).toFixed(1)}%`;
    }

    function ageText(seconds) {
      if (seconds === null || seconds === undefined) return "-";
      if (seconds < 60) return `${Math.round(seconds)}s ago`;
      if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
      if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
      return `${Math.round(seconds / 86400)}d ago`;
    }

    function setText(id, value) {
      $(id).textContent = value === null || value === undefined || value === "" ? "-" : String(value);
    }

    function showError(id, message) {
      const el = $(id);
      el.style.display = message ? "block" : "none";
      el.textContent = message || "";
    }

    function badge(text, kind = "neutral") {
      const span = document.createElement("span");
      span.className = `badge ${kind}`;
      span.textContent = text;
      return span;
    }

    function appendKv(parent, key, value) {
      const row = document.createElement("div");
      row.className = "kv";
      const keyEl = document.createElement("div");
      keyEl.className = "key";
      keyEl.textContent = key;
      const valEl = document.createElement("div");
      valEl.className = "val";
      valEl.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
      row.append(keyEl, valEl);
      parent.append(row);
    }

    function renderMetrics(data) {
      const runtime = data.runtime;
      const positions = data.positions.summary;
      const health = data.health;
      const liveReady = runtime.dry_run || data.environment.live_credentials_ready;

      setText("generatedAt", `Last refresh ${formatDate(data.generated_at)}`);
      setText("modeMetric", runtime.dry_run ? "Dry run" : "Live");
      setText("modeSub", runtime.clob_host);
      setText("positionsMetric", positions.total);
      setText("positionsSub", `${positions.dry_run} dry run / ${positions.live} live`);
      setText("exposureMetric", formatMoney(positions.total_position_usd));
      setText("exposureSub", `${positions.yes_count} YES / ${positions.no_count} NO`);
      setText("reviewMetric", positions.manual_review);
      setText("reviewSub", `${positions.unknown_posted} unknown order states`);
      setText("activityMetric", health.activity_label);
      setText("activitySub", health.last_log_age_seconds === null ? health.detail : ageText(health.last_log_age_seconds));
      setText("credentialsMetric", liveReady ? "Ready" : "Missing");
      setText("credentialsSub", runtime.dry_run ? "Not required in dry run" : `${data.environment.missing_required.length} missing`);
    }

    function renderRuntime(data) {
      const el = $("runtimeDetails");
      el.innerHTML = "";
      const runtime = data.runtime;
      appendKv(el, "Model", `${runtime.model_name} / ${runtime.model_variant}`);
      appendKv(el, "Poll interval", `${runtime.poll_interval_seconds}s`);
      appendKv(el, "Offline retry", `${runtime.offline_retry_seconds}s`);
      appendKv(el, "Max position", formatMoney(runtime.max_position_usd));
      appendKv(el, "Live market limit", runtime.live_market_limit === null ? "Full scan" : runtime.live_market_limit);
      appendKv(el, "NO side", runtime.enable_no_side ? "Enabled" : "Disabled");
      appendKv(el, "Startup reconcile", runtime.reconcile_on_startup ? "Enabled" : "Disabled");
      appendKv(el, "Data dir", runtime.data_dir);
      appendKv(el, "Log dir", runtime.log_dir);
      appendKv(el, "Version", data.version);
    }

    function renderArtifacts(data) {
      const el = $("artifactList");
      el.innerHTML = "";
      data.artifacts.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const name = document.createElement("code");
        name.textContent = item.name;
        row.append(name, badge(item.exists ? "OK" : "Missing", item.exists ? "good" : "bad"));
        el.append(row);
      });
    }

    function renderEnvironment(data) {
      const el = $("envList");
      el.innerHTML = "";
      data.environment.variables.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const name = document.createElement("code");
        name.textContent = item.name;
        const kind = item.required_for_live && !item.present ? "bad" : item.present ? "good" : "warn";
        row.append(name, badge(item.present ? "Set" : "Unset", kind));
        el.append(row);
      });
    }

    function positionMatches(row) {
      const search = $("positionSearch").value.trim().toLowerCase();
      const mode = $("positionMode").value;
      if (mode === "dry" && !row.dry_run) return false;
      if (mode === "live" && row.dry_run) return false;
      if (!search) return true;
      return [
        row.market_id,
        row.question,
        row.city,
        row.target_date,
        row.side,
        row.token_id,
        row.posted,
        row.reconciliation_status
      ].some((value) => String(value || "").toLowerCase().includes(search));
    }

    function renderPositions(data) {
      const body = $("positionsBody");
      body.innerHTML = "";
      showError("positionsError", data.positions.error);
      const rows = data.positions.recent.filter(positionMatches);
      $("positionsEmpty").style.display = rows.length ? "none" : "block";
      rows.forEach((row) => {
        const tr = document.createElement("tr");
        const question = document.createElement("td");
        question.className = "question";
        const title = document.createElement("div");
        title.textContent = row.question || row.market_id;
        const meta = document.createElement("div");
        meta.className = "muted";
        meta.textContent = [row.city, row.target_date].filter(Boolean).join(" / ");
        question.append(title, meta);

        const side = document.createElement("td");
        side.append(badge(row.side || "-", row.side === "NO" ? "warn" : "neutral"));

        const entry = document.createElement("td");
        entry.className = "num";
        entry.textContent = formatDecimal(row.entry_price);

        const forecast = document.createElement("td");
        forecast.className = "num";
        forecast.textContent = formatPct(row.forecast_prob);

        const edge = document.createElement("td");
        edge.className = "num";
        edge.textContent = formatPct(row.edge);

        const usd = document.createElement("td");
        usd.className = "num";
        usd.textContent = formatMoney(row.position_usd);

        const shares = document.createElement("td");
        shares.className = "num";
        shares.textContent = formatDecimal(row.shares, 2);

        const status = document.createElement("td");
        if (row.manual_review) {
          status.append(badge("Review", "bad"));
        } else if (row.posted === "unknown") {
          status.append(badge("Unknown", "warn"));
        } else if (row.dry_run) {
          status.append(badge("Dry run", "neutral"));
        } else {
          status.append(badge(String(row.posted || "Live"), "good"));
        }

        const time = document.createElement("td");
        time.className = "num";
        time.textContent = formatCompactDate(row.entry_time);

        tr.append(question, side, entry, forecast, edge, usd, shares, status, time);
        body.append(tr);
      });
    }

    function logMatches(entry) {
      const level = $("logLevel").value;
      if (level === "warning") return entry.level === "WARNING" || entry.level === "ERROR" || entry.level === "CRITICAL";
      if (level === "error") return entry.level === "ERROR" || entry.level === "CRITICAL";
      return true;
    }

    function renderLogs(data) {
      const el = $("logList");
      el.innerHTML = "";
      showError("logsError", data.logs.error);
      const entries = data.logs.entries.filter(logMatches);
      $("logsEmpty").style.display = entries.length ? "none" : "block";
      entries.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "log-entry";
        const at = document.createElement("div");
        at.textContent = entry.timestamp || "";
        const level = document.createElement("div");
        level.textContent = entry.level || "";
        const msg = document.createElement("div");
        msg.className = "message";
        msg.textContent = entry.message || entry.raw || "";
        row.append(at, level, msg);
        el.append(row);
      });
    }

    function render(data) {
      state.data = data;
      showError("errorBanner", "");
      renderMetrics(data);
      renderRuntime(data);
      renderArtifacts(data);
      renderEnvironment(data);
      renderPositions(data);
      renderLogs(data);
    }

    async function refresh() {
      try {
        const response = await fetch("/api/status?log_lines=160", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        showError("errorBanner", `Dashboard refresh failed: ${error.message}`);
      }
    }

    $("refreshButton").addEventListener("click", refresh);
    $("positionSearch").addEventListener("input", () => state.data && renderPositions(state.data));
    $("positionMode").addEventListener("change", () => state.data && renderPositions(state.data));
    $("logLevel").addEventListener("change", () => state.data && renderLogs(state.data));
    setInterval(() => {
      if ($("autoRefresh").checked) refresh();
    }, 15000);
    refresh();
  </script>
</body>
</html>
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _file_status(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "size_bytes": 0,
            "modified_at": None,
        }
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _read_json(path: Path) -> tuple[Any, str | None]:
    if not path.exists():
        return None, None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle), None
    except Exception as exc:
        return None, str(exc)


def tail_lines(path: Path, limit: int) -> tuple[list[str], str | None]:
    if limit <= 0 or not path.exists():
        return [], None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            remaining = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while remaining > 0 and newline_count <= limit:
                read_size = min(8192, remaining)
                remaining -= read_size
                handle.seek(remaining)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        content = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return content.splitlines()[-limit:], None
    except Exception as exc:
        return [], str(exc)


def parse_log_lines(lines: list[str]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    levels: Counter[str] = Counter()
    last_cycle_start: str | None = None
    last_cycle_end: str | None = None
    last_enter: str | None = None

    for line in lines:
        match = LOG_PATTERN.match(line)
        if match:
            entry = match.groupdict()
            entry["raw"] = line
        else:
            entry = {
                "timestamp": "",
                "level": "",
                "logger": "",
                "message": line,
                "raw": line,
            }
        level = entry["level"]
        message = entry["message"]
        if level:
            levels[level] += 1
        if message.startswith("cycle_start"):
            last_cycle_start = entry["timestamp"]
        elif message.startswith("cycle_end"):
            last_cycle_end = entry["timestamp"]
        elif message.startswith("decision_enter"):
            last_enter = entry["timestamp"]
        entries.append(entry)

    return {
        "entries": entries,
        "level_counts": dict(levels),
        "last_cycle_start": last_cycle_start,
        "last_cycle_end": last_cycle_end,
        "last_enter": last_enter,
    }


def summarize_positions(positions: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    side_counts: Counter[str] = Counter()
    dry_run_count = 0
    live_count = 0
    unknown_posted = 0
    manual_review = 0
    total_position_usd = 0.0

    for key, value in positions.items():
        if not isinstance(value, dict):
            continue
        dry_run = bool(value.get("dry_run"))
        dry_run_count += int(dry_run)
        live_count += int(not dry_run)

        side = str(value.get("side") or "").upper()
        if side:
            side_counts[side] += 1

        order_response = value.get("order_response") if isinstance(value.get("order_response"), dict) else {}
        posted = order_response.get("posted")
        if posted == "unknown":
            unknown_posted += 1

        reconciliation = value.get("reconciliation") if isinstance(value.get("reconciliation"), dict) else {}
        requires_review = bool(reconciliation.get("requires_manual_review"))
        manual_review += int(requires_review)

        position_usd = _safe_float(value.get("position_usd"))
        if position_usd is not None:
            total_position_usd += position_usd

        rows.append(
            {
                "market_id": str(value.get("market_id") or key),
                "token_id": str(value.get("token_id") or ""),
                "side": side,
                "question": str(value.get("question") or ""),
                "city": str(value.get("city") or ""),
                "target_date": value.get("target_date"),
                "market_price": _safe_float(value.get("market_price")),
                "entry_price": _safe_float(value.get("entry_price")),
                "shares": _safe_float(value.get("shares")),
                "position_usd": position_usd,
                "forecast_prob": _safe_float(value.get("forecast_prob")),
                "edge": _safe_float(value.get("edge")),
                "lead_days": value.get("lead_days"),
                "entry_time": value.get("entry_time"),
                "dry_run": dry_run,
                "posted": posted,
                "manual_review": requires_review,
                "reconciliation_status": reconciliation.get("status"),
            }
        )

    def sort_key(row: dict[str, Any]) -> datetime:
        return _parse_timestamp(row.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc)

    rows.sort(key=sort_key, reverse=True)

    return {
        "total": len(rows),
        "dry_run": dry_run_count,
        "live": live_count,
        "yes_count": side_counts.get("YES", 0),
        "no_count": side_counts.get("NO", 0),
        "unknown_posted": unknown_posted,
        "manual_review": manual_review,
        "total_position_usd": round(total_position_usd, 2),
        "recent": rows,
    }


def runtime_payload() -> dict[str, Any]:
    runtime = config.load_runtime_config()
    return {
        "dry_run": runtime.dry_run,
        "poll_interval_seconds": runtime.poll_interval_seconds,
        "max_position_usd": runtime.max_position_usd,
        "clob_host": runtime.clob_host,
        "model_name": runtime.model_name,
        "model_variant": runtime.model_variant,
        "enable_no_side": runtime.enable_no_side,
        "offline_retry_seconds": runtime.offline_retry_seconds,
        "reconcile_on_startup": runtime.reconcile_on_startup,
        "live_market_limit": config.live_market_limit(),
        "data_dir": str(config.DATA_DIR),
        "log_dir": str(config.LOG_DIR),
    }


def environment_payload() -> dict[str, Any]:
    variables = []
    for name in REQUIRED_LIVE_CREDENTIALS:
        variables.append(
            {
                "name": name,
                "present": bool(os.getenv(name)),
                "required_for_live": True,
            }
        )
    for name in OPTIONAL_RUNTIME_ENV:
        variables.append(
            {
                "name": name,
                "present": bool(os.getenv(name)),
                "required_for_live": False,
            }
        )
    missing_required = [
        item["name"]
        for item in variables
        if item["required_for_live"] and not item["present"]
    ]
    return {
        "variables": variables,
        "missing_required": missing_required,
        "live_credentials_ready": not missing_required,
    }


def artifacts_payload() -> list[dict[str, Any]]:
    artifacts = (
        ("live_positions", config.POSITIONS_PATH),
        ("live_bot_log", LOG_PATH),
        ("weather_cache", config.WEATHER_CACHE_PATH),
        ("empirical_residuals", config.RESIDUALS_CACHE_PATH),
        ("sigma_cache", config.SIGMA_CACHE_PATH),
        ("calibration_table", config.CALIBRATION_PATH),
    )
    return [{"name": name, **_file_status(path)} for name, path in artifacts]


def health_payload(runtime: dict[str, Any], log_status: dict[str, Any]) -> dict[str, Any]:
    if not log_status["exists"] or not log_status["modified_at"]:
        return {
            "activity": "no_log",
            "activity_label": "No log",
            "detail": "logs/live_bot.log has not been created",
            "last_log_age_seconds": None,
        }

    modified_at = _parse_timestamp(log_status["modified_at"])
    if modified_at is None:
        return {
            "activity": "unknown",
            "activity_label": "Unknown",
            "detail": "Log timestamp could not be parsed",
            "last_log_age_seconds": None,
        }

    age_seconds = max(0.0, (datetime.now(timezone.utc) - modified_at).total_seconds())
    threshold = max(
        300,
        int(runtime["poll_interval_seconds"]) * 2 + 60,
        int(runtime["offline_retry_seconds"]) * 2 + 60,
    )
    is_recent = age_seconds <= threshold
    return {
        "activity": "recent" if is_recent else "stale",
        "activity_label": "Recent" if is_recent else "Stale",
        "detail": "Last log write is within the expected polling window" if is_recent else "Last log write is older than the polling window",
        "last_log_age_seconds": round(age_seconds, 1),
    }


def build_dashboard_state(*, log_limit: int = 160) -> dict[str, Any]:
    runtime = runtime_payload()
    environment = environment_payload()
    artifacts = artifacts_payload()
    log_status = next(item for item in artifacts if item["name"] == "live_bot_log")

    positions_data, positions_error = _read_json(config.POSITIONS_PATH)
    if positions_data is None:
        positions = {}
    elif isinstance(positions_data, dict):
        positions = positions_data
    else:
        positions = {}
        positions_error = positions_error or "positions file is not a JSON object"

    log_lines, logs_error = tail_lines(LOG_PATH, log_limit)
    parsed_logs = parse_log_lines(log_lines)
    position_summary = summarize_positions(positions)
    recent_positions = position_summary.pop("recent")

    return {
        "generated_at": utc_now_iso(),
        "version": __version__,
        "runtime": runtime,
        "environment": environment,
        "health": health_payload(runtime, log_status),
        "artifacts": artifacts,
        "positions": {
            "path": str(config.POSITIONS_PATH),
            "error": positions_error,
            "summary": position_summary,
            "recent": recent_positions,
        },
        "logs": {
            "path": str(LOG_PATH),
            "error": logs_error,
            **parsed_logs,
        },
    }


def _query_int(query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (query.get(name) or [default])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WeatherArbDashboard/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_bytes(
                DASHBOARD_HTML.encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            log_limit = _query_int(query, "log_lines", 160, 0, 500)
            self._send_json(build_dashboard_state(log_limit=log_limit))
            return
        if parsed.path == "/healthz":
            self._send_json({"ok": True, "generated_at": utc_now_iso()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    def _send_bytes(self, payload: bytes, *, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Polymarket weather live bot dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"dashboard listening on {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
