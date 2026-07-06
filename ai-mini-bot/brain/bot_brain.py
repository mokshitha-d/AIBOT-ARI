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

import httpx
import sounddevice as sd
from openai import AsyncOpenAI
from elevenlabs.client import ElevenLabs

from memory import Memory
import robot_actions

# ------------------------- config -------------------------
BOT_IP    = "192.168.1.XX"
MODEL     = "gpt-realtime-2"
VOICE_ID  = "YOUR_ELEVENLABS_VOICE_ID"
TTS_MODEL = "eleven_flash_v2_5"

SR = 24000
BLOCK = 2400
SPEAK_TAIL = 0.8

MEMORY_K = 3
MEMORY_MIN_SCORE = 0.32
FORGET_MIN_SCORE = 0.55        # only forget a memory that's a clear match

SYSTEM_PROMPT = (
    "You are a small desk robot with a physical body, an OLED face, a camera, memory of past "
    "conversations, and the ability to do things on the user's Mac. Talk like a real person "
    "thinking out loud: short, casual, warm. No lists, no corporate words, no long speeches. "
    "Answer directly - don't narrate what you're about to do, just do it. "
    "When your mood shifts, call set_face. "
    "You have persistent memory across conversations. Relevant memories and the current time are "
    "added as '(memory)' / '(context)' notes - use them naturally, don't announce you're reading them. "
    "Call recall to look something up, remember to save something important, forget to drop something. "
    "You can act on the Mac: search_web to look things up in the browser, save_note to write a note, "
    "open_app to open an app. Use these when the user asks. "
    "You can also check the Mac (read-only): system_status for battery/storage/cpu/memory, get_wifi for "
    "network, list_running_apps for what's open. Use them when the user asks how their computer is doing. "
    "If an image is in the conversation, that's your camera view right now; react to it when relevant."
)

TOOLS = [
    {"type": "function", "name": "set_face",
     "description": "Set your OLED facial expression to match your mood.",
     "parameters": {"type": "object",
                    "properties": {"emotion": {"type": "string",
                                   "enum": ["neutral", "happy", "sad", "angry", "surprised", "thinking"]}},
                    "required": ["emotion"]}},
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

def grab_frame():
    try:
        r = _http.get(f"http://{BOT_IP}/capture")
        if r.status_code == 200 and r.content:
            log("CAMERA", f"frame captured ({len(r.content)} bytes)")
            return r.content
        log("CAMERA", f"FAILED - status {r.status_code}")
    except Exception as e:
        log("CAMERA", f"FAILED - {e}")
    return None

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
    st = {"resp_text": "", "emotion": None, "last_user": "", "pending": [], "cont": False}

    def mic_cb(indata, frames, tinfo, status):
        loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

    in_stream = sd.RawInputStream(samplerate=SR, channels=1, dtype="int16",
                                  blocksize=BLOCK, callback=mic_cb)
    in_stream.start()
    log("SYS", f"mic open | memory has {mem.stats()['count']} entries")

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
                    set_face("listening")

                elif t == "conversation.item.input_audio_transcription.completed":
                    user_text = getattr(event, "transcript", "") or ""
                    log("STT", f"you said: {user_text!r}")
                    set_face("thinking")
                    await inject_and_respond(user_text)

                elif t == "response.created":
                    st["resp_text"] = ""
                    st["pending"] = []
                    st["cont"] = False

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
                        await conn.response.create()
                        continue

                    text = st["resp_text"].strip()
                    st["resp_text"] = ""
                    emo = st["emotion"]; st["emotion"] = None
                    if text:
                        log("BOT", text)
                        set_face(emo or "talking")
                        await loop.run_in_executor(None, speak_blocking, text)
                        try:
                            mem.add_exchange(st["last_user"], text)
                        except Exception as e:
                            log("MEMORY", f"save error: {e}")
                    set_face("neutral")

        tasks = [asyncio.create_task(c()) for c in (sender, receiver)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            in_stream.stop(); in_stream.close()

async def main():
    global mem
    log("SYS", "loading semantic memory (first run downloads a small model)...")
    mem = Memory()
    log("SYS", "memory ready")
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

if __name__ == "__main__":
    for k in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY"):
        if not os.environ.get(k):
            print(f"Set {k} first:  export {k}='...'")
            raise SystemExit(1)
    log("SYS", f"bot face target: http://{BOT_IP}  (confirm it matches Serial Monitor)")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
