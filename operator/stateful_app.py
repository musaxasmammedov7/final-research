#!/usr/bin/env python3
import http.server
import json
import os
import socket
import threading
import time
import urllib.request

STATE = {
    "session_ticks": 0,
    "video_frame": 1000,
    "started_at": time.strftime("%H:%M:%S"),
    "pid": os.getpid(),
}

# Выделенный буфер в оперативной памяти (16 МБ RAM), который переносится через CRIU Pre-Copy + Dump
MEMORY_BUFFER = bytearray(16 * 1024 * 1024)

def read_current_node():
    try:
        with open("/etc/host_nodename", "r") as f:
            return f.read().strip()
    except Exception:
        return socket.gethostname()

def read_last_metrics():
    try:
        if os.path.exists("/var/run/migration_metrics.json"):
            with open("/var/run/migration_metrics.json", "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def background_loop():
    idx = 0
    while True:
        STATE["session_ticks"] += 1
        STATE["video_frame"] += 1
        MEMORY_BUFFER[idx % len(MEMORY_BUFFER)] = STATE["session_ticks"] % 256
        idx += 4096
        time.sleep(0.1)

threading.Thread(target=background_loop, daemon=True).start()

HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>CRIU + Cilium eBPF Live Migration Lab</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; background: #0b0f19; color: #f8fafc; margin: 0; padding: 24px; }
    .container { max-width: 960px; margin: 0 auto; }
    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
    .badge { background: #0284c7; color: #fff; padding: 6px 12px; border-radius: 6px; font-weight: 600; font-size: 13px; }
    .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-top: 20px; }
    .card { background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 18px; }
    .label { font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }
    .val { font-size: 26px; font-weight: 700; margin-top: 8px; font-family: ui-monospace, monospace; color: #38bdf8; }
    .val.green { color: #4ade80; }
    .val.amber { color: #fbbf24; }
    .actions { margin-top: 24px; background: #1e293b; padding: 20px; border-radius: 10px; display: flex; gap: 16px; align-items: center; justify-content: space-between; }
    button { background: #2563eb; color: white; border: none; padding: 12px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 14px; }
    button:hover { background: #1d4ed8; }
    button.danger { background: #dc2626; }
    button.danger:hover { background: #b91c1c; }
    pre { background: #090d16; padding: 14px; border-radius: 8px; border: 1px solid #1e293b; color: #a5f3fc; font-size: 13px; overflow-x: auto; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h2 style="margin:0;">⚡ Стенд живой миграции контейнеров (CRIU + Cilium Service Mesh eBPF)</h2>
        <p style="margin:6px 0 0; color:#94a3b8; font-size:14px;">Непрерывная сессия в оперативной памяти (16 MB RAM) + маршрутизация Cilium eBPF</p>
      </div>
      <span class="badge">IP: 10.244.88.10 (Static /32 Route)</span>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label">Физический сервер (Текущая Нода)</div>
        <div class="val green" id="node_name">...</div>
      </div>
      <div class="card">
        <div class="label">Счетчик сессии в RAM (Не сбрасывается!)</div>
        <div class="val" id="ticks">0</div>
      </div>
      <div class="card">
        <div class="label">Кадр живого видеопотока в RAM</div>
        <div class="val amber" id="frame">0</div>
      </div>
      <div class="card">
        <div class="label">PID процесса в ядре</div>
        <div class="val" id="pid">...</div>
      </div>
      <div class="card">
        <div class="label">Макс. пауза клиента (Measured Freeze)</div>
        <div class="val green" id="max_gap">0 ms</div>
      </div>
      <div class="card">
        <div class="label">Статус потока</div>
        <div class="val green" id="status">ONLINE</div>
      </div>
    </div>

    <div class="actions">
      <div>
        <div style="font-weight:600;">Управление экспериментом (Реальный вызов CRIU на ноде)</div>
        <div style="font-size:13px; color:#94a3b8; margin-top:4px;">Выполняет: <code>criu pre-dump</code> &rarr; <code>criu dump --tcp-established</code> &rarr; <code>rsync</code> &rarr; <code>criu restore</code></div>
      </div>
      <div style="display:flex; gap:12px;">
        <button onclick="triggerMigration()">🚀 Мигрировать на другой сервер (CRIU + eBPF)</button>
        <button class="danger" onclick="triggerReset()">💥 Обычный рестарт K8s (Baseline: Потеря RAM)</button>
      </div>
    </div>

    <div class="card" style="margin-top:20px;">
      <div class="label">Протокол последнего замера CRIU на сервере (Реальные миллисекунды и КБ из ядра):</div>
      <pre id="metrics_log">Ожидание запуска миграции...</pre>
    </div>
  </div>

  <script>
    let lastTs = performance.now();
    let maxGap = 0;

    async function pollState() {
      try {
        const res = await fetch('/live/state', { cache: 'no-store' });
        const data = await res.json();
        const now = performance.now();
        const gap = Math.round(now - lastTs);
        if (gap > maxGap && lastTs > 0) maxGap = gap;
        lastTs = now;

        document.getElementById('node_name').innerText = data.current_node;
        document.getElementById('ticks').innerText = data.session_ticks;
        document.getElementById('frame').innerText = '#' + data.video_frame;
        document.getElementById('pid').innerText = 'PID ' + data.pid + ' (since ' + data.started_at + ')';
        document.getElementById('max_gap').innerText = maxGap + ' ms';
        document.getElementById('status').innerText = 'STREAMING (OK)';
        document.getElementById('status').style.color = '#4ade80';
        if (data.last_metrics) {
          document.getElementById('metrics_log').innerText = JSON.stringify(data.last_metrics, null, 2);
        }
      } catch (e) {
        document.getElementById('status').innerText = 'MIGRATING...';
        document.getElementById('status').style.color = '#fbbf24';
      }
      setTimeout(pollState, 120);
    }

    async function triggerMigration() {
      maxGap = 0;
      document.getElementById('metrics_log').innerText = 'Выполняется criu pre-dump -> criu dump -> перенос на соседний воркер...';
      fetch('/live/migrate', { method: 'POST' }).catch(() => {});
    }

    async function triggerReset() {
      maxGap = 0;
      fetch('/live/reset', { method: 'POST' }).catch(() => {});
    }

    pollState();
  </script>
</body>
</html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path.startswith("/live/state"):
            payload = json.dumps({
                "current_node": read_current_node(),
                "session_ticks": STATE["session_ticks"],
                "video_frame": STATE["video_frame"],
                "pid": STATE["pid"],
                "started_at": STATE["started_at"],
                "last_metrics": read_last_metrics(),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        if self.path.startswith("/live/migrate"):
            self.send_response(202)
            self.end_headers()
            self.wfile.write(b'{"status":"migrating"}')
            try:
                req = urllib.request.Request("http://10.244.88.1:9099/trigger", method="POST")
                urllib.request.urlopen(req, timeout=1)
            except Exception:
                pass
        elif self.path.startswith("/live/reset"):
            STATE["session_ticks"] = 0
            STATE["video_frame"] = 1000
            STATE["started_at"] = time.strftime("%H:%M:%S")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"reset"}')

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    server.serve_forever()
