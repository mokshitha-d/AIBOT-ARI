"""
AI Mini Bot - OpenAI Realtime (brain) + ElevenLabs (voice) + memory + Mac actions

Realtime listens/sees/thinks/controls the face in TEXT-ONLY mode; ElevenLabs speaks
in your cloned voice; memory.py remembers across sessions; robot_actions.py does
things on your Mac (the "Robot").

Turn flow (server VAD detects your turn; WE trigger the reply so we can inject
memory + time + camera first):
  you speak -> transcript -> inject (relevant memories + current time) + camera frame
             -> model replies with TEXT, possibly calling tools
             -> ElevenLabs speaks it in your voice
             -> save the exchange to memory

Tools the model can call:
  set_face(emotion)        - change the OLED expression (fire-and-forget)
  look(pan, tilt, see?)    - aim head (base L/R, neck U/D); see=true grabs a new photo
  look_center()            - return head to neutral (pan=90, tilt=90)
  recall(query)            - search long-term memory
  remember(fact)           - save a fact to long-term memory
  forget(query)            - delete the closest matching memory
  search_web(query)        - open a browser search on the Mac
  save_note(title, text)   - save a markdown note to ~/RobotNotes/
  open_app(name)           - open a whitelisted Mac app

Time: the current time is injected into every turn's context, so the bot always
knows what time it is without a round-trip.

--------------------------------------------------------------------------
SETUP
    pip install openai elevenlabs sounddevice httpx fastembed numpy
    export OPENAI_API_KEY="..."
    export ELEVENLABS_API_KEY="..."
    # set BOT_IP + VOICE_ID below, then (keep memory.py + robot_actions.py in this folder):
    python bot_brain.py
--------------------------------------------------------------------------
"""

import os
import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import sounddevice as sd
from openai import AsyncOpenAI
from elevenlabs.client import ElevenLabs

from memory import Memory
import robot_actions
from head_controller import HeadController, Activity

# ------------------------- config -------------------------
BOT_IP    = "192.168.1.XX"  # from Serial Monitor after flashing
MODEL     = "gpt-realtime-2"
VOICE_ID  = "YOUR_ELEVENLABS_VOICE_ID"
TTS_MODEL = "eleven_flash_v2_5"
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
USE_OLLAMA = os.environ.get("USE_OLLAMA", "1").lower() in {"1", "true", "yes", "on"}

SR = 24000
BLOCK = 2400
SPEAK_TAIL = 0.8

MEMORY_K = 3
MEMORY_MIN_SCORE = 0.32
FORGET_MIN_SCORE = 0.55        # only forget a memory that's a clear match

SYSTEM_PROMPT = (
    "You are a small desk robot with a physical body, an OLED face, a movable head (pan/tilt servos), "
    "memory of past conversations, and the ability to do things on the user's Mac. "
    "Your head moves on its own — it glances, scans the room, and tracks faces — but you can override "
    "with look / look_center for deliberate nods, pointing, or when the user asks you to look somewhere. "
    "You have a camera; photos are attached to turns and when you call look with see=true. "
    "Talk like a real person thinking out loud: short, casual, warm. No lists, no corporate words, no long speeches. "
    "Answer directly - don't narrate what you're about to do, just do it. "
    "When your mood shifts, call set_face. "
    "You can move your head: pan 0-180 is left/right (90 faces the user), tilt 0-180 is down/up (90 level). "
    "Use look for natural glances, nods, looking left/right when asked, and small expressive moves. "
    "Call look_center when you should face the user again. Don't spin constantly — the auto-tracker handles idle gaze. "
    "You have persistent memory across conversations. Relevant memories and the current time are "
    "added as '(memory)' / '(context)' notes - use them naturally, don't announce you're reading them. "
    "Call recall to look something up, remember to save something important, forget to drop something. "
    "You can act on the Mac: search_web to look things up in the browser, save_note to write a note, "
    "open_app to open an app. Use these when the user asks. "
    "You can also check the Mac (read-only): system_status for battery/storage/cpu/memory, get_wifi for "
    "network, list_running_apps for what's open. Use them when the user asks how their computer is doing."
)

