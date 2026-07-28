import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import bot_brain

HOST = '127.0.0.1'
PORT = 8000

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            with open(os.path.join(os.path.dirname(__file__), 'web', 'index.html'), 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != '/api/chat':
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            payload = self.rfile.read(length).decode('utf-8')
            data = json.loads(payload or '{}')
            message = (data.get('message') or '').strip()
            print(f"[APP] chat request: {message}")
            reply = bot_brain.ollama_generate(message or 'Hello!') or 'Sorry, I could not reply.'
            print(f"[APP] chat reply: {reply}")
            body = json.dumps({'reply': reply}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            print(f"[APP] chat error: {exc}")
            body = json.dumps({'reply': 'The bot is unavailable.'}).encode('utf-8')
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        return

if __name__ == '__main__':
    print(f'Serving UI at http://{HOST}:{PORT}')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
