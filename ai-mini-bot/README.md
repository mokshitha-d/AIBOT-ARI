# AI Mini Bot 🤖

A tiny desk robot that **talks, sees, remembers you, and controls your Mac** — built from a coin-sized ESP32 with a camera and an OLED face.

The board is the **body**; your computer is the **brain**. The ESP32 draws an animated face and takes photos; a Python program on your Mac does the hearing, thinking, speaking, and remembering. They talk over Wi-Fi.

> 📖 **Full illustrated build guide:** open [`docs/build_guide.html`](docs/build_guide.html) in a browser.

---

## What it does

- 🎙️ **Live voice conversation** — hears you and replies out loud
- 🗣️ **Your chosen voice** — speaks in a cloned voice (ElevenLabs)
- 👁️ **Sees you** — reacts to what its camera looks at
- 🧠 **Remembers** — local semantic memory that persists across sessions
- 😊 **Animated face** — 15 expressions on the OLED, including action faces
- 💻 **Acts on your Mac** — web search, notes, opening apps, system info

---

## How it works

```
   [ XIAO ESP32S3 — the body ]                 [ your Mac — the brain ]
   • draws the OLED face                        • hears you, thinks, replies
   • camera: one photo on request     Wi-Fi     • speaks in a cloned voice
   • /set (face)  •  /capture (photo)  <----->   • remembers every chat (local)
                                                 • searches, takes notes, etc.
```

It's **turn-based**: it listens, then speaks with its mic muted while talking. This is deliberate — on laptop speakers a fully live voice hears itself and cuts off mid-sentence. See the gotchas in the build guide.

---

## Repo layout

```
firmware/   bot_face.ino        → flash this to the ESP32
brain/      bot_brain.py        → the main program (run this on your Mac)
            memory.py           → local semantic memory
            robot_actions.py    → Mac actions (search, notes, apps, system)
            requirements.txt
docs/       build_guide.html    → illustrated step-by-step guide
hardware/   3D model + wiring notes
```

---

## Quick start

### 1. Hardware
- Seeed **XIAO ESP32S3 Sense** (the "Sense" version — it has the camera)
- 0.96" **SSD1306 OLED** (I²C, 128×64), 4 wires:

  | OLED | → | XIAO |
  |------|---|------|
  | VCC  | → | 3V3  |
  | GND  | → | GND  |
  | SDA  | → | D4 (GPIO5) |
  | SCL  | → | D5 (GPIO6) |

### 2. Flash the body
1. Arduino IDE → install the **ESP32** boards package
2. **Tools:** Board = `XIAO_ESP32S3`, PSRAM = `OPI PSRAM` (required!), USB CDC On Boot = `Enabled`
3. Install libraries: `Adafruit GFX`, `Adafruit SSD1306`
4. Open `firmware/bot_face.ino`, set your Wi-Fi name + password near the top
5. Upload, open Serial Monitor @ 115200, and note the **Bot IP** it prints
6. Test: open `http://<bot-ip>/` in a browser — tap faces, hit "look" for a photo

### 3. Set up the brain
```bash
cd brain
python3.11 -m venv ../venv
source ../venv/bin/activate
pip install -r requirements.txt
```

Add your API keys (copy `.env.example` → `.env` and fill them in, or export them):
```bash
export OPENAI_API_KEY="your-key"        # needs Realtime API access
export ELEVENLABS_API_KEY="your-key"
```

Edit two values at the top of `brain/bot_brain.py`:
```python
BOT_IP    = "192.168.1.XX"              # the IP from Serial Monitor
VOICE_ID  = "YOUR_ELEVENLABS_VOICE_ID"  # a voice you cloned at elevenlabs.io
```

### 4. Run it
```bash
python bot_brain.py
# or from the repo root:  ./run.sh
```
First run downloads a small local memory model (once). Then just talk to it.

---

## You'll need

| Service | For | Note |
|---------|-----|------|
| [OpenAI](https://platform.openai.com) | Hearing + thinking | Key with **Realtime API** access; audio is billed |
| [ElevenLabs](https://elevenlabs.io) | The voice | A cloned voice via API needs a **paid** plan |

Memory runs **fully locally** (fastembed) — no API, no cost, and your conversation data never leaves your machine.

---

## Managing memory

Memory is saved in plain files in `brain/bot_memory/`. Manage it from the terminal:
```bash
python memory.py list             # see everything
python memory.py search "coffee"  # semantic search
python memory.py delete 5         # remove one entry
python memory.py clear            # wipe it all
python memory.py export           # write a readable memory.md
```

---

## ⚠️ Security

- **Never commit** your `.env`, API keys, or Wi-Fi password. `.gitignore` already excludes `.env`, `bot_memory/`, and `venv/`.
- The firmware needs your Wi-Fi credentials typed in locally — don't push that edited copy with real values.
- If a key ever leaks, rotate it.

---

## Gotchas (learn from the pain)

- **Talks over itself?** Live voice on speakers hears itself. Use headphones, or keep the built-in mic-gating. Separating mic and speaker physically is the real fix.
- **Camera won't start?** "frame buffer malloc failed" → set `OPI PSRAM`. Cold-boot flicker → use a good USB cable.
- **Image upside down?** One line in the firmware flips it (`set_vflip`).
- **One folder.** Keep every file in a single folder — duplicate copies with the same name are the #1 time-sink.

---

## License

MIT — see [LICENSE](LICENSE). Built in public. PRs and forks welcome.
