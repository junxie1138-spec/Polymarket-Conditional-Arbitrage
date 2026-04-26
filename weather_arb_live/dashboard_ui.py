from __future__ import annotations


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket Weather Live - Dashboard</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

    :root {
      color-scheme: light;
      --paper: #f6f7f4;
      --panel: #ffffff;
      --panel-2: #fbfcfa;
      --terminal: #101211;
      --terminal-2: #1a1d1b;
      --ink: #202321;
      --ink-2: #3b403d;
      --muted: #6a706b;
      --muted-2: #8e948f;
      --line: #dfe3dd;
      --line-2: #ebede8;
      --teal: #14746f;
      --teal-600: #0f5a56;
      --teal-700: #0a423f;
      --teal-soft: #e4f3f0;
      --green: #2f8f46;
      --green-soft: #e7f5ea;
      --amber: #b36b00;
      --amber-soft: #fff1d8;
      --red: #b42318;
      --red-soft: #fde7e4;
      --sky: #2a6db0;
      --sky-soft: #e3edf8;
      --font-display: 'Fraunces', 'Iowan Old Style', Georgia, serif;
      --font-ui: 'IBM Plex Sans', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      --font-mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace;
      --fs-microlabel: 10px;
      --fs-caption: 11px;
      --fs-small: 12px;
      --fs-body: 13px;
      --fs-base: 14px;
      --fs-lead: 15px;
      --fs-h3: 18px;
      --fs-display: 40px;
      --s-2: 4px;
      --s-3: 6px;
      --s-4: 8px;
      --s-5: 12px;
      --s-6: 14px;
      --s-7: 16px;
      --s-8: 18px;
      --s-9: 22px;
      --s-10: 28px;
      --s-11: 36px;
      --r-chip: 4px;
      --r-ctl: 7px;
      --r-card: 8px;
      --r-modal: 12px;
      --r-pill: 999px;
      --e1: 0 1px 2px rgba(32, 35, 33, 0.08);
      --e3: 0 12px 32px rgba(32, 35, 33, 0.16), 0 0 0 1px rgba(32, 35, 33, 0.06);
      --t-fast: 100ms ease-out;
      --t-base: 150ms ease-out;
      --shell-max: 1480px;
      --topbar-h: 62px;
      --section-head-h: 58px;
    }

    * { box-sizing: border-box; }
    [hidden] { display: none !important; }

    html, body {
      margin: 0;
      min-height: 100vh;
      background: var(--paper);
      color: var(--ink);
      font-family: var(--font-ui);
      font-size: var(--fs-base);
      line-height: 1.45;
      letter-spacing: 0;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    button, input, select {
      font: inherit;
      letter-spacing: 0;
    }

    button, .button-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: var(--r-ctl);
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
      padding: 0 12px;
      text-decoration: none;
      transition: border-color var(--t-fast), background var(--t-fast), transform var(--t-fast);
    }

    button:hover, .button-link:hover {
      border-color: var(--teal);
      background: var(--panel-2);
    }

    button:active, .button-link:active {
      transform: translateY(0.5px);
    }

    .btn-primary {
      background: var(--teal);
      border-color: var(--teal);
      color: #fff;
      font-weight: 500;
    }

    .btn-primary:hover {
      background: var(--teal-600);
      border-color: var(--teal-600);
    }

    .input {
      height: 32px;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: var(--r-ctl);
      background: var(--panel);
      color: var(--ink);
      padding: 0 10px;
    }

    .input:focus {
      outline: 2px solid var(--teal);
      outline-offset: -1px;
      border-color: var(--teal);
    }

    .muted { color: var(--muted); }
    .mono, code, .num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
    .num { white-space: nowrap; }
    .small { font-size: var(--fs-caption); }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--s-7);
      min-height: var(--topbar-h);
      padding: 12px 28px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
      backdrop-filter: blur(8px);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: var(--s-5);
      min-width: 0;
    }

    .brand-mark {
      flex: 0 0 auto;
      width: 22px;
      height: 22px;
    }

    .brand h1 {
      margin: 0;
      font-family: var(--font-display);
      font-variation-settings: 'opsz' 96;
      font-size: 20px;
      font-weight: 600;
      line-height: 1;
      letter-spacing: 0;
      white-space: nowrap;
    }

    .live-tag {
      margin-left: 6px;
      color: var(--muted);
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 500;
      letter-spacing: 0.04em;
      vertical-align: 2px;
    }

    .brand-meta {
      display: inline-flex;
      align-items: center;
      gap: var(--s-3);
      margin-left: var(--s-4);
      padding-left: var(--s-6);
      border-left: 1px solid var(--line);
      color: var(--muted);
      font-size: var(--fs-small);
      white-space: nowrap;
    }

    .dot-sep { color: var(--muted-2); padding: 0 2px; }

    .actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: var(--s-5);
      flex-wrap: wrap;
    }

    .toggle {
      display: inline-flex;
      align-items: center;
      gap: var(--s-3);
      color: var(--muted);
      cursor: pointer;
      font-size: var(--fs-body);
      white-space: nowrap;
    }

    .toggle input {
      width: 14px;
      height: 14px;
      margin: 0;
    }

    .mode-dot {
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }

    .mode-dot.is-live {
      background: var(--green);
      animation: pulse 1.5s ease-in-out infinite;
    }

    .mode-dot.is-dry { background: var(--muted-2); }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

    .shell {
      width: min(var(--shell-max), 100%);
      margin: 0 auto;
      padding: var(--s-8) var(--s-10) var(--s-11);
    }

    .error {
      display: none;
      margin-bottom: var(--s-5);
      border: 1px solid #f6c7c1;
      border-radius: var(--r-card);
      background: var(--red-soft);
      color: var(--red);
      padding: 10px 14px;
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: var(--s-5);
      margin-bottom: var(--s-7);
    }

    .kpi {
      display: grid;
      align-content: start;
      gap: var(--s-3);
      min-height: 102px;
      border: 1px solid var(--line);
      border-radius: var(--r-card);
      background: var(--panel);
      box-shadow: var(--e1);
      padding: 14px 16px;
    }

    .kpi-label {
      color: var(--muted);
      font-size: var(--fs-microlabel);
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .kpi-value {
      display: flex;
      align-items: center;
      gap: var(--s-4);
      min-width: 0;
      overflow-wrap: anywhere;
      font-family: var(--font-display);
      font-variation-settings: 'opsz' 144;
      font-size: 28px;
      font-weight: 500;
      line-height: 1;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }

    .kpi-value.is-good { color: var(--green); }
    .kpi-value.is-warn { color: var(--amber); }
    .kpi-value.is-bad { color: var(--red); }
    .kpi-sub {
      min-width: 0;
      overflow-wrap: anywhere;
      color: var(--muted);
      font-family: var(--font-mono);
      font-size: var(--fs-small);
      font-variant-numeric: tabular-nums;
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.7fr) minmax(320px, 0.85fr);
      gap: var(--s-7);
      align-items: start;
    }

    .main, .sidebar {
      display: grid;
      gap: var(--s-7);
      min-width: 0;
    }

    .card {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: var(--r-card);
      background: var(--panel);
      box-shadow: var(--e1);
    }

    .card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--s-6);
      min-height: var(--section-head-h);
      border-bottom: 1px solid var(--line);
      padding: 12px 16px;
    }

    .card-head-l {
      display: flex;
      align-items: baseline;
      gap: var(--s-5);
      min-width: 0;
    }

    .card-head-r {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      flex-wrap: wrap;
      gap: 10px;
    }

    .card-head h2 {
      margin: 0;
      font-size: var(--fs-lead);
      font-weight: 600;
      line-height: 1.25;
    }

    .card-head-meta { font-size: var(--fs-small); }

    .seg {
      display: inline-flex;
      height: 32px;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: var(--r-ctl);
      background: var(--panel);
    }

    .seg button {
      height: 100%;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--muted);
      font-size: var(--fs-small);
      font-weight: 500;
      padding: 0 12px;
    }

    .seg button + button { border-left: 1px solid var(--line); }
    .seg button:hover { border-color: var(--line); background: var(--panel-2); }
    .seg button.on { background: var(--ink); color: #fff; }
    .seg button.on.yes { background: var(--teal); }
    .seg button.on.no { background: var(--amber); }

    .table-wrap {
      overflow: auto;
      max-height: 520px;
    }

    table {
      width: 100%;
      min-width: 1080px;
      border-collapse: collapse;
    }

    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: middle;
    }

    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: var(--panel-2);
      color: var(--muted);
      cursor: pointer;
      font-size: var(--fs-caption);
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      user-select: none;
    }

    th.is-sorted { color: var(--ink); }
    th.t-right, td.t-right { text-align: right; }
    td { font-size: var(--fs-body); }
    tbody tr { cursor: pointer; transition: background var(--t-fast); }
    tbody tr:hover td { background: var(--panel-2); }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr.row-flag td { background: color-mix(in oklab, var(--red-soft) 50%, transparent); }
    tbody tr.row-flag:hover td { background: var(--red-soft); }

    .caret { margin-left: 4px; color: var(--ink); font-size: 8px; }
    .question-cell { max-width: 390px; line-height: 1.3; }
    .question-title { color: var(--ink); font-size: var(--fs-body); }
    .question-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2);
      margin-top: 3px;
      color: var(--muted);
      font-size: var(--fs-caption);
    }

    .pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 20px;
      border-radius: var(--r-pill);
      font-size: var(--fs-caption);
      font-weight: 700;
      line-height: 1.1;
      padding: 2px 8px;
      white-space: nowrap;
    }

    .pill.yes { color: var(--teal); background: var(--teal-soft); }
    .pill.no { color: var(--amber); background: var(--amber-soft); }
    .pill.status-live { color: var(--green); background: var(--green-soft); }
    .pill.status-dry { color: var(--muted); background: var(--line-2); }
    .pill.status-unknown { color: var(--amber); background: var(--amber-soft); }
    .pill.status-review { color: var(--red); background: var(--red-soft); }

    .edge-cell {
      display: inline-flex;
      align-items: center;
      justify-content: flex-end;
      gap: var(--s-4);
    }

    .edge-bar {
      display: inline-block;
      width: 48px;
      height: 4px;
      overflow: hidden;
      border-radius: 2px;
      background: var(--line-2);
    }

    .edge-bar > i { display: block; height: 100%; }
    .edge-pos { color: var(--green); }
    .edge-neg { color: var(--red); }
    .pnl-cell {
      display: grid;
      justify-items: end;
      gap: 2px;
      line-height: 1.15;
    }

    .pnl-cell small {
      color: var(--muted);
      font-family: var(--font-mono);
      font-size: var(--fs-caption);
    }

    .pnl-chart-body {
      display: grid;
      gap: var(--s-6);
      padding: 16px;
    }

    .pnl-chart-stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: var(--s-5);
    }

    .chart-stat {
      min-width: 0;
      border: 1px solid var(--line-2);
      border-radius: var(--r-card);
      background: var(--panel-2);
      padding: 10px 12px;
    }

    .chart-stat span {
      display: block;
      color: var(--muted);
      font-size: var(--fs-microlabel);
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .chart-stat strong {
      display: block;
      min-width: 0;
      margin-top: 4px;
      overflow-wrap: anywhere;
      font-family: var(--font-display);
      font-variation-settings: 'opsz' 96;
      font-size: 22px;
      font-weight: 500;
      line-height: 1.05;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }

    .pnl-chart-frame {
      position: relative;
      min-height: 220px;
      border: 1px solid var(--line-2);
      border-radius: var(--r-card);
      background: var(--panel-2);
      padding: 10px;
    }

    .pnl-chart {
      display: block;
      width: 100%;
      height: 220px;
      overflow: visible;
    }

    .pnl-chart-grid { stroke: var(--line-2); stroke-width: 1; vector-effect: non-scaling-stroke; }
    .pnl-chart-zero { stroke: var(--muted-2); stroke-width: 1; stroke-dasharray: 5 5; vector-effect: non-scaling-stroke; }
    .pnl-chart-area { fill: var(--teal-soft); opacity: 0.72; }
    .pnl-chart-area.neg { fill: var(--red-soft); }
    .pnl-chart-line {
      fill: none;
      stroke: var(--teal);
      stroke-width: 2.5;
      stroke-linecap: round;
      stroke-linejoin: round;
      vector-effect: non-scaling-stroke;
    }
    .pnl-chart-line.neg { stroke: var(--red); }
    .pnl-chart-point {
      fill: var(--panel);
      stroke: var(--teal);
      stroke-width: 2;
      vector-effect: non-scaling-stroke;
    }
    .pnl-chart-point.neg { stroke: var(--red); }
    .pnl-chart-empty {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: var(--muted);
      font-size: var(--fs-body);
      text-align: center;
      padding: 16px;
    }

    .logs {
      max-height: 360px;
      overflow: auto;
      background: var(--terminal);
    }

    .log-row {
      display: grid;
      grid-template-columns: 178px 64px minmax(0, 1fr);
      gap: 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      color: #eef2ef;
      font-family: var(--font-mono);
      font-size: var(--fs-small);
      line-height: 1.4;
      padding: 6px 16px;
    }

    .log-row:last-child { border-bottom: 0; }
    .log-row:hover { background: var(--terminal-2); }
    .log-ts { color: #8c948f; }
    .log-lvl { font-weight: 600; }
    .log-lvl.lvl-info { color: #9bd0c8; }
    .log-lvl.lvl-warning { color: #f0bf6b; }
    .log-lvl.lvl-error, .log-lvl.lvl-critical { color: #ff8d83; }
    .log-msg { min-width: 0; overflow-wrap: anywhere; }
    .log-msg .event-key { color: #fff; font-weight: 600; }
    .log-msg .skip-key { color: #f0bf6b; font-weight: 600; }
    .log-msg .arg { color: #c8d3cd; }

    .lvl-counts {
      display: inline-flex;
      gap: var(--s-3);
    }

    .lvl-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 24px;
      border: 1px solid var(--line);
      border-radius: var(--r-pill);
      color: var(--muted);
      font-family: var(--font-mono);
      font-size: var(--fs-caption);
      padding: 0 8px;
    }

    .lvl-pill i {
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
    }

    .lvl-pill.info i { background: var(--teal); }
    .lvl-pill.warn i { background: var(--amber); }
    .lvl-pill.error i { background: var(--red); }

    .kv-list, .list { display: grid; }
    .kv {
      display: grid;
      grid-template-columns: minmax(110px, 0.5fr) minmax(0, 1fr);
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid var(--line-2);
      padding: 9px 16px;
    }

    .kv:last-child, .list-row:last-child { border-bottom: 0; }
    .kv .k { color: var(--muted); font-size: var(--fs-small); }
    .kv .v { min-width: 0; overflow-wrap: anywhere; font-size: var(--fs-body); }
    .kv .v.small { font-size: var(--fs-small); }

    .list-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      border-bottom: 1px solid var(--line-2);
      padding: 9px 16px;
    }

    .list-row code {
      min-width: 0;
      overflow-wrap: anywhere;
      font-size: var(--fs-small);
    }

    .list-meta {
      display: inline-flex;
      align-items: center;
      gap: var(--s-4);
      flex: 0 0 auto;
    }

    .empty {
      padding: 28px 14px;
      color: var(--muted);
      text-align: center;
    }

    .drawer-backdrop {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: flex;
      align-items: stretch;
      justify-content: flex-end;
      background: rgba(32, 35, 33, 0.32);
    }

    .drawer {
      width: min(520px, 100%);
      height: 100vh;
      overflow: auto;
      border-left: 1px solid var(--line);
      background: var(--panel);
      box-shadow: var(--e3);
      animation: slideIn 200ms ease-out;
    }

    @keyframes slideIn {
      from { transform: translateX(20px); opacity: 0; }
      to { transform: none; opacity: 1; }
    }

    .drawer-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding: 16px 22px;
    }

    .drawer-head h3 {
      margin: 0;
      font-size: 16px;
      line-height: 1.35;
    }

    .drawer-body {
      display: grid;
      gap: 18px;
      padding: 18px 22px;
    }

    .drawer-pills {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: var(--s-4);
    }

    .drawer-stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }

    .drawer-stat {
      border: 1px solid var(--line);
      border-radius: var(--r-card);
      background: var(--panel-2);
      padding: 12px;
    }

    .drawer-stat .l {
      color: var(--muted);
      font-size: var(--fs-microlabel);
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .drawer-stat .v {
      margin-top: 6px;
      font-family: var(--font-display);
      font-variation-settings: 'opsz' 96;
      font-size: 24px;
      font-weight: 500;
      line-height: 1;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }

    @media (max-width: 1240px) {
      .kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
    }

    @media (max-width: 700px) {
      .topbar {
        align-items: flex-start;
        flex-direction: column;
        height: auto;
        padding: 12px 16px;
      }

      .brand {
        align-items: flex-start;
        flex-wrap: wrap;
      }

      .brand h1 { white-space: normal; }
      .brand-meta {
        flex-basis: 100%;
        margin-left: 34px;
        padding-left: 0;
        border-left: 0;
        white-space: normal;
      }

      .shell { padding: 14px 16px 28px; }
      .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kpi-value { font-size: 24px; }
      .card-head { align-items: flex-start; flex-direction: column; }
      .card-head-r { width: 100%; justify-content: flex-start; }
      .card-head-r .input { flex: 1 1 140px; }
      .pnl-chart-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kv { grid-template-columns: 1fr; gap: var(--s-2); }
      .log-row { grid-template-columns: 1fr; gap: var(--s-2); }
      .drawer-stat-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <svg class="brand-mark" viewBox="0 0 28 28" fill="none" aria-hidden="true">
        <circle cx="14" cy="14" r="11" stroke="#202321" stroke-width="1.5"></circle>
        <path d="M6 17 L11 13 L15 15 L22 9" stroke="#14746f" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"></path>
        <circle cx="14" cy="14" r="1.6" fill="#b36b00"></circle>
      </svg>
      <h1>Polymarket Weather <span class="live-tag">Live</span></h1>
      <span class="brand-meta">
        <span id="modeDot" class="mode-dot is-dry" aria-hidden="true"></span>
        <span id="modeText">Loading</span>
        <span class="dot-sep">/</span>
        <span id="versionText" class="mono">v-</span>
        <span class="dot-sep">/</span>
        <span id="generatedAt">Last refresh -</span>
      </span>
    </div>
    <div class="actions">
      <label class="toggle"><input id="autoRefresh" type="checkbox" checked> Auto refresh</label>
      <button id="refreshButton" class="btn-primary" type="button">Refresh</button>
    </div>
  </header>

  <main class="shell">
    <div id="errorBanner" class="error"></div>

    <section class="kpi-grid" aria-label="Runtime metrics">
      <div class="kpi">
        <div class="kpi-label">Mode</div>
        <div id="modeMetric" class="kpi-value"><span class="mode-dot is-dry" aria-hidden="true"></span><span>-</span></div>
        <div id="modeSub" class="kpi-sub">-</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Positions</div>
        <div id="positionsMetric" class="kpi-value">-</div>
        <div id="positionsSub" class="kpi-sub">-</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Exposure</div>
        <div id="exposureMetric" class="kpi-value">-</div>
        <div id="exposureSub" class="kpi-sub">-</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Manual review</div>
        <div id="reviewMetric" class="kpi-value">-</div>
        <div id="reviewSub" class="kpi-sub">-</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Log activity</div>
        <div id="activityMetric" class="kpi-value">-</div>
        <div id="activitySub" class="kpi-sub">-</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Account balance</div>
        <div id="accountMetric" class="kpi-value">-</div>
        <div id="accountSub" class="kpi-sub">-</div>
      </div>
    </section>

    <div class="layout">
      <div class="main">
        <section class="card">
          <div class="card-head">
            <div class="card-head-l">
              <h2>Cumulative PnL</h2>
              <span id="pnlChartMeta" class="muted card-head-meta">0 marked positions</span>
            </div>
            <div class="card-head-r"><span id="pnlChartBadge" class="pill status-dry">-</span></div>
          </div>
          <div class="pnl-chart-body">
            <div class="pnl-chart-stats">
              <div class="chart-stat"><span>Latest</span><strong id="pnlChartLatest">-</strong></div>
              <div class="chart-stat"><span>High water</span><strong id="pnlChartHigh">-</strong></div>
              <div class="chart-stat"><span>Low water</span><strong id="pnlChartLow">-</strong></div>
              <div class="chart-stat"><span>Marked</span><strong id="pnlChartMarked">-</strong></div>
            </div>
            <div class="pnl-chart-frame">
              <svg id="pnlChart" class="pnl-chart" viewBox="0 0 640 220" preserveAspectRatio="none" role="img" aria-label="Cumulative PnL chart"></svg>
              <div id="pnlChartEmpty" class="pnl-chart-empty">No marked PnL yet.</div>
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <div class="card-head-l">
              <h2>Positions</h2>
              <span id="positionsCount" class="muted card-head-meta">0 shown</span>
            </div>
            <div class="card-head-r">
              <input id="positionSearch" class="input" type="search" placeholder="Filter by market, city, ID">
              <div class="seg" aria-label="Side filter">
                <button id="sideAll" class="on" type="button" data-side="all">All</button>
                <button id="sideYes" type="button" data-side="YES">YES</button>
                <button id="sideNo" type="button" data-side="NO">NO</button>
              </div>
              <select id="positionMode" class="input">
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
                  <th data-sort="question">Market</th>
                  <th data-sort="side">Side</th>
                  <th data-sort="entry_price" class="t-right">Entry</th>
                  <th data-sort="forecast_prob" class="t-right">Forecast</th>
                  <th data-sort="edge" class="t-right">Edge</th>
                  <th data-sort="pnl_usd" class="t-right">PnL</th>
                  <th data-sort="position_usd" class="t-right">USD</th>
                  <th data-sort="shares" class="t-right">Shares</th>
                  <th>Status</th>
                  <th data-sort="entry_time" class="t-right">Time</th>
                </tr>
              </thead>
              <tbody id="positionsBody"></tbody>
            </table>
          </div>
          <div id="positionsEmpty" class="empty" hidden>No positions recorded.</div>
        </section>

        <section class="card">
          <div class="card-head">
            <div class="card-head-l">
              <h2>Logs</h2>
              <span id="logsMeta" class="muted card-head-meta mono">tail / live_bot.log / 0 lines</span>
            </div>
            <div class="card-head-r">
              <div class="lvl-counts">
                <span class="lvl-pill info"><i></i>INFO <span id="infoCount">0</span></span>
                <span class="lvl-pill warn"><i></i>WARN <span id="warnCount">0</span></span>
                <span class="lvl-pill error"><i></i>ERR <span id="errorCount">0</span></span>
              </div>
              <select id="logLevel" class="input">
                <option value="all">All levels</option>
                <option value="warning">Warnings</option>
                <option value="error">Errors</option>
              </select>
            </div>
          </div>
          <div id="logsError" class="error"></div>
          <div id="logList" class="logs"></div>
          <div id="logsEmpty" class="empty" hidden>No log lines available.</div>
        </section>
      </div>

      <aside class="sidebar">
        <section class="card">
          <div class="card-head"><div class="card-head-l"><h2>Runtime</h2></div></div>
          <div id="runtimeDetails" class="kv-list"></div>
        </section>

        <section class="card">
          <div class="card-head">
            <div class="card-head-l"><h2>Account</h2></div>
            <div class="card-head-r"><span id="accountStatus" class="pill status-dry">-</span></div>
          </div>
          <div id="accountDetails" class="kv-list"></div>
        </section>

        <section class="card">
          <div class="card-head"><div class="card-head-l"><h2>Artifacts</h2></div></div>
          <div id="artifactList" class="list"></div>
        </section>

        <section class="card">
          <div class="card-head">
            <div class="card-head-l"><h2>Environment</h2></div>
            <div class="card-head-r"><span id="environmentStatus" class="pill status-review">-</span></div>
          </div>
          <div id="envList" class="list"></div>
        </section>
      </aside>
    </div>
  </main>

  <div id="drawerBackdrop" class="drawer-backdrop" hidden>
    <div class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawerTitle">
      <div class="drawer-head">
        <div>
          <h3 id="drawerTitle">Position</h3>
          <div id="drawerMeta" class="muted small mono" style="margin-top: 6px;"></div>
        </div>
        <button id="drawerClose" type="button">Close</button>
      </div>
      <div class="drawer-body">
        <div id="drawerPills" class="drawer-pills"></div>
        <div id="drawerStats" class="drawer-stat-grid"></div>
        <div id="drawerDetails" class="kv-list"></div>
      </div>
    </div>
  </div>

  <script>
    const state = {
      data: null,
      side: "all",
      sortBy: "entry_time",
      sortDir: "desc",
    };
    const $ = (id) => document.getElementById(id);

    function setText(id, value) {
      $(id).textContent = value === null || value === undefined || value === "" ? "-" : String(value);
    }

    function setError(id, message) {
      const el = $(id);
      el.style.display = message ? "block" : "none";
      el.textContent = message || "";
    }

    function clearNode(node) {
      while (node.firstChild) node.removeChild(node.firstChild);
    }

    function formatDate(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    }

    function formatCompactDate(value) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    function formatMoney(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toLocaleString(undefined, {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      });
    }

    function formatSignedMoney(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      const number = Number(value);
      const formatted = Math.abs(number).toLocaleString(undefined, {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      });
      return `${number < 0 ? "-" : "+"}${formatted}`;
    }

    function formatDecimal(value, digits = 3) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toFixed(digits);
    }

    function formatPct(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return `${(Number(value) * 100).toFixed(1)}%`;
    }

    function formatSignedPct(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      const number = Number(value);
      const pct = Math.abs(number * 100).toFixed(1);
      return `${number < 0 ? "-" : "+"}${pct}%`;
    }

    function formatBytes(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      const n = Number(value);
      if (n < 1024) return `${n} B`;
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
      return `${(n / 1024 / 1024).toFixed(2)} MB`;
    }

    function ageText(seconds) {
      if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "-";
      const value = Number(seconds);
      if (value < 60) return `${Math.round(value)}s ago`;
      if (value < 3600) return `${Math.round(value / 60)}m ago`;
      if (value < 86400) return `${Math.round(value / 3600)}h ago`;
      return `${Math.round(value / 86400)}d ago`;
    }

    function shortPath(path) {
      if (!path) return "-";
      const parts = String(path).split(/[\\\\/]/).filter(Boolean);
      return parts.slice(-2).join("/");
    }

    function pill(text, kind) {
      const span = document.createElement("span");
      span.className = `pill ${kind}`;
      span.textContent = text;
      return span;
    }

    function appendKv(parent, key, value, options = {}) {
      const row = document.createElement("div");
      row.className = "kv";
      const keyEl = document.createElement("div");
      keyEl.className = "k";
      keyEl.textContent = key;
      const valEl = document.createElement("div");
      valEl.className = `v ${options.mono ? "mono" : ""} ${options.small ? "small" : ""}`.trim();
      if (value instanceof Node) {
        valEl.append(value);
      } else {
        valEl.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
      }
      row.append(keyEl, valEl);
      parent.append(row);
    }

    function statusFor(row) {
      if (row.manual_review) return { kind: "status-review", label: "Review" };
      if (row.posted === "unknown") return { kind: "status-unknown", label: "Unknown" };
      if (row.dry_run) return { kind: "status-dry", label: "Dry run" };
      return { kind: "status-live", label: "Posted" };
    }

    function updateMetricClass(id, className) {
      const el = $(id);
      el.classList.remove("is-good", "is-warn", "is-bad");
      if (className) el.classList.add(className);
    }

    function renderMetrics(data) {
      const runtime = data.runtime;
      const positions = data.positions.summary;
      const health = data.health;
      const account = data.account || {};
      const isLive = !runtime.dry_run;
      const modeDotClass = isLive ? "is-live" : "is-dry";

      $("modeDot").className = `mode-dot ${modeDotClass}`;
      setText("modeText", isLive ? "Live trading" : "Dry run");
      setText("versionText", `v${data.version}`);
      setText("generatedAt", `Last refresh ${formatDate(data.generated_at)}`);

      const modeMetric = $("modeMetric");
      clearNode(modeMetric);
      const dot = document.createElement("span");
      dot.className = `mode-dot ${modeDotClass}`;
      dot.setAttribute("aria-hidden", "true");
      const modeLabel = document.createElement("span");
      modeLabel.textContent = runtime.dry_run ? "Dry run" : "Live";
      modeMetric.append(dot, modeLabel);
      setText("modeSub", String(runtime.clob_host || "").replace("https://", ""));

      setText("positionsMetric", positions.total);
      setText("positionsSub", `${positions.dry_run} dry run / ${positions.live} live`);
      setText("exposureMetric", formatMoney(positions.total_position_usd));
      setText("exposureSub", `${positions.yes_count} YES / ${positions.no_count} NO / PnL ${positions.pnl_count ? formatSignedMoney(positions.total_pnl_usd) : "-"}`);
      setText("reviewMetric", positions.manual_review);
      setText("reviewSub", `${positions.unknown_posted} unknown posted`);
      setText("activityMetric", health.activity_label);
      setText("activitySub", health.last_log_age_seconds === null ? health.detail : ageText(health.last_log_age_seconds));
      const hasAccountBalance = account.balance_usd !== null && account.balance_usd !== undefined && !Number.isNaN(Number(account.balance_usd));
      setText("accountMetric", hasAccountBalance ? formatMoney(account.balance_usd) : account.status_label);
      setText(
        "accountSub",
        account.status === "ok"
          ? account.balance_source === "wallet_usdc"
            ? `${account.wallet_token || "Wallet"} / CLOB ${formatMoney(account.clob_balance_usd)}`
            : `Allowance ${account.allowance_usd === null || account.allowance_usd === undefined ? "-" : formatMoney(account.allowance_usd)}`
          : account.error || account.status_label
      );

      updateMetricClass("reviewMetric", positions.manual_review > 0 ? "is-bad" : "");
      updateMetricClass("activityMetric", health.activity === "stale" ? "is-warn" : "");
      updateMetricClass(
        "accountMetric",
        account.status === "ok"
          ? Number(account.balance_usd) < Number(runtime.max_position_usd) ? "is-warn" : "is-good"
          : account.status === "disabled" ? "" : "is-warn"
      );
    }

    function renderRuntime(data) {
      const el = $("runtimeDetails");
      clearNode(el);
      const runtime = data.runtime;
      appendKv(el, "Model", `${runtime.model_name} / ${runtime.model_variant}`, { mono: true });
      appendKv(el, "Poll interval", `${runtime.poll_interval_seconds}s`, { mono: true });
      appendKv(el, "Offline retry", `${runtime.offline_retry_seconds}s`, { mono: true });
      appendKv(el, "Max position", formatMoney(runtime.max_position_usd), { mono: true });
      appendKv(el, "Live limit", runtime.live_market_limit === null ? "Full scan" : runtime.live_market_limit, { mono: true });
      appendKv(el, "NO side", pill(runtime.enable_no_side ? "Enabled" : "Disabled", runtime.enable_no_side ? "status-live" : "status-dry"));
      appendKv(el, "Reconcile", pill(runtime.reconcile_on_startup ? "On startup" : "Off", runtime.reconcile_on_startup ? "status-live" : "status-dry"));
      appendKv(el, "Market WS", pill(runtime.market_ws_enabled ? "Enabled" : "Disabled", runtime.market_ws_enabled ? "status-live" : "status-dry"));
      appendKv(el, "User WS", pill(runtime.user_ws_enabled ? "Enabled" : "Disabled", runtime.user_ws_enabled ? "status-live" : "status-dry"));
      appendKv(el, "Quote stale", `${runtime.ws_market_stale_seconds}s`, { mono: true });
      appendKv(el, "Safety check", `${runtime.safety_reconcile_interval_seconds}s`, { mono: true });
      appendKv(el, "Wallet balance TTL", `${runtime.wallet_balance_ttl_seconds}s`, { mono: true });
      appendKv(el, "Data dir", shortPath(runtime.data_dir), { mono: true, small: true });
      appendKv(el, "Log dir", shortPath(runtime.log_dir), { mono: true, small: true });
    }

    function renderAccount(data) {
      const el = $("accountDetails");
      clearNode(el);
      const account = data.account || {};
      const status = $("accountStatus");
      const statusKind = account.status === "ok" ? "status-live" : account.status === "disabled" ? "status-dry" : "status-review";
      status.className = `pill ${statusKind}`;
      status.textContent = account.status_label || "-";

      appendKv(el, "Account balance", formatMoney(account.balance_usd), { mono: true });
      if (account.wallet_balance_usd !== null && account.wallet_balance_usd !== undefined) {
        appendKv(el, `Wallet ${account.wallet_token || "USDC"}`, formatMoney(account.wallet_balance_usd), { mono: true });
      }
      appendKv(el, "CLOB balance", formatMoney(account.clob_balance_usd), { mono: true });
      appendKv(el, "CLOB allowance", formatMoney(account.clob_allowance_usd), { mono: true });
      appendKv(el, "Balance source", account.balance_source || "-", { mono: true });
      if (account.funder_address) appendKv(el, "Funder", account.funder_address, { mono: true });
      if (account.signer_address) appendKv(el, "Signer", account.signer_address, { mono: true });
      if (account.signature_type) appendKv(el, "Signature type", account.signature_type, { mono: true });
      appendKv(el, "Status", account.status || "-", { mono: true });
      appendKv(el, "Updated", formatCompactDate(account.updated_at));
      if (account.warning) appendKv(el, "Warning", account.warning, { small: true });
      if (account.wallet_error) appendKv(el, "Wallet error", account.wallet_error, { small: true });
      if (account.error) appendKv(el, "Error", account.error, { small: true });
    }

    function renderArtifacts(data) {
      const el = $("artifactList");
      clearNode(el);
      data.artifacts.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const name = document.createElement("code");
        name.className = "mono";
        name.textContent = item.name;
        const meta = document.createElement("div");
        meta.className = "list-meta";
        const size = document.createElement("span");
        size.className = "muted small mono";
        size.textContent = formatBytes(item.size_bytes);
        meta.append(size, pill(item.exists ? "OK" : "Missing", item.exists ? "status-live" : "status-review"));
        row.append(name, meta);
        el.append(row);
      });
    }

    function renderEnvironment(data) {
      const el = $("envList");
      clearNode(el);
      const env = data.environment;
      const status = $("environmentStatus");
      status.className = `pill ${env.live_credentials_ready ? "status-live" : "status-review"}`;
      status.textContent = env.live_credentials_ready ? "Live ready" : `${env.missing_required.length} missing`;

      env.variables.forEach((item) => {
        const row = document.createElement("div");
        row.className = "list-row";
        const name = document.createElement("code");
        name.className = "mono";
        name.textContent = item.name;
        const kind = item.required_for_live && !item.present ? "status-review" : item.present ? "status-live" : "status-dry";
        row.append(name, pill(item.present ? "Set" : item.required_for_live ? "Missing" : "Unset", kind));
        el.append(row);
      });
    }

    function positionMatches(row) {
      const search = $("positionSearch").value.trim().toLowerCase();
      const mode = $("positionMode").value;
      if (mode === "dry" && !row.dry_run) return false;
      if (mode === "live" && row.dry_run) return false;
      if (state.side !== "all" && row.side !== state.side) return false;
      if (!search) return true;
      return [
        row.market_id,
        row.question,
        row.city,
        row.target_date,
        row.side,
        row.token_id,
        row.posted,
        row.reconciliation_status,
      ].some((value) => String(value || "").toLowerCase().includes(search));
    }

    function compareRows(a, b) {
      const av = a[state.sortBy];
      const bv = b[state.sortBy];
      if (av === null || av === undefined || av === "") return 1;
      if (bv === null || bv === undefined || bv === "") return -1;
      if (av < bv) return state.sortDir === "asc" ? -1 : 1;
      if (av > bv) return state.sortDir === "asc" ? 1 : -1;
      return 0;
    }

    function updateSortHeaders() {
      document.querySelectorAll("th[data-sort]").forEach((th) => {
        th.classList.toggle("is-sorted", th.dataset.sort === state.sortBy);
        const old = th.querySelector(".caret");
        if (old) old.remove();
        if (th.dataset.sort === state.sortBy) {
          const caret = document.createElement("span");
          caret.className = "caret";
          caret.textContent = state.sortDir === "asc" ? "^" : "v";
          th.append(caret);
        }
      });
    }

    function setSideFilter(side) {
      state.side = side;
      document.querySelectorAll("[data-side]").forEach((button) => {
        button.classList.toggle("on", button.dataset.side === side);
        button.classList.toggle("yes", button.dataset.side === "YES" && button.dataset.side === side);
        button.classList.toggle("no", button.dataset.side === "NO" && button.dataset.side === side);
      });
      if (state.data) renderPositions(state.data);
    }

    function appendCell(row, child, className = "") {
      const cell = document.createElement("td");
      if (className) cell.className = className;
      if (child instanceof Node) cell.append(child);
      else cell.textContent = child === null || child === undefined || child === "" ? "-" : String(child);
      row.append(cell);
      return cell;
    }

    function pnlNode(row) {
      const pnl = Number(row.pnl_usd);
      if (row.pnl_usd === null || row.pnl_usd === undefined || Number.isNaN(pnl)) return "-";
      const wrapper = document.createElement("div");
      wrapper.className = "pnl-cell";
      const money = document.createElement("span");
      money.className = pnl >= 0 ? "edge-pos" : "edge-neg";
      money.textContent = formatSignedMoney(row.pnl_usd);
      const pct = document.createElement("small");
      pct.textContent = formatSignedPct(row.pnl_pct);
      wrapper.append(money, pct);
      return wrapper;
    }

    function svgNode(name, attrs = {}) {
      const node = document.createElementNS("http://www.w3.org/2000/svg", name);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
      return node;
    }

    function chartPath(points) {
      return points.map((point, index) => `${index ? "L" : "M"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
    }

    function renderPnlChart(data) {
      const curve = (data.positions.pnl_curve || [])
        .map((point) => ({
          ...point,
          value: Number(point.cumulative_pnl_usd),
          pnl: Number(point.pnl_usd),
        }))
        .filter((point) => !Number.isNaN(point.value));
      const svg = $("pnlChart");
      const empty = $("pnlChartEmpty");
      const badge = $("pnlChartBadge");
      clearNode(svg);

      if (!curve.length) {
        setText("pnlChartMeta", "0 marked positions");
        setText("pnlChartLatest", "-");
        setText("pnlChartHigh", "-");
        setText("pnlChartLow", "-");
        setText("pnlChartMarked", "0");
        badge.className = "pill status-dry";
        badge.textContent = "No PnL";
        empty.hidden = false;
        return;
      }

      const values = curve.map((point) => point.value);
      const latest = curve[curve.length - 1].value;
      const high = Math.max(...values);
      const low = Math.min(...values);
      const first = curve[0];
      const last = curve[curve.length - 1];
      setText("pnlChartMeta", `${curve.length} marked positions / ${formatCompactDate(first.entry_time)} to ${formatCompactDate(last.entry_time)}`);
      setText("pnlChartLatest", formatSignedMoney(latest));
      setText("pnlChartHigh", formatSignedMoney(high));
      setText("pnlChartLow", formatSignedMoney(low));
      setText("pnlChartMarked", curve.length);
      badge.className = `pill ${latest >= 0 ? "status-live" : "status-review"}`;
      badge.textContent = formatSignedMoney(latest);
      empty.hidden = true;

      const width = 640;
      const height = 220;
      const pad = { left: 16, right: 16, top: 16, bottom: 18 };
      let minY = Math.min(0, low);
      let maxY = Math.max(0, high);
      if (minY === maxY) {
        minY -= 1;
        maxY += 1;
      } else {
        const margin = (maxY - minY) * 0.12;
        minY -= margin;
        maxY += margin;
      }
      const innerW = width - pad.left - pad.right;
      const innerH = height - pad.top - pad.bottom;
      const xFor = (index) => curve.length === 1 ? width / 2 : pad.left + (index / (curve.length - 1)) * innerW;
      const yFor = (value) => pad.top + ((maxY - value) / (maxY - minY)) * innerH;
      const coords = curve.map((point, index) => ({ ...point, x: xFor(index), y: yFor(point.value) }));
      const zeroY = Math.max(pad.top, Math.min(height - pad.bottom, yFor(0)));

      [0.25, 0.5, 0.75].forEach((fraction) => {
        const y = pad.top + fraction * innerH;
        svg.append(svgNode("line", { class: "pnl-chart-grid", x1: pad.left, y1: y, x2: width - pad.right, y2: y }));
      });
      svg.append(svgNode("line", { class: "pnl-chart-zero", x1: pad.left, y1: zeroY, x2: width - pad.right, y2: zeroY }));

      if (coords.length > 1) {
        const lineD = chartPath(coords);
        const areaD = `M ${coords[0].x.toFixed(2)} ${zeroY.toFixed(2)} ${lineD.slice(1)} L ${coords[coords.length - 1].x.toFixed(2)} ${zeroY.toFixed(2)} Z`;
        svg.append(svgNode("path", { class: `pnl-chart-area ${latest < 0 ? "neg" : ""}`.trim(), d: areaD }));
        svg.append(svgNode("path", { class: `pnl-chart-line ${latest < 0 ? "neg" : ""}`.trim(), d: lineD }));
      }

      const pointStep = Math.max(1, Math.ceil(coords.length / 80));
      coords.forEach((point, index) => {
        const isLast = index === coords.length - 1;
        if (!isLast && index % pointStep !== 0) return;
        const circle = svgNode("circle", {
          class: `pnl-chart-point ${point.value < 0 ? "neg" : ""}`.trim(),
          cx: point.x.toFixed(2),
          cy: point.y.toFixed(2),
          r: isLast ? 4 : 2.5,
        });
        const title = svgNode("title");
        title.textContent = `${formatCompactDate(point.entry_time)} / ${point.market_id || "-"} / ${formatSignedMoney(point.pnl)} / cum ${formatSignedMoney(point.value)}`;
        circle.append(title);
        svg.append(circle);
      });
    }

    function renderPositions(data) {
      const body = $("positionsBody");
      clearNode(body);
      setError("positionsError", data.positions.error);
      const sourceRows = data.positions.recent || [];
      const rows = sourceRows.filter(positionMatches).sort(compareRows);
      const maxEdge = Math.max(...sourceRows.map((row) => Math.abs(Number(row.edge || 0))), 0.001);
      const markMeta = data.positions.mark_count ? ` / ${data.positions.mark_count} marks` : "";
      $("positionsCount").textContent = `${rows.length} of ${sourceRows.length} shown${markMeta}`;
      $("positionsEmpty").hidden = rows.length > 0;
      updateSortHeaders();

      rows.forEach((row, index) => {
        const tr = document.createElement("tr");
        const status = statusFor(row);
        if (status.kind === "status-review") tr.classList.add("row-flag");
        tr.addEventListener("click", () => openDrawer(row));

        const question = document.createElement("div");
        question.className = "question-cell";
        const title = document.createElement("div");
        title.className = "question-title";
        title.textContent = row.question || row.market_id || "-";
        const meta = document.createElement("div");
        meta.className = "question-meta";
        [row.city, row.target_date, row.market_id].filter(Boolean).forEach((value, idx) => {
          if (idx) {
            const sep = document.createElement("span");
            sep.className = "dot-sep";
            sep.textContent = "/";
            meta.append(sep);
          }
          const item = document.createElement(idx === 2 ? "code" : "span");
          item.textContent = value;
          meta.append(item);
        });
        question.append(title, meta);
        appendCell(tr, question);
        appendCell(tr, pill(row.side || "-", row.side === "NO" ? "no" : "yes"));
        appendCell(tr, formatDecimal(row.entry_price), "num t-right");
        appendCell(tr, formatPct(row.forecast_prob), "num t-right");

        const edgeCell = document.createElement("div");
        edgeCell.className = "edge-cell";
        const bar = document.createElement("span");
        bar.className = "edge-bar";
        const fill = document.createElement("i");
        const edge = Number(row.edge || 0);
        fill.style.width = `${Math.min(100, Math.abs(edge / maxEdge) * 100)}%`;
        fill.style.background = edge >= 0 ? (row.side === "NO" ? "var(--amber)" : "var(--teal)") : "var(--red)";
        bar.append(fill);
        const edgeText = document.createElement("span");
        edgeText.className = edge >= 0 ? "edge-pos" : "edge-neg";
        edgeText.textContent = formatSignedPct(row.edge);
        edgeCell.append(bar, edgeText);
        appendCell(tr, edgeCell, "num t-right");

        appendCell(tr, pnlNode(row), "num t-right");
        appendCell(tr, formatMoney(row.position_usd), "num t-right");
        appendCell(tr, formatDecimal(row.shares, 2), "num t-right");
        appendCell(tr, pill(status.label, status.kind));
        appendCell(tr, formatCompactDate(row.entry_time), "num t-right muted");
        body.append(tr);
      });
    }

    function renderLogMessage(message) {
      const fragment = document.createDocumentFragment();
      const match = String(message || "").match(/^([a-z][a-z0-9_:]*)(\\s+)(.*)$/);
      if (!match) {
        fragment.append(document.createTextNode(message || ""));
        return fragment;
      }
      const key = document.createElement("span");
      key.className = match[1].startsWith("skip:") ? "skip-key" : "event-key";
      key.textContent = match[1];
      const space = document.createTextNode(match[2]);
      const rest = document.createElement("span");
      rest.className = "arg";
      rest.textContent = match[3];
      fragment.append(key, space, rest);
      return fragment;
    }

    function logMatches(entry) {
      const level = $("logLevel").value;
      const entryLevel = String(entry.level || "").toUpperCase();
      if (level === "warning") return ["WARNING", "ERROR", "CRITICAL"].includes(entryLevel);
      if (level === "error") return ["ERROR", "CRITICAL"].includes(entryLevel);
      return true;
    }

    function renderLogs(data) {
      const el = $("logList");
      clearNode(el);
      setError("logsError", data.logs.error);
      const entries = data.logs.entries || [];
      const filtered = entries.filter(logMatches);
      const counts = entries.reduce((acc, entry) => {
        const level = String(entry.level || "INFO").toUpperCase();
        acc[level] = (acc[level] || 0) + 1;
        return acc;
      }, {});
      $("infoCount").textContent = counts.INFO || 0;
      $("warnCount").textContent = counts.WARNING || 0;
      $("errorCount").textContent = (counts.ERROR || 0) + (counts.CRITICAL || 0);
      $("logsMeta").textContent = `tail / live_bot.log / ${entries.length} lines`;
      $("logsEmpty").hidden = filtered.length > 0;

      filtered.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "log-row";
        const timestamp = document.createElement("span");
        timestamp.className = "log-ts";
        timestamp.textContent = entry.timestamp || "";
        const level = document.createElement("span");
        const levelText = String(entry.level || "INFO").toUpperCase();
        level.className = `log-lvl lvl-${levelText.toLowerCase()}`;
        level.textContent = levelText;
        const message = document.createElement("span");
        message.className = "log-msg";
        message.append(renderLogMessage(entry.message || entry.raw || ""));
        row.append(timestamp, level, message);
        el.append(row);
      });
    }

    function drawerStat(label, value, className = "") {
      const stat = document.createElement("div");
      stat.className = "drawer-stat";
      const l = document.createElement("div");
      l.className = "l";
      l.textContent = label;
      const v = document.createElement("div");
      v.className = `v ${className}`.trim();
      v.textContent = value;
      stat.append(l, v);
      return stat;
    }

    function openDrawer(row) {
      $("drawerTitle").textContent = row.question || row.market_id || "Position";
      $("drawerMeta").textContent = [row.city, row.target_date, row.market_id].filter(Boolean).join(" / ");
      const pills = $("drawerPills");
      clearNode(pills);
      pills.append(pill(row.side || "-", row.side === "NO" ? "no" : "yes"));
      const status = statusFor(row);
      pills.append(pill(status.label, status.kind));
      if (row.manual_review) pills.append(pill("Manual review", "status-review"));

      const stats = $("drawerStats");
      clearNode(stats);
      stats.append(
        drawerStat("Entry price", formatDecimal(row.entry_price)),
        drawerStat("Forecast prob", formatPct(row.forecast_prob)),
        drawerStat("Edge", formatSignedPct(row.edge), Number(row.edge || 0) >= 0 ? "edge-pos" : "edge-neg"),
        drawerStat("PnL", formatSignedMoney(row.pnl_usd), Number(row.pnl_usd || 0) >= 0 ? "edge-pos" : "edge-neg"),
        drawerStat("Position", formatMoney(row.position_usd)),
        drawerStat("Current price", formatDecimal(row.current_price)),
        drawerStat("Shares", formatDecimal(row.shares, 2))
      );

      const details = $("drawerDetails");
      clearNode(details);
      appendKv(details, "Token ID", row.token_id, { mono: true, small: true });
      appendKv(details, "Market price", formatDecimal(row.market_price), { mono: true });
      appendKv(details, "Recorded position", formatMoney(row.recorded_position_usd), { mono: true });
      appendKv(details, "Current value", formatMoney(row.current_value_usd), { mono: true });
      appendKv(details, "PnL source", row.pnl_source || "-", { mono: true });
      appendKv(details, "Posted", row.posted || "-", { mono: true });
      appendKv(details, "Reconciliation", row.reconciliation_status || "-", { mono: true, small: true });
      appendKv(details, "Entered", formatCompactDate(row.entry_time));
      $("drawerBackdrop").hidden = false;
    }

    function closeDrawer() {
      $("drawerBackdrop").hidden = true;
    }

    function render(data) {
      state.data = data;
      setError("errorBanner", "");
      renderMetrics(data);
      renderRuntime(data);
      renderAccount(data);
      renderArtifacts(data);
      renderEnvironment(data);
      renderPnlChart(data);
      renderPositions(data);
      renderLogs(data);
    }

    async function refresh() {
      try {
        const response = await fetch("/api/status?log_lines=160", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        setError("errorBanner", `Dashboard refresh failed: ${error.message}`);
      }
    }

    $("refreshButton").addEventListener("click", refresh);
    $("positionSearch").addEventListener("input", () => state.data && renderPositions(state.data));
    $("positionMode").addEventListener("change", () => state.data && renderPositions(state.data));
    $("logLevel").addEventListener("change", () => state.data && renderLogs(state.data));
    $("drawerClose").addEventListener("click", closeDrawer);
    $("drawerBackdrop").addEventListener("click", (event) => {
      if (event.target === $("drawerBackdrop")) closeDrawer();
    });
    document.querySelectorAll("[data-side]").forEach((button) => {
      button.addEventListener("click", () => setSideFilter(button.dataset.side));
    });
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        if (state.sortBy === th.dataset.sort) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortBy = th.dataset.sort;
          state.sortDir = "desc";
        }
        if (state.data) renderPositions(state.data);
      });
    });

    setInterval(() => {
      if ($("autoRefresh").checked) refresh();
    }, 15000);
    refresh();
  </script>
</body>
</html>
"""
