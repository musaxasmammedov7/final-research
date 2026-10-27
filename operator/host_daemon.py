#!/usr/bin/env python3
import http.server
import subprocess
import threading
import time

def run_migration():
    time.sleep(0.05)
    subprocess.run(["/opt/criu-operator/migrate_now.sh"], check=False)

class OperatorHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        if self.path == "/trigger":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            threading.Thread(target=run_migration, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("10.244.88.1", 9099), OperatorHandler)
    server.serve_forever()
