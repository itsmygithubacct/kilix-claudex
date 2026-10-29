#!/usr/bin/env python3
"""Local, read-only token-limit dashboard for claudex.

The server binds to loopback only and has no mutating endpoints:

    GET /             self-contained page (inline script and style under a nonce CSP)
    GET /api/limits   the cached report as JSON
    GET /api/health   liveness probe, with page/limits/health request counts

Requests whose Host header isn't a loopback name are refused, which blocks
DNS-rebinding pages from reading the report. The page builds every piece of
report text with ``textContent``, never as HTML.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from providers import LABELS, USAGE_PAGES

PROVIDER_PAGES = USAGE_PAGES


def provider_links() -> list[dict]:
    return [{"id": key, "label": f"{LABELS[key]} usage page", "url": url} for key, url in USAGE_PAGES.items()]


LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]"}


class CachedReport:
    """Build the report at most once per ``ttl`` seconds, whoever asks."""

    def __init__(self, build, ttl: float = 15.0):
        self.build = build
        self.ttl = ttl
        self.lock = threading.Lock()
        self.value = None
        self.built_at = 0.0

    def get(self) -> dict:
        with self.lock:
            if self.value is None or time.monotonic() - self.built_at >= self.ttl:
                try:
                    self.value = {"ok": True, "generated_at": time.time(), **self.build()}
                except Exception as error:  # the page shows the failure instead of dying
                    self.value = {"ok": False, "generated_at": time.time(),
                                  "error": f"{type(error).__name__}: {error}"}
                self.built_at = time.monotonic()
            return self.value


def _host_name(header: str) -> str:
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.split(":", 1)[0]


def make_handler(report: CachedReport):
    counts = {"page": 0, "limits": 0, "health": 0}
    lock = threading.Lock()
    started = time.time()

    def count(name: str) -> None:
        with lock:
            counts[name] += 1

    class Handler(BaseHTTPRequestHandler):
        server_version = "claudex-dashboard"

        def log_message(self, format, *args):  # noqa: A002 - quiet by default
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra=None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _text(self, status: int, text: str, extra=None) -> None:
            self._send(status, (text + "\n").encode(), "text/plain; charset=utf-8", extra)

        def do_GET(self):
            if _host_name(self.headers.get("Host", "")) not in LOOPBACK_NAMES:
                return self._text(403, "forbidden host")
            path = urlsplit(self.path).path
            if path == "/api/health":
                count("health")
                with lock:  # request counts show whether a browser page is polling
                    body = json.dumps({"ok": True, "started_at": started, "requests": dict(counts)})
                return self._send(200, (body + "\n").encode(), "application/json")
            if path == "/api/limits":
                count("limits")
                body = json.dumps(report.get(), ensure_ascii=False).encode()
                return self._send(200, body, "application/json; charset=utf-8")
            if path == "/":
                count("page")
                nonce = secrets.token_urlsafe(18)
                policy = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                          "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                          "form-action 'none'; frame-ancestors 'none'")
                return self._send(200, PAGE.replace("{{NONCE}}", nonce).encode(),
                                  "text/html; charset=utf-8", {"Content-Security-Policy": policy})
            return self._text(404, "not found")

        do_HEAD = do_GET

        def _read_only(self):
            self._text(405, "read-only dashboard", {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only

    return Handler


def make_server(build, port: int, *, host: str = "127.0.0.1", ttl: float = 15.0) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the dashboard only binds to loopback")
    server = ThreadingHTTPServer((host, port), make_handler(CachedReport(build, ttl)))
    server.daemon_threads = True
    return server


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claudex limits</title>
<style nonce="{{NONCE}}">
:root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --dim:#667085; --line:#d9dde5;
        --ok:#2e8b57; --low:#c98a00; --out:#c0392b; --unk:#7a8394; --bar:#e8ebf0; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151b; --card:#1b1f27; --ink:#e6e9ef; --dim:#98a2b3;
        --line:#2c323d; --bar:#2a303a; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui, sans-serif; }
header { display:flex; flex-wrap:wrap; gap:.6rem 1rem; align-items:center; padding:1rem 1.25rem;
         border-bottom:1px solid var(--line); }
h1 { font-size:1.15rem; margin:0; margin-right:auto; }
a.page, button { color:var(--ink); background:var(--card); border:1px solid var(--line); border-radius:6px;
         padding:.35rem .7rem; font:inherit; text-decoration:none; cursor:pointer; }
main { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:1rem; padding:1.25rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem; }
.top { display:flex; justify-content:space-between; gap:.5rem; align-items:baseline; }
.name { font-weight:600; }
.badge { border-radius:999px; padding:.1rem .6rem; font-size:.8rem; font-weight:600; color:#fff; }
.exhausted { background:var(--out); } .low { background:var(--low); } .ok { background:var(--ok); }
.unknown { background:var(--unk); }
.dim { color:var(--dim); font-size:.85rem; }
.window { margin-top:.9rem; }
.row { display:flex; justify-content:space-between; gap:.5rem; }
.bar { height:10px; background:var(--bar); border-radius:5px; overflow:hidden; margin:.3rem 0; }
.fill { height:100%; }
svg.spark { width:100%; height:44px; display:block; margin-top:.3rem; }
ul.events { margin:.5rem 0 0; padding-left:1.1rem; }
footer { padding:0 1.25rem 1.25rem; }
</style>
</head>
<body>
<header>
  <h1>claudex token limits</h1>
  <span class="dim" id="stamp">loading…</span>
  <button id="refresh" type="button">Refresh</button>
  <span id="pages"></span>
</header>
<main id="cards"></main>
<footer class="dim">Local files and pane screens only · weekly, five-hour and monthly windows where the agent reports them ·
  unknown never blocks sends</footer>
<script nonce="{{NONCE}}">
"use strict";
const $ = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls;
  if (text !== undefined && text !== null) el.textContent = String(text); return el; };
const duration = (s) => { if (s === null || s === undefined) return "unknown"; s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; };
const color = (used) => used >= 100 ? "var(--out)" : used >= 90 ? "var(--low)" : "var(--ok)";

function sparkline(points) {
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "spark"); svg.setAttribute("viewBox", "0 0 300 44"); svg.setAttribute("preserveAspectRatio", "none");
  if (!points || points.length < 2) return svg;
  const t0 = points[0][0], t1 = points[points.length - 1][0], span = Math.max(1, t1 - t0);
  const path = points.map((p, i) => `${i ? "L" : "M"}${(300 * (p[0] - t0) / span).toFixed(1)},${(42 - 40 * Math.min(100, p[1]) / 100).toFixed(1)}`).join(" ");
  const line = document.createElementNS(ns, "path");
  line.setAttribute("d", path); line.setAttribute("fill", "none"); line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "1.5"); svg.appendChild(line);
  return svg;
}

function windowBlock(name, w) {
  const box = $("div", "window"), row = $("div", "row");
  row.append($("span", "name", name.replace("_", " ")));
  if (w.used_percent !== null && w.used_percent !== undefined) {
    row.append($("span", null, `${w.used_percent}% used · ${w.remaining_percent}% left`));
    box.append(row);
    const bar = $("div", "bar"), fill = $("div", "fill");
    fill.style.width = `${Math.min(100, w.used_percent)}%`; fill.style.background = color(w.used_percent);
    bar.append(fill); box.append(bar);
  } else {
    row.append($("span", null, w.status ? `status ${w.status}` : `under ${w.remaining_below_percent}% left (screen)`));
    box.append(row);
  }
  const facts = [];
  if (w.superseded_by_success_at) facts.push("a later request succeeded");
  else if (w.reset_since_observed) facts.push("reset since observed");
  else if (w.resets_in_seconds !== null && w.resets_in_seconds !== undefined) facts.push(`resets in ${duration(w.resets_in_seconds)}`);
  if (w.burn_percent_per_hour !== null && w.burn_percent_per_hour !== undefined) facts.push(`burn ${w.burn_percent_per_hour}%/h`);
  else if (w.history) facts.push("no recent burn (idle)");
  if (w.average_percent_per_hour !== null && w.average_percent_per_hour !== undefined) facts.push(`window average ${w.average_percent_per_hour}%/h`);
  if (w.projection && w.projection.exhausts_in_seconds !== null && w.projection.exhausts_in_seconds !== undefined) {
    facts.push(w.projection.exhausts_before_reset ? `runs out in ${duration(w.projection.exhausts_in_seconds)}, before reset`
                                                  : `lasts until reset at this rate`);
  }
  facts.push(`source: ${w.source || "unknown"}`);
  box.append($("div", "dim", facts.join(" · ")));
  if (w.history && w.history.length > 1) box.append(sparkline(w.history));
  return box;
}

function card(p) {
  const el = $("section", "card"), top = $("div", "top");
  top.append($("span", "name", `${p.title || p.target} · pane ${p.pane_id ?? "?"} · ${p.agent || "?"}`));
  top.append($("span", `badge ${p.state || "unknown"}`, p.state || "unknown"));
  el.append(top, $("div", "dim", p.reason || ""));
  if (p.limit_id) el.append($("div", "dim", `limit bucket ${p.limit_id}${p.plan_type ? ` · plan ${p.plan_type}` : ""}${p.age_seconds !== undefined && p.age_seconds !== null ? ` · newest evidence ${duration(p.age_seconds)} old` : ""}`));
  if (p.model) el.append($("div", "dim", `model ${p.model}${p.detected_from ? ` · detected from ${p.detected_from}` : ""}`));
  if (p.last_success_at) el.append($("div", "dim", `last successful request ${duration(Date.now() / 1000 - p.last_success_at)} ago`));
  for (const [name, w] of Object.entries(p.windows || {})) el.append(windowBlock(name, w));
  if ((p.not_reported || []).length && Object.keys(p.windows || {}).length) el.append($("div", "dim", `not reported: ${p.not_reported.join(", ")}`));
  if (p.note) el.append($("div", "dim", p.note));
  if (p.events && p.events.length) {
    const list = $("ul", "events dim");
    for (const e of p.events.slice(0, 8)) list.append($("li", null, `${new Date(e.observed_at * 1000).toLocaleString()}: ${e.limit_type} ${e.status}`));
    el.append($("div", "dim", "recent limit events"), list);
  }
  return el;
}

async function load() {
  const stamp = document.getElementById("stamp"), cards = document.getElementById("cards");
  try {
    const response = await fetch("/api/limits", {cache: "no-store"});
    const data = await response.json();
    cards.replaceChildren();
    if (!data.ok) { cards.append($("section", "card", `report failed: ${data.error}`)); }
    for (const pane of data.panes || []) cards.append(card(pane));
    const pages = document.getElementById("pages");
    pages.replaceChildren();
    for (const link of data.providers || []) {
      const a = $("a", "page", link.label);
      if (/^https:\/\//.test(link.url)) a.href = link.url;
      pages.append(a);
    }
    stamp.textContent = `updated ${new Date(data.generated_at * 1000).toLocaleTimeString()}`;
  } catch (error) {
    stamp.textContent = `update failed: ${error}`;
  }
}
document.getElementById("refresh").addEventListener("click", load);
load();
setInterval(load, 30000);
</script>
</body>
</html>
"""
