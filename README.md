# AI Mini Bot — DIY Kit

Build your own tiny desk robot that **talks, looks around, remembers you, and can control your Mac**.

The **ESP32 is the body** (OLED face, camera, pan/tilt servos).  
Your **computer is the brain** (OpenAI Realtime voice + ElevenLabs + local memory + head tracking).

> Illustrated guide: open [`docs/build_guide.html`](docs/build_guide.html) in a browser.

---

## What you get

- Live voice conversation (OpenAI Realtime `gpt-realtime-2`)
- Cloned / chosen voice (ElevenLabs)
- Animated OLED face (15 expressions) + web **Face Deck** at `http://<bot-ip>/`
- Pan/tilt head via PCA9685 (AI tools + autonomous glance / scan / face track)
- Local semantic memory (stays on your machine)
- Optional Mac actions (search, notes, apps, system info)

---

## Parts list

| Part | Notes |
|------|--------|
| Seeed **XIAO ESP32S3 Sense** | Must be **Sense** (onboard camera) |
| 0.96" **SSD1306** OLED (I²C) | 128×64 |
| **PCA9685** 16-ch PWM board | Drives servos |
| 2× **HG90 / SG90** servos | Base = pan, neck = tilt |
| **5V 3–4A** supply | Powers PCA9685 `V+` (not from the XIAO 3V3) |
| Mac (or Linux/Windows with mic) | Runs the Python brain |

### Wiring (short version)

| From | To |
|------|----|
| OLED VCC / GND | XIAO 3V3 / GND |
| OLED SDA / SCL | XIAO **D4** / **D5** |
| PCA9685 VCC / GND | XIAO 3V3 / GND (common ground with 5V brick) |
| PCA9685 SDA / SCL | Same bus: **D4** / **D5** |
| PCA9685 V+ | External **5V** servo supply |
| Servos | PCA **CH0 = pan**, **CH1 = tilt** |

Full notes: [`hardware/README.md`](hardware/README.md).

---

## 1. Flash the body

1. Arduino IDE → ESP32 boards package  
2. **Tools:** Board `XIAO_ESP32S3`, **PSRAM = OPI PSRAM**, USB CDC On Boot = Enabled  
3. Libraries: `Adafruit GFX`, `Adafruit SSD1306`, `Adafruit PWM Servo Driver`  
4. Open `firmware/bot_face/bot_face.ino` (keep `control_page.h` beside it)  
5. Set your Wi‑Fi SSID/password near the top  
6. Upload → Serial Monitor @ **115200** → note **Bot IP**  
7. Open `http://<bot-ip>/` → Face Deck + head controls  

If upload fails at high baud, set **Upload Speed = 115200** and hold **BOOT** while resetting.

Camera off? In the sketch set `#define ENABLE_CAMERA 0`.

---

## 2. Run the brain

```bash
cd brain
python3.11 -m venv ../venv
source ../venv/bin/activate   # Windows: ..\venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` → `.env` and fill:

```bash
OPENAI_API_KEY=...        # needs Realtime API access
ELEVENLABS_API_KEY=...
```

Edit `brain/bot_brain.py`:

```python
BOT_IP   = "192.168.1.XX"           # from Serial Monitor
VOICE_ID = "YOUR_ELEVENLABS_VOICE_ID"
```

From the kit root:

```bash
./run.sh
# or:  cd brain && python bot_brain.py
```

---

## Repo layout

```
diy-kit/
  firmware/bot_face/   ESP32 body (face + servos + camera + web UI)
  brain/               Mac brain (Realtime, voice, memory, head tracking)
  hardware/            Wiring notes
  docs/                Build guide
  run.sh               Launcher
  .env.example         API key template
```

---

## How it thinks

```
[ ESP32 body ]                         [ your computer — brain ]
  OLED face                              hear → Realtime → reply text
  /set  /look  /capture   ←── Wi‑Fi ──→  ElevenLabs speaks
  Face Deck web UI                       memory + Mac tools
                                         head_controller (glance/scan/track)
```

The model can call tools (`set_face`, `look`, …). A separate head loop can glance, scan the room, and track faces from camera snapshots.

---

## Security

- Never commit `.env`, Wi‑Fi passwords, or API keys  
- `.gitignore` already excludes `.env`, `venv/`, and `bot_memory/`  
- Rotate any key that leaks  

---

## License

MIT — see [LICENSE](LICENSE). Fork it, build it, make it weird.
