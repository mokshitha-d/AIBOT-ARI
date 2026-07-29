"""Lightweight cloud backend for Ari — no local hardware or Ollama needed.

Serves the chat UI and proxies /api/chat to Groq's free hosted LLM API.
Deploy target: Render free web service. Start command: python cloud_server.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(line_buffering=True)

HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', 8000))
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'llama-3.1-8b-instant')
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '*')

SYSTEM_PROMPT = (
    "You are Ari, a friendly, upbeat robot buddy for kids and families. "
    "Keep answers short, clear, and age-appropriate."
)


def _groq_call(message):
    if not GROQ_API_KEY:
        raise RuntimeError('GROQ_API_KEY is not set on this service')
    body = json.dumps({
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        "temperature": 0.7,
        "max_tokens": 300,
    }).encode('utf-8')
    req = urllib.request.Request(
        GROQ_URL,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {GROQ_API_KEY}',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content'].strip()


def groq_generate(message):
    try:
        return _groq_call(message)
    except urllib.error.HTTPError as e:
        print(f"[GROQ] HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')}")
        return "Sorry, my brain hiccuped. Try asking again in a moment."
    except Exception as e:
        print(f"[GROQ] error: {e}")
        return "Sorry, my brain hiccuped. Try asking again in a moment."


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', ALLOWED_ORIGIN)
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == '/':
            path = os.path.join(os.path.dirname(__file__), 'web', 'index.html')
            with open(path, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/health':
            info = {
                'groq_key_set': bool(GROQ_API_KEY),
                'groq_key_length': len(GROQ_API_KEY),
                'model': GROQ_MODEL,
            }
            try:
                info['test_reply'] = _groq_call('Reply with exactly: OK')
            except urllib.error.HTTPError as e:
                info['test_error'] = f'HTTP {e.code}: {e.read().decode("utf-8", errors="ignore")}'
            except Exception as e:
                info['test_error'] = str(e)
            body = json.dumps(info).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
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
            reply = groq_generate(message or 'Hello!')
            print(f"[APP] chat reply: {reply}")
            body = json.dumps({'reply': reply}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            print(f"[APP] chat error: {exc}")
            body = json.dumps({'reply': 'The bot is unavailable.'}).encode('utf-8')
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == '__main__':
    print(f'Serving Ari on {HOST}:{PORT}')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
