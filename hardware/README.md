# Hardware

Breadboard prototype for the AI Mini Bot body: OLED face, camera (on Sense), and pan/tilt head via **PCA9685**.

## Parts

| Part | Role |
|------|------|
| Seeed XIAO ESP32S3 **Sense** | Brain-facing body (Wi-Fi, camera, I²C master) |
| 0.96" SSD1306 OLED | Face |
| PCA9685 16-ch PWM board | Drives servos (I²C) |
| HG90 / SG90-class servos ×2 | Base = pan (L/R), neck = tilt (U/D) |
| 5V **3–4A** supply | Powers PCA9685 `V+` (servos) — do **not** power servos from the XIAO 3V3 |

## Wiring (breadboard)

```
XIAO 3V3  ──► breadboard 3V3 rail ──► OLED VCC
                                    ──► PCA9685 VCC (logic)
XIAO GND  ──► breadboard GND      ──► OLED GND
                                    ──► PCA9685 GND
                                    ──► 5V supply GND  (common ground!)

XIAO D4 (SDA) ──► OLED SDA + PCA9685 SDA
XIAO D5 (SCL) ──► OLED SCL + PCA9685 SCL

5V supply (+) ──► PCA9685 V+ / terminal block  (servo power only)
```

### Servo channels (firmware defaults)

| PCA9685 channel | Axis | Motion |
|-----------------|------|--------|
| **CH0** | pan | base left / right |
| **CH1** | tilt | neck up / down |

Change `SERVO_CH_PAN` / `SERVO_CH_TILT` in `firmware/bot_face/bot_face.ino` if your plugs differ.

### Angles

| Pose | pan | tilt |
|------|-----|------|
| Neutral (looking at you) | 90 | 90 |
| Soft limits (first test) | 0–180 | 0–180 |

If left/right or up/down feel inverted after mounting, swap the mapping in firmware or reverse the mechanical horn.

## Power notes

- External **5V 3–4A** on the PCA9685 servo rail is required; USB alone will brown out under two HG90s.
- Keep **GND common** between XIAO, PCA9685, and the 5V brick.
- If the PCA9685 has an **OE** (output enable) pin, tie it to GND so outputs stay enabled.

## Test after flashing

1. Serial Monitor @ 115200 → note Bot IP; confirm `Servos: ready`
2. Browser: `http://<bot-ip>/` → use pan/tilt sliders + **center**
3. Or: `curl "http://<bot-ip>/look?pan=90&tilt=90"`

## 3D body

- **Model:** [`QBIT.3mf`](QBIT.3mf) (printable body)
- Optional: add a Printables / Thingiverse link here
