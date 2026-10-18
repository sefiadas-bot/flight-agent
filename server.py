"""
Local dashboard server — run this instead of opening dashboard.html directly.
Saves changes to searches.json and main.yml on disk immediately.
Auto-syncs with GitHub via git pull every 60 seconds.

Usage:  python server.py
Then open:  http://localhost:8080
"""
import http.server
import json
import os
import subprocess
import threading
import time

PORT = 8080
SYNC_INTERVAL = 60  # seconds
BASE = os.path.dirname(os.path.abspath(__file__))


def git_pull():
    try:
        result = subprocess.run(
            ['git', 'pull'],
            cwd=BASE, capture_output=True, text=True, timeout=30
        )
        msg = result.stdout.strip()
        if result.returncode == 0 and 'Already up to date' not in msg:
            print(f'  [sync] {msg}')
        elif result.returncode != 0:
            print(f'  [sync] git pull failed: {result.stderr.strip()}')
    except Exception as e:
        print(f'  [sync] error: {e}')


def auto_sync():
    while True:
        time.sleep(SYNC_INTERVAL)
        git_pull()


class Handler(http.server.BaseHTTPRequestHandler):

    # ── routing ───────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path in ('/', '/dashboard.html'):
            self._serve_file('dashboard.html', 'text/html; charset=utf-8')
        elif self.path == '/api/searches':
            self._serve_file('searches.json', 'application/json')
        elif self.path == '/api/workflow':
            self._serve_file('.github/workflows/main.yml', 'text/plain; charset=utf-8')
        else:
            self.send_error(404)

    def do_POST(self):
        body = self._read_body()
        if self.path == '/api/searches':
            self._write_file('searches.json',
                             json.dumps(json.loads(body), indent=2) + '\n',
                             encoding='utf-8')
        elif self.path == '/api/workflow':
            self._write_file('.github/workflows/main.yml',
                             body.decode('utf-8'),
                             encoding='utf-8')
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── helpers ───────────────────────────────────────────────────────────────
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length)

    def _serve_file(self, rel_path, content_type):
        path = os.path.join(BASE, rel_path)
        try:
            with open(path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _write_file(self, rel_path, text, encoding='utf-8'):
        path = os.path.join(BASE, rel_path)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding=encoding) as f:
                f.write(text)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            print(f'  saved -> {rel_path}')
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, fmt, *args):
        print(f'  {args[0]}')


if __name__ == '__main__':
    print('Syncing with GitHub...')
    git_pull()
    threading.Thread(target=auto_sync, daemon=True).start()
    print(f'Auto-sync every {SYNC_INTERVAL}s  |  Flight Agent Dashboard  ->  http://localhost:{PORT}')
    print('Press Ctrl+C to stop.\n')
    with http.server.HTTPServer(('localhost', PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nStopped.')
