/*
  AI Mini Bot - Phase 3a: Face + camera "eyes" (snapshot on demand)
  Board:   Seeed XIAO ESP32S3 Sense
  Display: 0.96" SSD1306 OLED, I2C, addr 0x3C  (VCC->3V3 GND->GND SDA->D4 SCL->D5)
  Camera:  onboard OV2640 (Sense variant)

  This is Phase 1's face sketch, unchanged, PLUS one new endpoint:
      GET /capture  -> single JPEG frame from the onboard camera

  Why snapshot-on-demand instead of continuous streaming:
  Running the MJPEG stream constantly alongside the OLED (I2C) and WiFi is what
  caused the DMA/resource conflicts on the intruder-alarm build. Grabbing ONE
  frame only when the Mac brain asks for it keeps the camera idle the rest of
  the time, so there's nothing to fight over.

  REQUIRED board settings (same as your working camera sketch):
    Board: XIAO_ESP32S3 (Sense)
    Tools > PSRAM: OPI PSRAM        <-- critical, this is what fixed cam_dma_config before
    Tools > USB CDC On Boot: Enabled

  Test after upload:
    - Serial Monitor @ 115200 for the IP
    - curl http://<ip>/capture --output test.jpg    (should be a real photo)
    - http://<ip>/ still shows the face control page, plus a "look" button
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <WebServer.h>
#include "esp_camera.h"

// ---------- display ----------
#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64
#define OLED_ADDR     0x3C
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ---------- wifi ----------
const char* ssid     = "YOUR_WIFI_NAME";     // <-- your network
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
bool initCamera() {
  camera_config_t config;
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
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // snapshot use case: prioritize a clean single frame over streaming FPS
  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;    // 640x480 - faster capture+transfer, plenty for vision
    config.jpeg_quality  = 12;
    config.fb_count      = 2;
    config.grab_mode     = CAMERA_GRAB_WHEN_EMPTY;  // only capture when asked, not continuously in the background
  } else {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality  = 12;
    config.fb_count      = 1;
    config.grab_mode     = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }

  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_vflip(s, 1);    // flip vertically - fixes upside-down image
    s->set_hmirror(s, 0);  // set to 1 instead if it's mirrored left-right rather than upside-down
  }
  return true;
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
  String h =
    "<!DOCTYPE html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<style>body{background:#0b0b0f;color:#e6e6e6;font-family:system-ui;margin:0;padding:24px;text-align:center}"
    "h1{font-weight:500;font-size:18px}#s{color:#8a8a99;font-size:13px;margin-bottom:18px}"
    ".g{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;max-width:360px;margin:0 auto}"
    "button{background:#17171f;color:#e6e6e6;border:1px solid #2a2a35;border-radius:10px;padding:14px 8px;font-size:14px}"
    "button:active{background:#2a2a35}img{max-width:280px;margin-top:16px;border-radius:8px}</style></head><body>"
    "<h1>bot face</h1><div id=s>tap a mood</div><div class=g>";
  const char* names[] = {"neutral","happy","sad","angry","surprised","thinking","listening","talking","sleep",
                         "searching","loading","scanning","wifi","memory","saving"};
  for (int i = 0; i < 15; i++) {
    h += "<button onclick=\"set('"; h += names[i]; h += "')\">"; h += names[i]; h += "</button>";
  }
  h += "</div><div><button onclick=\"document.getElementById('cam').src='/capture?'+Date.now()\">look</button></div>"
       "<img id=cam src=''>"
       "<script>function set(e){fetch('/set?e='+e).then(r=>r.text()).then(t=>document.getElementById('s').innerText=t)}</script>"
       "</body></html>";
  server.send(200, "text/html", h);
}

void setupRoutes() {
  server.on("/", handleRoot);
  server.on("/set", handleSet);
  server.on("/capture", handleCapture);
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

  cameraReady = initCamera();   // init camera BEFORE Wire/I2C, matches working stream sketch order
  if (!cameraReady) {
    Serial.println("Continuing without camera - face still works, /capture will 503");
  }

  Wire.begin(D4, D5);
  Wire.setClock(400000);

  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("OLED not found - check wiring / address");
    while (1) delay(100);
  }
  display.clearDisplay();
  display.display();
  randomSeed(micros());

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println();
  Serial.print("Bot IP: ");
  Serial.println(WiFi.localIP());
  Serial.println(cameraReady ? "Camera: ready" : "Camera: NOT ready");

  setupRoutes();
  server.begin();
  nextBlink = millis() + 2000;
}

void loop() {
  server.handleClient();
  unsigned long now = millis();
  if (now - lastFrame >= (unsigned long)FRAME_MS) {
    lastFrame = now;
    updateBlink();
    renderFace();
  }
}
