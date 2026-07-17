/*
  AI Mini Bot - Face + camera + pan/tilt head (PCA9685)

  Board:   Seeed XIAO ESP32S3 Sense
  Display: 0.96" SSD1306 OLED, I2C, addr 0x3C  (VCC->3V3 GND->GND SDA->D4 SCL->D5)
  Camera:  onboard OV2640 (Sense variant)
  Servos:  HG90 / SG90-class via PCA9685 on the SAME I2C bus (D4/D5)
           - external 5V 3–4A supply to PCA9685 V+ / GND
           - PCA9685 VCC -> 3V3, GND common with XIAO
           - default channels: CH0 = pan (base L/R), CH1 = tilt (neck U/D)

  Endpoints:
      GET /capture              -> single JPEG frame
      GET /look?pan=90&tilt=90  -> set head angles (0–180)
      GET /look/center          -> pan=90, tilt=90
      GET /look/status          -> current angles JSON

  Why snapshot-on-demand instead of continuous streaming:
  Running the MJPEG stream constantly alongside the OLED (I2C) and WiFi is what
  caused the DMA/resource conflicts on the intruder-alarm build. Grabbing ONE
  frame only when the Mac brain asks for it keeps the camera idle the rest of
  the time, so there's nothing to fight over.

  REQUIRED board settings:
    Board: XIAO_ESP32S3 (Sense)
    Tools > PSRAM: OPI PSRAM
    Tools > USB CDC On Boot: Enabled

  Libraries: Adafruit GFX, Adafruit SSD1306, Adafruit PWM Servo Driver

  Test after upload:
    - Serial Monitor @ 115200 for the IP
    - curl "http://<ip>/look?pan=90&tilt=90"
    - curl http://<ip>/capture --output test.jpg
    - http://<ip>/ face page + head sliders
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_PWMServoDriver.h>
#include <WiFi.h>
#include <WebServer.h>
#include "esp_camera.h"
#include <string.h>
#include "control_page.h"

// Set to 0 while the Sense camera is out of commission (servos/OLED still work).
#define ENABLE_CAMERA  1

// ---------- display ----------
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_ADDR     0x3C
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ---------- wifi ----------
const char* ssid     = "YOUR_WIFI_SSID";     // <-- your network
const char* password = "YOUR_WIFI_PASSWORD";   // <-- fill this in
WebServer server(80);

// ---------- camera pins (XIAO ESP32S3 Sense) ----------
#define PWDN_GPIO_NUM     -1
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     10
#define SIOD_GPIO_NUM     40
#define SIOC_GPIO_NUM     39
#define Y9_GPIO_NUM       48
#define Y8_GPIO_NUM       11
#define Y7_GPIO_NUM       12
#define Y6_GPIO_NUM       14
#define Y5_GPIO_NUM       16
#define Y4_GPIO_NUM       18
#define Y3_GPIO_NUM       17
#define Y2_GPIO_NUM       15
#define VSYNC_GPIO_NUM    38
#define HREF_GPIO_NUM     47
#define PCLK_GPIO_NUM     13

bool cameraReady = false;
bool oledReady = false;

// ---------- head servos (PCA9685 @ 0x40 on shared I2C) ----------
// Change CH_* if your breadboard plugs pan/tilt into different PCA slots.
#define PCA9685_ADDR   0x40
#define SERVO_CH_PAN   0      // base: left / right
#define SERVO_CH_TILT  1      // neck: up / down
#define SERVO_US_MIN   500    // HG90 / SG90-ish full travel
#define SERVO_US_MAX   2500
#define PAN_CENTER     90
#define TILT_CENTER    90
#define PAN_MIN        0
#define PAN_MAX        180
#define TILT_MIN       0
#define TILT_MAX       180

Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(PCA9685_ADDR, Wire);
bool servosReady = false;
int currentPan  = PAN_CENTER;
int currentTilt = TILT_CENTER;

// ---------- emotion state ----------
enum Emotion { NEUTRAL, HAPPY, SAD, ANGRY, SURPRISED, THINKING, LISTENING, TALKING, SLEEP,
               SEARCHING, LOADING, SCANNING, WIFI, MEMORY, SAVING };
Emotion current = NEUTRAL;

// ---------- timing ----------
unsigned long lastFrame = 0;
const int FRAME_MS = 33;

// ---------- blink ----------
bool blinking = false;
unsigned long blinkStart = 0;
unsigned long nextBlink = 0;
const int BLINK_MS = 180;

// ---------- geometry ----------
const int CX = 64, CY = 32;
const int EYE_W = 34, EYE_H = 40, EYE_R = 10;
const int EYE_GAP = 20;

float blinkOpenness() {
  if (!blinking) return 1.0;
  unsigned long e = millis() - blinkStart;
  if (e >= (unsigned long)BLINK_MS) { blinking = false; return 1.0; }
  float t = (float)e / BLINK_MS;
  return fabs(t - 0.5) * 2.0;
}

void drawOpenEyes(float hScale, int yOffset, int wDelta) {
  int h = (int)(EYE_H * hScale);
  if (h < 4) h = 4;
  int w = EYE_W + wDelta;
  int y  = CY - h / 2 + yOffset;
  int lx = CX - EYE_GAP / 2 - w;
  int rx = CX + EYE_GAP / 2;
  display.fillRoundRect(lx, y, w, h, EYE_R, SSD1306_WHITE);
  display.fillRoundRect(rx, y, w, h, EYE_R, SSD1306_WHITE);
}

void drawHappyEyes() {
  int r = 18, y = CY - 2;
  int lcx = CX - EYE_GAP / 2 - r;
  int rcx = CX + EYE_GAP / 2 + r;
  display.fillCircle(lcx, y, r, SSD1306_WHITE);
  display.fillCircle(rcx, y, r, SSD1306_WHITE);
  display.fillRect(0, y, SCREEN_WIDTH, r + 3, SSD1306_BLACK);
}

void drawSurprisedEyes() {
  int r = 19;
  int lcx = CX - EYE_GAP / 2 - r + 3;
  int rcx = CX + EYE_GAP / 2 + r - 3;
  int y = CY;
  display.fillCircle(lcx, y, r, SSD1306_WHITE);
  display.fillCircle(rcx, y, r, SSD1306_WHITE);
  display.fillCircle(lcx, y, r / 2, SSD1306_BLACK);
  display.fillCircle(rcx, y, r / 2, SSD1306_BLACK);
}

void drawAngryEyes() {
  int h = 40, w = 34, y = CY - h / 2 + 2;
  int lx = CX - EYE_GAP / 2 - w;
  int rx = CX + EYE_GAP / 2;
  display.fillRoundRect(lx, y, w, h, EYE_R, SSD1306_WHITE);
  display.fillRoundRect(rx, y, w, h, EYE_R, SSD1306_WHITE);
  int cw = 26, ch = 20;
  display.fillTriangle(lx + w, y, lx + w - cw, y, lx + w, y + ch, SSD1306_BLACK);
  display.fillTriangle(rx,     y, rx + cw,     y, rx,     y + ch, SSD1306_BLACK);
}

void drawSadEyes() {
  int h = 30, w = 34, y = CY - h / 2 + 6;
  int lx = CX - EYE_GAP / 2 - w;
  int rx = CX + EYE_GAP / 2;
  display.fillRoundRect(lx, y, w, h, EYE_R, SSD1306_WHITE);
  display.fillRoundRect(rx, y, w, h, EYE_R, SSD1306_WHITE);
  int cw = 26, ch = 20;
  display.fillTriangle(lx,     y, lx + cw,     y, lx,     y + ch, SSD1306_BLACK);
  display.fillTriangle(rx + w, y, rx + w - cw, y, rx + w, y + ch, SSD1306_BLACK);
}

void drawThinkingEyes() {
  int h = 26, w = 34, xs = 6, ys = -4;
  int y  = CY - h / 2 + ys;
  int lx = CX - EYE_GAP / 2 - w + xs;
  int rx = CX + EYE_GAP / 2 + xs;
  display.fillRoundRect(lx, y, w, h, EYE_R, SSD1306_WHITE);
  display.fillRoundRect(rx, y, w, h, EYE_R, SSD1306_WHITE);
}

void drawSleepEyes() {
  int w = 34, y = CY;
  int lx = CX - EYE_GAP / 2 - w;
  int rx = CX + EYE_GAP / 2;
  display.fillRoundRect(lx, y - 2, w, 5, 2, SSD1306_WHITE);
  display.fillRoundRect(rx, y - 2, w, 5, 2, SSD1306_WHITE);
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(98, 8);  display.print("z");
  display.setCursor(106, 0); display.print("z");
}

// shared: eyes with an x/y shift and height scale (used by the action faces)
void drawEyesShift(int xs, int ys, float hScale) {
  int h = (int)(EYE_H * hScale);
  if (h < 4) h = 4;
  int w = EYE_W;
  int y  = CY - h / 2 + ys;
  int lx = CX - EYE_GAP / 2 - w + xs;
  int rx = CX + EYE_GAP / 2 + xs;
  display.fillRoundRect(lx, y, w, h, EYE_R, SSD1306_WHITE);
  display.fillRoundRect(rx, y, w, h, EYE_R, SSD1306_WHITE);
}

// SEARCHING (web search): eyes dart side to side, scanning
void drawSearching() {
  int xs = (int)(14 * sin(millis() / 180.0));
  drawEyesShift(xs, 0, 0.8);
}

// LOADING (opening app / generic work): a comet-head spinner ring
void drawLoading() {
  int r = 16, dots = 12;
  int head = (int)(millis() / 80) % dots;
  for (int i = 0; i < dots; i++) {
    float a = (2 * PI * i) / dots;
    int x = CX + (int)(r * cos(a));
    int y = CY + (int)(r * sin(a));
    int dist = (i - head + dots) % dots;
    int rad = (dist == 0) ? 3 : (dist == 1 ? 2 : (dist == 2 ? 1 : 0));
    if (rad > 0) display.fillCircle(x, y, rad, SSD1306_WHITE);
  }
}

// SCANNING (system status): focused eyes + a scan bar sweeping across
void drawScanning() {
  drawEyesShift(0, 0, 0.55);
  int x = (int)(CX + 60 * sin(millis() / 300.0));
  display.drawFastVLine(x, 0, SCREEN_HEIGHT, SSD1306_WHITE);
}

// WIFI (network check): pulsing wifi arcs rising from a base dot
void drawWifi() {
  int by = 46;
  display.fillCircle(CX, by, 3, SSD1306_WHITE);
  int phase = (int)(millis() / 300) % 4;
  for (int k = 1; k <= 3; k++) {
    if (k <= phase) {
      int rr = k * 11;
      for (int deg = 225; deg <= 315; deg += 5) {
        float a = deg * PI / 180.0;
        int x = CX + (int)(rr * cos(a));
        int y = by + (int)(rr * sin(a));
        display.drawPixel(x, y, SSD1306_WHITE);
      }
    }
  }
}

// MEMORY (remember / recall / forget): eyes glance up, thought dots rise
void drawMemory() {
  drawEyesShift(-6, -3, 0.8);
  int phase = (int)(millis() / 250) % 3;
  int bx = 98, by = 22;
  for (int i = 0; i < 3; i++) {
    if (i <= phase) display.fillCircle(bx + i * 7, by - i * 7, (i == 2 ? 1 : 2), SSD1306_WHITE);
  }
}

// SAVING (save note): eyes + a cycling progress bar
void drawSaving() {
  drawEyesShift(0, -5, 0.7);
  int w = 80, x = (SCREEN_WIDTH - w) / 2, y = 52, h = 7;
  display.drawRoundRect(x, y, w, h, 2, SSD1306_WHITE);
  int fill = (int)(millis() / 18) % (w - 2);
  display.fillRect(x + 1, y + 1, fill, h - 2, SSD1306_WHITE);
}

void renderFace() {
  display.clearDisplay();
  switch (current) {
    case NEUTRAL:
      drawOpenEyes(blinkOpenness(), 0, 0);
      break;
    case LISTENING: {
      float pulse = 1.0 + 0.06 * sin(millis() / 250.0);
      drawOpenEyes(blinkOpenness() * pulse, 0, 2);
      break;
    }
    case TALKING: {
      int bob = (int)(2 * sin(millis() / 90.0));
      drawOpenEyes(blinkOpenness(), bob, 0);
      break;
    }
    case HAPPY:     drawHappyEyes();     break;
    case SAD:       drawSadEyes();       break;
    case ANGRY:     drawAngryEyes();     break;
    case SURPRISED: drawSurprisedEyes(); break;
    case THINKING:  drawThinkingEyes();  break;
    case SLEEP:     drawSleepEyes();     break;
    case SEARCHING: drawSearching();     break;
    case LOADING:   drawLoading();       break;
    case SCANNING:  drawScanning();      break;
    case WIFI:      drawWifi();          break;
    case MEMORY:    drawMemory();        break;
    case SAVING:    drawSaving();        break;
  }
  display.display();
}

void updateBlink() {
  if (current != NEUTRAL && current != LISTENING && current != TALKING) return;
  unsigned long now = millis();
  if (!blinking && now > nextBlink) {
    blinking = true;
    blinkStart = now;
    nextBlink = now + random(2500, 6000);
  }
}

// ---------- camera ----------
// 0x106 = ESP_ERR_NOT_SUPPORTED → sensor probe failed / bad config.
// Newer Arduino-ESP32 BSPs added fields to camera_config_t; leaving them
// uninitialized often causes exactly this. Zero the struct, keep SCCB on
// I2C port 1 so it never fights Wire (OLED + PCA9685 on port 0 / D4+D5).
bool initCameraOnce(int xclk_hz) {
  camera_config_t config;
  memset(&config, 0, sizeof(config));

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk  = XCLK_GPIO_NUM;
  config.pin_pclk  = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href  = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn  = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = xclk_hz;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size   = FRAMESIZE_VGA;
  config.jpeg_quality = 12;
  config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
#if defined(CAMERA_FB_IN_PSRAM)
  config.fb_location  = CAMERA_FB_IN_PSRAM;
#endif

  if (psramFound()) {
    config.fb_count = 2;
  } else {
    config.fb_count = 1;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x (xclk=%dHz)\n", err, xclk_hz);
    return false;
  }
  return true;
}

bool initCamera() {
  Serial.printf("PSRAM: %s\n", psramFound() ? "found" : "NOT found (set Tools > PSRAM = OPI PSRAM)");
  delay(300);   // Sense cam ribbon is picky on cold boot / after upload

  // Single init only — esp_camera_init is effectively one-shot on many BSPs.
  if (!initCameraOnce(20000000)) {
    Serial.println("Camera probe failed (0x106 = sensor ID not recognized).");
    Serial.println("This is usually: loose Sense ribbon, OR Arduino ESP32 core 3.x bug.");
    Serial.println("Fix: reseat camera FPC, then try Boards Manager -> esp32 -> 2.0.14");
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_vflip(s, 1);    // flip vertically - fixes upside-down image
    s->set_hmirror(s, 0);  // set to 1 instead if it's mirrored left-right rather than upside-down
  }
  Serial.println("Camera: init ok");
  return true;
}

// ---------- servos ----------
int clampInt(int v, int lo, int hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

int angleToUs(int angle) {
  angle = clampInt(angle, 0, 180);
  return SERVO_US_MIN + (angle * (SERVO_US_MAX - SERVO_US_MIN)) / 180;
}

void scanI2C() {
  Serial.println("I2C scan (D4=SDA, D5=SCL):");
  int n = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  found 0x%02X", addr);
      if (addr == OLED_ADDR) Serial.print("  (OLED)");
      if (addr == PCA9685_ADDR) Serial.print("  (PCA9685)");
      Serial.println();
      n++;
    }
  }
  if (n == 0) Serial.println("  nothing found — check 3V3/GND/SDA/SCL");
  Serial.println("Expect OLED 0x3C/0x3D + PCA 0x40. 0x70=PCA all-call OK. Ghost 0x01 => flaky bus/pull-ups.");
}


// Drive a channel with setPWM (more reliable across PCA9685 clones than us helpers).
void writeServoAngle(uint8_t ch, int angle) {
  int us = angleToUs(angle);
  // Prefer library us helper; also poke setPWM for stubborn clones.
  pca.writeMicroseconds(ch, us);
  uint16_t tick = (uint16_t)(((uint32_t)us * 4096UL) / 20000UL);
  if (tick < 1) tick = 1;
  if (tick > 4095) tick = 4095;
  pca.setPWM(ch, 0, tick);
  Serial.printf("servo CH%d -> %d deg (%dus / tick %u)\n", ch, angle, us, tick);
}

bool initServos() {
  // Older Adafruit_PWMServoDriver: begin(prescale=0). Address comes from the constructor.
  if (!pca.begin()) {
    Serial.println("PCA9685 not found on I2C - check wiring / address 0x40");
    return false;
  }
  // Cheap PCA9685 boards often need this or pulse widths are wrong / no motion.
  pca.setOscillatorFrequency(27000000);
  pca.setPWMFreq(50);   // hobby servo rate
  delay(20);

  currentPan  = PAN_CENTER;
  currentTilt = TILT_CENTER;
  writeServoAngle(SERVO_CH_PAN,  currentPan);
  writeServoAngle(SERVO_CH_TILT, currentTilt);
  delay(300);

  // Visible boot self-test — if you see no motion here, it's power/wiring/channel.
  Serial.println("Servo wiggle test (CH0 pan, CH1 tilt)...");
  writeServoAngle(SERVO_CH_PAN,  70);
  delay(250);
  writeServoAngle(SERVO_CH_PAN, 110);
  delay(250);
  writeServoAngle(SERVO_CH_PAN,  PAN_CENTER);
  writeServoAngle(SERVO_CH_TILT, 70);
  delay(250);
  writeServoAngle(SERVO_CH_TILT, 110);
  delay(250);
  writeServoAngle(SERVO_CH_TILT, TILT_CENTER);

  Serial.printf("Servos: ready (pan CH%d, tilt CH%d) centered at %d/%d\n",
                SERVO_CH_PAN, SERVO_CH_TILT, currentPan, currentTilt);
  Serial.println("If no wiggle: check 5V on PCA V+/terminal, common GND, OE low, servo plugs on CH0/CH1");
  return true;
}

void setHead(int pan, int tilt) {
  currentPan  = clampInt(pan,  PAN_MIN,  PAN_MAX);
  currentTilt = clampInt(tilt, TILT_MIN, TILT_MAX);
  if (!servosReady) return;
  writeServoAngle(SERVO_CH_PAN,  currentPan);
  writeServoAngle(SERVO_CH_TILT, currentTilt);
}

void handleCapture() {
  if (!cameraReady) {
    server.send(503, "text/plain", "camera not ready");
    return;
  }
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) {
    server.send(500, "text/plain", "capture failed");
    return;
  }
  server.setContentLength(fb->len);
  server.send(200, "image/jpeg", "");
  WiFiClient client = server.client();
  client.write(fb->buf, fb->len);
  esp_camera_fb_return(fb);
}

void handleLook() {
  // Accept any combo of pan / tilt; missing args keep the current angle.
  Serial.printf("LOOK req args pan=%s tilt=%s\n",
                server.hasArg("pan") ? server.arg("pan").c_str() : "-",
                server.hasArg("tilt") ? server.arg("tilt").c_str() : "-");
  int pan  = server.hasArg("pan")  ? server.arg("pan").toInt()  : currentPan;
  int tilt = server.hasArg("tilt") ? server.arg("tilt").toInt() : currentTilt;
  if (!servosReady) {
    server.send(503, "text/plain", "servos not ready");
    return;
  }
  setHead(pan, tilt);
  String body = "ok:pan=" + String(currentPan) + ",tilt=" + String(currentTilt);
  server.send(200, "text/plain", body);
  Serial.println(body);
}

void handleLookCenter() {
  if (!servosReady) {
    server.send(503, "text/plain", "servos not ready");
    return;
  }
  setHead(PAN_CENTER, TILT_CENTER);
  server.send(200, "text/plain", "ok:center");
  Serial.println("ok:center");
}

void handleLookStatus() {
  String j = "{\"pan\":" + String(currentPan) +
             ",\"tilt\":" + String(currentTilt) +
             ",\"ready\":" + String(servosReady ? "true" : "false") + "}";
  server.send(200, "application/json", j);
}

// ---------- web ----------
Emotion parseEmotion(String s) {
  s.toLowerCase();
  if (s == "neutral")   return NEUTRAL;
  if (s == "happy")     return HAPPY;
  if (s == "sad")       return SAD;
  if (s == "angry")     return ANGRY;
  if (s == "surprised") return SURPRISED;
  if (s == "thinking")  return THINKING;
  if (s == "listening") return LISTENING;
  if (s == "talking")   return TALKING;
  if (s == "sleep")     return SLEEP;
  if (s == "searching") return SEARCHING;
  if (s == "loading")   return LOADING;
  if (s == "scanning")  return SCANNING;
  if (s == "wifi")      return WIFI;
  if (s == "memory")    return MEMORY;
  if (s == "saving")    return SAVING;
  return NEUTRAL;
}

void handleSet() {
  if (!server.hasArg("e")) { server.send(400, "text/plain", "missing e"); return; }
  current = parseEmotion(server.arg("e"));
  server.send(200, "text/plain", "ok:" + server.arg("e"));
}

void handleRoot() {
  // Fancy animated control deck lives in flash (control_page.h).
  server.send_P(200, "text/html", CONTROL_PAGE);
}

void setupRoutes() {
  server.on("/", handleRoot);
  server.on("/set", handleSet);
  server.on("/capture", handleCapture);
  // Register /look/* BEFORE /look so the longer paths win on picky WebServer builds.
  server.on("/look/center", HTTP_GET, handleLookCenter);
  server.on("/look/status", HTTP_GET, handleLookStatus);
  server.on("/look", HTTP_GET, handleLook);
  auto reg = [](const char* p, Emotion e) {
    server.on(p, [e]() { current = e; server.send(200, "text/plain", "ok"); });
  };
  reg("/neutral", NEUTRAL);   reg("/happy", HAPPY);       reg("/sad", SAD);
  reg("/angry", ANGRY);       reg("/surprised", SURPRISED); reg("/thinking", THINKING);
  reg("/listening", LISTENING); reg("/talking", TALKING);  reg("/sleep", SLEEP);
  reg("/searching", SEARCHING); reg("/loading", LOADING);  reg("/scanning", SCANNING);
  reg("/wifi", WIFI);         reg("/memory", MEMORY);      reg("/saving", SAVING);
}

void setup() {
  Serial.begin(115200);
  delay(800);   // USB CDC needs a moment after reset before logs

#if ENABLE_CAMERA
  cameraReady = initCamera();   // camera BEFORE Wire if enabled
  if (!cameraReady) {
    Serial.println("Continuing without camera - face still works, /capture will 503");
  }
#else
  cameraReady = false;
  Serial.println("Camera: disabled (ENABLE_CAMERA=0) — servos + face only");
#endif

  // Breadboard I2C: 100 kHz is much more reliable than 400 kHz.
  Wire.begin(D4, D5);
  Wire.setClock(100000);
  scanI2C();

  // OLED first. Many modules are 0x3C; some are 0x3D. Probe both.
  // Note: if the I2C scan did not list 0x3C/0x3D, wiring/pull-ups are suspect even if begin() looks ok.
  oledReady = false;
  uint8_t oledAddr = 0;
  for (uint8_t a : { (uint8_t)0x3C, (uint8_t)0x3D }) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() != 0) {
      Serial.printf("OLED probe 0x%02X: no ACK\n", a);
      continue;
    }
    if (display.begin(SSD1306_SWITCHCAPVCC, a)) {
      oledReady = true;
      oledAddr = a;
      Serial.printf("OLED: ok @ 0x%02X\n", a);
      break;
    }
    Serial.printf("OLED begin failed @ 0x%02X\n", a);
  }
  if (!oledReady) {
    Serial.println("OLED not found @ 0x3C/0x3D — check VCC/GND/SDA(D4)/SCL(D5), try 4.7k pull-ups to 3V3");
  } else {
    display.clearDisplay();
    display.setTextSize(2);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(8, 24);
    display.println("HELLO");
    display.display();
    delay(500);
    display.clearDisplay();
    display.setTextSize(1);
    display.setCursor(0, 24);
    display.println("bot boot...");
    display.printf("addr 0x%02X", oledAddr);
    display.display();
  }

  servosReady = initServos();   // PCA9685 on same Wire bus

  if (oledReady) {
    display.clearDisplay();
    display.setCursor(0, 0);
    display.println(servosReady ? "servos: ok" : "servos: FAIL");
    display.println("face ready");
    display.display();
  }
  randomSeed(micros());

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println();
  Serial.print("Bot IP: ");
  Serial.println(WiFi.localIP());
  Serial.println(cameraReady ? "Camera: ready" : "Camera: disabled/not ready");
  Serial.println(servosReady ? "Servos: ready" : "Servos: NOT ready");
  Serial.println(oledReady ? "OLED: ready" : "OLED: NOT ready");

  setupRoutes();
  server.begin();
  nextBlink = millis() + 2000;

}

void loop() {
  server.handleClient();
  unsigned long now = millis();
  if (oledReady && now - lastFrame >= (unsigned long)FRAME_MS) {
    lastFrame = now;
    updateBlink();
    renderFace();
  }
}
