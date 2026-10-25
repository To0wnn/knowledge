# Stand-in for the knowledge server in tests/swap/run.sh, with the same handover: it "loads" for a few seconds, then
# listens on the shared port with SO_REUSEPORT and writes state/listening-<instance>; on SIGTERM it stops listening
# (queued connections move to the other listener when net.ipv4.tcp_migrate_req=1) and finishes what it has.
import http.server, os, signal, threading, time
NAME, PORT = os.environ["KNOWLEDGE_INSTANCE"], int(os.environ.get("KNOWLEDGE_PORT", "18766"))
time.sleep(4)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(NAME.encode())
class S(http.server.ThreadingHTTPServer):
    allow_reuse_address = allow_reuse_port = True
    daemon_threads = False
srv = S(("127.0.0.1", PORT), H)
flag = f"/state/listening-{NAME}"
open(flag, "w").close()
def stop(*_):
    threading.Thread(target=srv.shutdown).start()
signal.signal(signal.SIGTERM, stop)
srv.serve_forever()
srv.server_close()
os.path.exists(flag) and os.unlink(flag)
