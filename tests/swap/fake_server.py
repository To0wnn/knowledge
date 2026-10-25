# Stand-in for the knowledge server in tests/swap/run.sh: ready after a few seconds, drained by state/drain-<instance>,
# and every answer names the instance and its start time (so the test sees which server answered).
import http.server, os, time
START, NAME = time.time(), os.environ["KNOWLEDGE_INSTANCE"]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        ready = time.time() - START > 4 and not os.path.exists(f"/state/drain-{NAME}")
        code = 503 if "ready=1" in self.path and not ready else 200
        self.send_response(code); self.end_headers(); self.wfile.write(f"{NAME} {START:.0f}".encode())
http.server.ThreadingHTTPServer(("", 8766), H).serve_forever()
