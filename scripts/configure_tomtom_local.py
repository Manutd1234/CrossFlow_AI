#!/usr/bin/env python3
"""Temporary loopback-only form for an owner to enter existing TomTom keys."""

import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".env.tomtom.local"
CSRF = secrets.token_urlsafe(32)
KEY = re.compile(r"^[A-Za-z0-9_.-]{8,512}$")


class Handler(BaseHTTPRequestHandler):
    server_version = "CrossFlowLocalSetup"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def send_page(self, body, code=200):
        data = ("<!doctype html><html><head><meta charset=utf-8><title>CrossFlow local TomTom setup</title>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<style>body{font:17px system-ui;background:#f6f7f2;color:#193c42;max-width:660px;margin:7vh auto;padding:24px}"
                "form{background:white;padding:28px;border:1px solid #cdddda;border-radius:18px}label{display:block;margin-top:24px}"
                "input{display:block;width:94%;padding:14px;margin:10px 0;border:1px solid #9bb2b5;border-radius:8px;font:inherit}"
                "button{margin-top:24px;padding:14px;background:#146a6a;color:white;border:0;border-radius:8px;font:inherit}"
                "small{display:block;color:#47646a;line-height:1.6}</style></head><body>" + body + "</body></html>").encode()
        self.send_response(code)
        for name, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(data)),
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if self.path != "/" or not self.allowed_host():
            self.send_page("<h1>Unavailable</h1>", 404)
            return
        self.send_page("<h1>CrossFlow · Local TomTom setup</h1><p>Paste your existing key here and save it. "
                       "This page writes only to the ignored local server configuration.</p><form method=post action=/save>"
                       f"<input type=hidden name=csrf value='{html.escape(CSRF)}'>"
                       "<label for=flow>TomTom developer / Traffic Flow key</label><input id=flow name=flow type=password required autocomplete=off spellcheck=false>"
                       "<label for=stats>Separate MOVE / Traffic Stats key (optional)</label><input id=stats name=stats type=password autocomplete=off spellcheck=false>"
                       "<small>If you only have one key, leave the second field empty. We will test whether the first key has historical access. "
                       "No key value is returned to the browser after saving.</small><button type=submit>Save keys locally</button></form>"
                       "<p><small>Bound to 127.0.0.1 only. Temporary setup page expires after 15 minutes.</small></p>")

    def do_POST(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.path != "/save" or not self.allowed_host() or self.headers.get("Origin") != origin:
            self.send_page("<h1>Request rejected</h1>", 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True, max_num_fields=3, strict_parsing=True)
            if set(fields) != {"csrf", "flow", "stats"} or any(len(value) != 1 for value in fields.values()):
                raise ValueError
            if not secrets.compare_digest(fields["csrf"][0], CSRF):
                raise ValueError
            flow, stats = fields["flow"][0].strip(), fields["stats"][0].strip()
            if not KEY.fullmatch(flow) or (stats and not KEY.fullmatch(stats)):
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_page("<h1>Could not save</h1><p>Use an existing TomTom key with letters, digits, dots, underscores or hyphens. Return to the form to try again.</p>", 400)
            return
        temp_name = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".env.tomtom.local-", dir=ROOT)
            with os.fdopen(fd, "w") as file:
                file.write("# Server-only TomTom keys. Gitignored; never use VITE_ prefixes.\n")
                file.write("TOMTOM_API_KEY=" + flow + "\nTOMTOM_TRAFFIC_STATS_API_KEY=" + stats + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, TARGET)
            temp_name = None
        except OSError:
            self.send_page("<h1>Could not save</h1><p>The local configuration file could not be written. No key is displayed or logged.</p>", 503)
            return
        finally:
            if temp_name is not None:
                os.unlink(temp_name)
        self.send_page("<h1>TomTom keys saved locally</h1><p>Your key was saved to the ignored server configuration file. "
                       "Tell Codex it is saved so the access checks can continue. You can close this tab.</p>")
        threading.Thread(target=self.server.shutdown, daemon=True).start()


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 15
    deadline = threading.Timer(15 * 60, server.shutdown)
    deadline.daemon = True
    deadline.start()
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        deadline.cancel()
        server.server_close()