TOOLS = [
    {"type": "function", "name": "set_face",
     "description": "Set your OLED facial expression to match your mood.",
     "parameters": {"type": "object",
                    "properties": {"emotion": {"type": "string",
                                   "enum": ["neutral", "happy", "sad", "angry", "surprised", "thinking"]}},
                    "required": ["emotion"]}},
    {"type": "function", "name": "look",
     "description": "Aim your head (overrides auto-tracking briefly). pan=left/right (90=center), "
                    "tilt=down/up (90=level). Set see=true to grab a fresh photo after moving.",
     "parameters": {"type": "object",
                    "properties": {
                        "pan":  {"type": "integer", "minimum": 0, "maximum": 180},
                        "tilt": {"type": "integer", "minimum": 0, "maximum": 180},
                        "see":  {"type": "boolean",
                                 "description": "If true, grab a fresh camera frame after moving."},
                    },
                    "required": ["pan", "tilt"]}},
    {"type": "function", "name": "look_center",
     "description": "Return your head to the neutral facing-the-user pose (pan=90, tilt=90).",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "recall",
     "description": "Search your long-term memory of past conversations.",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "remember",
     "description": "Save an important fact to long-term memory for future conversations.",
     "parameters": {"type": "object",
                    "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
    {"type": "function", "name": "forget",
     "description": "Delete the closest matching memory from long-term memory.",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "search_web",
     "description": "Open a web search in the browser on the Mac.",
     "parameters": {"type": "object",
                    "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"type": "function", "name": "save_note",
     "description": "Save a note as a markdown file on the Mac.",
     "parameters": {"type": "object",
                    "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["title", "text"]}},
    {"type": "function", "name": "open_app",
     "description": "Open an app on the Mac (whitelisted apps only).",
     "parameters": {"type": "object",
                    "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"type": "function", "name": "system_status",
     "description": "Read-only: the Mac's battery, storage, CPU load, and memory.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "get_wifi",
     "description": "Read-only: the Mac's current WiFi network and IP address.",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "list_running_apps",
     "description": "Read-only: apps currently open and the top CPU processes on the Mac.",
     "parameters": {"type": "object", "properties": {}}},
]

# ------------------------- logging -------------------------
def log(tag, msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} [{tag}] {msg}")

# ------------------------- clients + state -------------------------
_http = httpx.Client(timeout=2.5)
el = ElevenLabs()
mem = None
head = None
_last_face = None
speaking = False
last_out_ts = 0.0

def bot_speaking():
    return speaking or (time.time() - last_out_ts) < SPEAK_TAIL

# ------------------------- face + camera -------------------------
def set_face(emotion):
    global _last_face
    if emotion == _last_face:
        return
    _last_face = emotion
    try:
        _http.get(f"http://{BOT_IP}/set", params={"e": emotion}, timeout=1.0)
        log("FACE", emotion)
    except Exception as e:
        log("FACE", f"FAILED to set '{emotion}': {e}")

_camera_warned = False

def grab_frame():
    """Best-effort snapshot. Returns None quietly when the camera is offline."""
    global _camera_warned
    try:
        r = _http.get(f"http://{BOT_IP}/capture", timeout=1.0)
        if r.status_code == 200 and r.content:
            log("CAMERA", f"frame captured ({len(r.content)} bytes)")
            return r.content
        if not _camera_warned:
            log("CAMERA", "offline — continuing without vision")
            _camera_warned = True
    except Exception:
        if not _camera_warned:
            log("CAMERA", "offline — continuing without vision")
            _camera_warned = True
    return None

def set_look(pan, tilt):
    """Aim pan (base L/R) + tilt (neck U/D). Angles are 0–180, 90/90 = neutral."""
    try:
        r = _http.get(f"http://{BOT_IP}/look",
                      params={"pan": int(pan), "tilt": int(tilt)}, timeout=1.5)
        ok = r.status_code == 200
        log("LOOK", f"pan={pan} tilt={tilt} -> {r.text if ok else r.status_code}")
        return ok
    except Exception as e:
        log("LOOK", f"FAILED pan={pan} tilt={tilt}: {e}")
        return False

def look_center():
    try:
        r = _http.get(f"http://{BOT_IP}/look/center", timeout=1.5)
        ok = r.status_code == 200
        log("LOOK", f"center -> {r.text if ok else r.status_code}")
        return ok
    except Exception as e:
        log("LOOK", f"FAILED center: {e}")
        return False

# ------------------------- ElevenLabs voice -------------------------
def speak_blocking(text):
    global speaking, last_out_ts
    speaking = True
    out = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16")
    out.start()
    leftover = b""
    try:
        for chunk in el.text_to_speech.stream(
            text=text, voice_id=VOICE_ID, model_id=TTS_MODEL, output_format="pcm_24000"
        ):
            if not chunk:
                continue
            chunk = leftover + chunk
            n = len(chunk) - (len(chunk) % 2)
            out.write(chunk[:n])
            leftover = chunk[n:]
    except Exception as e:
        log("TTS", f"error: {e}")
    finally:
        out.stop(); out.close()
        last_out_ts = time.time()
        speaking = False

# ------------------------- tool dispatch -------------------------
def run_tool(name, args):
    """Execute a tool, return (result_dict, needs_spoken_followup)."""
    if name == "set_face":
        emo = args.get("emotion", "neutral")
        set_face(emo)
        return {"ok": True}, False          # fire-and-forget: text comes in same response

    if name == "look":
        pan  = int(args.get("pan", 90))
        tilt = int(args.get("tilt", 90))
        see  = bool(args.get("see", False))
        if head:
            head.manual_look(pan, tilt, hold=3.0 if see else 2.5)
            ok = True
        else:
            ok = set_look(pan, tilt)
        result = {"ok": ok, "pan": pan, "tilt": tilt}
        if see:
            set_face("scanning")
            time.sleep(0.35)
            if grab_frame() is None:
                result["see"] = False
                result["note"] = "camera offline — moved head only"
                return result, True
            result["_inject_frame"] = True
            return result, True
        return result, False

    if name == "look_center":
        if head:
            head.look_center(hold=2.0)
            ok = True
        else:
            ok = look_center()
        return {"ok": ok, "pan": 90, "tilt": 90}, False

    if name == "recall":
        set_face("memory")
        hits = mem.search(args.get("query", ""), k=MEMORY_K, min_score=MEMORY_MIN_SCORE)
        log("TOOL", f"recall -> {len(hits)} hit(s)")
        return {"memories": [h["text"] for h in hits]}, True

    if name == "remember":
        set_face("memory")
        fact = args.get("fact", "")
        mem.add(fact, meta={"type": "fact"})
        log("TOOL", f"remember({fact!r})")
        return {"ok": True}, True

    if name == "forget":
        set_face("memory")
        hits = mem.search(args.get("query", ""), k=1, min_score=FORGET_MIN_SCORE)
        if hits:
            mem.delete(hits[0]["id"])
            log("TOOL", f"forget -> deleted id {hits[0]['id']}")
            return {"ok": True, "forgot": hits[0]["text"]}, True
        log("TOOL", "forget -> no clear match")
        return {"ok": False, "reason": "no clear match"}, True

    if name == "search_web":
        set_face("searching")
        msg = robot_actions.search_web(args.get("query", ""))
        log("TOOL", msg)
        return {"result": msg}, True

    if name == "save_note":
        set_face("saving")
        msg = robot_actions.save_note(args.get("title", ""), args.get("text", ""))
        log("TOOL", msg)
        return {"result": msg}, True

    if name == "open_app":
        set_face("loading")
        msg = robot_actions.open_app(args.get("name", ""))
        log("TOOL", msg)
        return {"result": msg}, True

    if name == "system_status":
        set_face("scanning")
        return {"status": robot_actions.system_status()}, True

    if name == "get_wifi":
        set_face("wifi")
        return {"wifi": robot_actions.get_wifi()}, True

    if name == "list_running_apps":
        set_face("scanning")
        return {"apps": robot_actions.list_running_apps()}, True

    return {"ok": False, "error": "unknown tool"}, False

# ------------------------- main -------------------------
async def run():
    global last_out_ts
    client = AsyncOpenAI()
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue()
    st = {"resp_text": "", "emotion": None, "last_user": "", "pending": [], "cont": False, "see_after_look": False}

    def mic_cb(indata, frames, tinfo, status):
        loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

    in_stream = sd.RawInputStream(samplerate=SR, channels=1, dtype="int16",
                                  blocksize=BLOCK, callback=mic_cb)
    in_stream.start()
    log("SYS", f"mic open | memory has {mem.stats()['count']} entries")
    head.set_activity(Activity.IDLE)
    head.start(loop)

    async with client.realtime.connect(model=MODEL) as conn:
        log("SYS", f"connected to {MODEL} (text out -> ElevenLabs voice)")
        await conn.session.update(session={
            "type": "realtime",
            "output_modalities": ["text"],
            "instructions": SYSTEM_PROMPT,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "server_vad",
                                       "create_response": False,
                                       "interrupt_response": False},
                },
            },
            "tools": TOOLS,
        })
        log("SYS", f"session configured (voice_id={VOICE_ID})")
        set_face("neutral")

        async def sender():
            while True:
                chunk = await mic_q.get()
                if bot_speaking():
                    continue
                await conn.input_audio_buffer.append(audio=base64.b64encode(chunk).decode())

        async def inject_and_respond(user_text):
            st["last_user"] = user_text or ""
            # memory + current time as a context note
            recalled = mem.recall_text(user_text, k=MEMORY_K, min_score=MEMORY_MIN_SCORE)
            note = f"(context) Current time: {robot_actions.current_time()}."
            if recalled:
                note += f"\n(memory) {recalled}"
                log("MEMORY", f"recalled {recalled.count(chr(10))} item(s)")
            await conn.conversation.item.create(item={
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": note}],
            })
            # camera frame
            frame = grab_frame()
            if frame:
                b64 = base64.b64encode(frame).decode()
                await conn.conversation.item.create(item={
                    "type": "message", "role": "user",
                    "content": [{"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}],
                })
            await conn.response.create()

        async def receiver():
            global last_out_ts
            async for event in conn:
                t = event.type

                if t == "error":
                    log("ERROR", f"{getattr(event.error, 'code', '')}: {getattr(event.error, 'message', event)}")

                elif t == "input_audio_buffer.speech_started":
                    if bot_speaking():
                        continue
                    log("LISTEN", "you started talking")
                    head.set_activity(Activity.LISTENING)
                    set_face("listening")

                elif t == "conversation.item.input_audio_transcription.completed":
                    user_text = getattr(event, "transcript", "") or ""
                    log("STT", f"you said: {user_text!r}")
                    head.set_activity(Activity.THINKING)
                    set_face("thinking")
                    await inject_and_respond(user_text)

                elif t == "response.created":
                    st["resp_text"] = ""
                    st["pending"] = []
                    st["cont"] = False
                    st["see_after_look"] = False

                elif t == "response.output_text.delta":
                    st["resp_text"] += getattr(event, "delta", "") or ""

                elif t == "response.function_call_arguments.done":
                    name = getattr(event, "name", "")
                    try:
                        args = json.loads(event.arguments or "{}")
                    except Exception:
                        args = {}
                    if name == "set_face":
                        st["emotion"] = args.get("emotion", "neutral")
                    result, needs_followup = run_tool(name, args)
                    if result.pop("_inject_frame", False):
                        st["see_after_look"] = True
                    st["pending"].append((event.call_id, result))
                    if needs_followup:
                        st["cont"] = True

                elif t == "response.done":
                    # submit any tool outputs produced this response
                    for call_id, result in st["pending"]:
                        await conn.conversation.item.create(item={
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(result),
                        })
                    st["pending"] = []

                    if st["cont"]:
                        # a data/action tool ran -> let the model continue and actually speak
                        st["cont"] = False
                        # after look(..., see=true), feed a fresh camera frame before continuing
                        if st["see_after_look"]:
                            st["see_after_look"] = False
                            frame = grab_frame()
                            if frame:
                                b64 = base64.b64encode(frame).decode()
                                await conn.conversation.item.create(item={
                                    "type": "message", "role": "user",
                                    "content": [{
                                        "type": "input_image",
                                        "image_url": f"data:image/jpeg;base64,{b64}",
                                    }],
                                })
                        await conn.response.create()
                        continue

                    text = st["resp_text"].strip()
                    st["resp_text"] = ""
                    emo = st["emotion"]; st["emotion"] = None
                    if text:
                        log("BOT", text)
                        head.set_activity(Activity.SPEAKING)
                        set_face(emo or "talking")
                        await loop.run_in_executor(None, speak_blocking, text)
                        try:
                            mem.add_exchange(st["last_user"], text)
                        except Exception as e:
                            log("MEMORY", f"save error: {e}")
                    head.set_activity(Activity.IDLE)
                    set_face("neutral")

        tasks = [asyncio.create_task(c()) for c in (sender, receiver)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await head.stop()
            in_stream.stop(); in_stream.close()

def ollama_generate(prompt):
    try:
        resp = _http.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.7}},
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("response") or "").strip()
    except Exception as e:
        log("OLLAMA", f"request failed: {e}")
        return None


def run_text_mode():
    global mem
    log("SYS", "running in local Ollama text mode")
    log("SYS", f"using model '{OLLAMA_MODEL}' at {OLLAMA_BASE_URL}")
    while True:
        try:
            user_text = input("You: ").strip()
        except EOFError:
            break
        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit", "bye"}:
            print("Bot: Goodbye.")
            break

        recalled = mem.recall_text(user_text, k=MEMORY_K, min_score=MEMORY_MIN_SCORE)
        note = f"(context) Current time: {robot_actions.current_time()}."
        if recalled:
            note += f"\n(memory) {recalled}"

        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"{note}\n\n"
            f"User: {user_text}\n"
            "Assistant:"
        )
        reply = ollama_generate(prompt)
        if not reply:
            reply = "I couldn't reach the local model. Make sure Ollama is running and the model is installed."
        log("BOT", reply)
        print(f"Bot: {reply}")
        try:
            mem.add_exchange(user_text, reply)
        except Exception as e:
            log("MEMORY", f"save error: {e}")


async def main():
    global mem, head
    log("SYS", "loading semantic memory (first run downloads a small model)...")
    mem = Memory()
    log("SYS", "memory ready")

    if USE_OLLAMA:
        run_text_mode()
        return

    head = HeadController(BOT_IP, _http, log, grab_frame, set_face)
    log("SYS", "head controller ready (glance / scan / track / face_user)")
    while True:
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("SYS", f"session error: {e}")
            set_face("sleep")
            log("SYS", "reconnecting in 3s...")
            await asyncio.sleep(3)

def load_env_file():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

if __name__ == "__main__":
    load_env_file()
    if USE_OLLAMA:
        log("SYS", "Ollama mode enabled; no paid API keys required")
    else:
        for k in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
            if not os.environ.get(k):
                print(f"Set {k} first:  export {k}='...'")
                raise SystemExit(1)
    log("SYS", f"bot face target: http://{BOT_IP}  (confirm it matches Serial Monitor)")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
