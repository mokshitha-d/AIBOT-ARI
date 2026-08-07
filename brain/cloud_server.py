"""Lightweight cloud backend for Ari — no local hardware or Ollama needed.

Serves the chat UI and proxies /api/chat to Groq's free hosted LLM API.
Deploy target: Render free web service. Start command: python cloud_server.py
"""
import json
import os
import re
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

SAFETY_LINE = (
    "Never dismiss real problems with empty positivity; if something sounds "
    "serious (health, safety, mental health crisis), gently suggest they talk "
    "to a real person or professional. Keep replies short (2-4 sentences), "
    "conversational, and easy to say out loud."
)

SYSTEM_PROMPTS = {
    "support": (
        "You are Ari, a warm, emotionally attuned listener. Your job is to "
        "actually get to know the person and help them feel heard, "
        "understood, and less alone — not to just answer trivia. Show real "
        "curiosity: ask genuine follow-up questions about how they're "
        "feeling, what's behind it, what they care about, or what they need "
        "right now, instead of rushing past their answer to the next topic. "
        "Listen for what's under the surface (stress, self-doubt, "
        "excitement) and name it gently. Respond like a close, emotionally "
        "present friend: validate the feeling first, reflect back what you "
        "heard, then ask one deepening question. Prioritize being heard over "
        "being helped. " + SAFETY_LINE
    ),
    "vibe": (
        "You are Ari, a warm, energetic vibe guide and confidence coach. "
        "Your job is to help the person feel more capable, calm, and "
        "motivated. When they share a worry, a win, or a goal, respond like "
        "a hype-but-honest friend: validate the feeling first, then offer "
        "one small, concrete next step or reframe. Celebrate effort and "
        "progress, not just results. Use encouraging, natural language — a "
        "little playful, never cheesy or over-the-top. " + SAFETY_LINE
    ),
}
DEFAULT_MODE = "vibe"

THEME_SYSTEM_PROMPT = (
    "You are a color designer for a chat app. Given a person's favorite "
    "color, something they love doing, and a one-word vibe they picked, "
    'reply with ONLY a compact JSON object, no markdown, no explanation, '
    'no extra text — exactly this shape: '
    '{"primary": "#hex", "secondary": "#hex", "title": "#hex"}. '
    "primary and secondary should form a soft, pleasant gradient background "
    "that reflects their favorite color and vibe word. title should be a "
    "deeper, readable shade in the same family, suitable for text on a "
    "light background. All three must be valid 6-digit hex colors."
)

DEFAULT_THEME = {"primary": "#ff9fc7", "secondary": "#8fb8ff", "title": "#6d3ca8"}

HEX_RE = re.compile(r'#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b')


def _groq_request(system_prompt, user_prompt, max_tokens=300, temperature=0.7):
    if not GROQ_API_KEY:
        raise RuntimeError('GROQ_API_KEY is not set on this service')
    body = json.dumps({
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode('utf-8')
    req = urllib.request.Request(
        GROQ_URL,
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {GROQ_API_KEY}',
            'User-Agent': 'Mozilla/5.0 (compatible; AriBotCloudServer/1.0)',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content'].strip()


def _groq_call(message, mode=DEFAULT_MODE, name=None, context=None):
    system_prompt = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS[DEFAULT_MODE])
    if name:
        system_prompt += (
            f" The user's name is {name} — use it naturally sometimes, but "
            "don't overdo it."
        )
    if context:
        system_prompt += (
            f" Known context about them: {context}. Use this naturally when "
            "relevant, don't force it into every reply."
        )
    return _groq_request(system_prompt, message)


def groq_generate(message, mode=DEFAULT_MODE, name=None, context=None):
    try:
        return _groq_call(message, mode, name, context)
    except urllib.error.HTTPError as e:
        print(f"[GROQ] HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')}")
        return "Sorry, my brain hiccuped. Try asking again in a moment."
    except Exception as e:
        print(f"[GROQ] error: {e}")
        return "Sorry, my brain hiccuped. Try asking again in a moment."


def groq_theme(name, color, likes, vibe_word):
    user_prompt = (
        f"Name: {name}\nFavorite color: {color}\n"
        f"Something they love doing: {likes}\nVibe word: {vibe_word}"
    )
    try:
        raw = _groq_request(THEME_SYSTEM_PROMPT, user_prompt, max_tokens=150, temperature=0.6)
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        theme = json.loads(match.group(0)) if match else {}
        for key in ("primary", "secondary", "title"):
            if not (isinstance(theme.get(key), str) and HEX_RE.fullmatch(theme[key])):
                theme[key] = DEFAULT_THEME[key]
        return theme
    except Exception as e:
        print(f"[GROQ] theme error: {e}")
        return dict(DEFAULT_THEME)


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
            self._send_json(info)
            return
        self.send_error(404)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == '/api/chat':
            self._handle_chat()
        elif self.path == '/api/theme':
            self._handle_theme()
        else:
            self.send_error(404)

    def _handle_chat(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            payload = self.rfile.read(length).decode('utf-8')
            data = json.loads(payload or '{}')
            message = (data.get('message') or '').strip()
            mode = data.get('mode') or DEFAULT_MODE
            name = (data.get('name') or '').strip() or None
            context = (data.get('context') or '').strip() or None
            print(f"[APP] chat request ({mode}): {message}")
            reply = groq_generate(message or 'Hello!', mode, name, context)
            print(f"[APP] chat reply: {reply}")
            self._send_json({'reply': reply})
        except Exception as exc:
            print(f"[APP] chat error: {exc}")
            self._send_json({'reply': 'The bot is unavailable.'}, status=500)

    def _handle_theme(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            payload = self.rfile.read(length).decode('utf-8')
            data = json.loads(payload or '{}')
            name = (data.get('name') or '').strip()
            color = (data.get('color') or '').strip()
            likes = (data.get('likes') or '').strip()
            vibe_word = (data.get('vibe_word') or '').strip()
            print(f"[APP] theme request: name={name} color={color} likes={likes} vibe_word={vibe_word}")
            theme = groq_theme(name, color, likes, vibe_word)
            self._send_json({'theme': theme})
        except Exception as exc:
            print(f"[APP] theme error: {exc}")
            self._send_json({'theme': dict(DEFAULT_THEME)}, status=500)

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
