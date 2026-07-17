"""
head_controller.py - Autonomous pan/tilt behavior for the desk robot body.

The Mac brain decides *when* to glance, face the user, scan the room, or track
a face. The ESP32 is only an actuator: it receives pan/tilt angles and draws
the OLED face. Vision runs here on snapshots from GET /capture.

Modes:
  face_user  - neutral pose (pan/tilt center), default when engaged
  glance     - quick expressive look away and back
  scan       - sweep pan while sampling the camera for a face
  track      - follow the largest face in frame with smoothed servo angles

Priority: manual LLM look() calls temporarily override autonomy.
Activity hints (listening / speaking / thinking / idle) bias mode selection.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import cv2
import httpx
import numpy as np

# ------------------------- geometry / tuning -------------------------
PAN_CENTER = 90
TILT_CENTER = 90
PAN_MIN, PAN_MAX = 0, 180
TILT_MIN, TILT_MAX = 0, 180

# Map pixel offset from frame center -> servo degrees (tune for your desk distance).
PAN_GAIN = 0.11
TILT_GAIN = 0.13
PAN_DEADZONE = 18   # pixels — ignore tiny jitter
TILT_DEADZONE = 14

MAX_STEP = 4.0          # max degrees per tick while tracking
TRACK_INTERVAL = 0.12   # seconds between track updates (~8 Hz)
SCAN_PAN_STEP = 6       # degrees per scan step
SCAN_TILT = 88          # slightly up while scanning
GLANCE_HOLD = 0.45      # seconds at glance target
IDLE_SCAN_AFTER = 12.0  # idle seconds before room scan
GLANCE_IDLE_MIN = 8.0   # min idle seconds between random glances


class HeadMode(str, Enum):
    FACE_USER = "face_user"
    GLANCE = "glance"
    SCAN = "scan"
    TRACK = "track"
    MANUAL = "manual"


class Activity(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass
class FaceObservation:
    cx: float
    cy: float
    width: int
    height: int
    area: float


@dataclass
class HeadState:
    mode: HeadMode = HeadMode.FACE_USER
    activity: Activity = Activity.IDLE
    pan: float = float(PAN_CENTER)
    tilt: float = float(TILT_CENTER)
    last_face_ts: float = 0.0
    idle_since: float = field(default_factory=time.time)
    manual_until: float = 0.0
    glance_until: float = 0.0
    glance_return_pan: float = float(PAN_CENTER)
    glance_return_tilt: float = float(TILT_CENTER)
    scan_pan: float = 55.0
    scan_dir: int = 1


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _clamp_int(v: float, lo: int, hi: int) -> int:
    return int(_clamp(v, lo, hi))


class FaceDetector:
    """OpenCV Haar cascade — lightweight, no extra model download."""

    def __init__(self):
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise RuntimeError(f"Failed to load face cascade from {path}")

    def detect(self, jpeg_bytes: bytes) -> Optional[FaceObservation]:
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.08, minNeighbors=5, minSize=(48, 48)
        )
        if len(faces) == 0:
            return None
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        return FaceObservation(
            cx=x + fw / 2.0,
            cy=y + fh / 2.0,
            width=w,
            height=h,
            area=float(fw * fh),
        )


def face_to_angles(face: FaceObservation, pan: float, tilt: float) -> tuple[float, float]:
    """Convert face position in frame to absolute pan/tilt targets."""
    cx_frame = face.width / 2.0
    cy_frame = face.height / 2.0
    dx = face.cx - cx_frame
    dy = face.cy - cy_frame

    if abs(dx) > PAN_DEADZONE:
        pan += dx * PAN_GAIN
    if abs(dy) > TILT_DEADZONE:
        tilt += dy * TILT_GAIN

    return _clamp(pan, PAN_MIN, PAN_MAX), _clamp(tilt, TILT_MIN, TILT_MAX)


def smooth_toward(current: float, target: float, max_step: float) -> float:
    delta = target - current
    if abs(delta) <= max_step:
        return target
    return current + max_step if delta > 0 else current - max_step


class HeadController:
    """
    Background gaze loop. Call set_activity() from the voice pipeline and
    manual_look() from LLM tools.
    """

    def __init__(
        self,
        bot_ip: str,
        http: httpx.Client,
        log: Callable[[str, str], None],
        grab_frame: Callable[[], Optional[bytes]],
        set_face: Callable[[str], None],
    ):
        self.bot_ip = bot_ip
        self._http = http
        self._log = log
        self._grab_frame = grab_frame
        self._set_face = set_face
        self._detector = FaceDetector()
        self._state = HeadState()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._camera_ok = False
        self._camera_warned = False

    @property
    def mode(self) -> HeadMode:
        return self._state.mode

    @property
    def pan(self) -> float:
        return self._state.pan

    @property
    def tilt(self) -> float:
        return self._state.tilt

    def set_activity(self, activity: Activity | str):
        act = Activity(activity) if isinstance(activity, str) else activity
        if act != self._state.activity:
            self._state.activity = act
            if act in (Activity.LISTENING, Activity.SPEAKING, Activity.THINKING):
                self._state.idle_since = time.time()

    def manual_look(self, pan: int, tilt: int, hold: float = 2.5):
        """LLM tool override — hold pose then resume autonomy."""
        self._state.mode = HeadMode.MANUAL
        self._state.manual_until = time.time() + hold
        self._state.pan = float(pan)
        self._state.tilt = float(tilt)
        self._send_look_sync(int(pan), int(tilt))

    def look_center(self, hold: float = 1.5):
        self.manual_look(PAN_CENTER, TILT_CENTER, hold=hold)

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = loop.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _send_look_sync(self, pan: int, tilt: int) -> bool:
        try:
            r = self._http.get(
                f"http://{self.bot_ip}/look",
                params={"pan": pan, "tilt": tilt},
                timeout=1.5,
            )
            return r.status_code == 200
        except Exception as e:
            self._log("HEAD", f"look failed pan={pan} tilt={tilt}: {e}")
            return False

    async def _send_look(self, pan: float, tilt: float):
        pan_i, tilt_i = _clamp_int(pan, PAN_MIN, PAN_MAX), _clamp_int(tilt, TILT_MIN, TILT_MAX)
        self._state.pan, self._state.tilt = float(pan_i), float(tilt_i)
        await asyncio.to_thread(self._send_look_sync, pan_i, tilt_i)

    async def _capture_face(self) -> Optional[FaceObservation]:
        frame = await asyncio.to_thread(self._grab_frame)
        if not frame:
            if not self._camera_warned:
                self._log("HEAD", "camera offline — head moves without vision")
                self._camera_warned = True
            self._camera_ok = False
            return None
        self._camera_ok = True
        self._camera_warned = False
        return await asyncio.to_thread(self._detector.detect, frame)

    def _pick_idle_mode(self, now: float, face_recent: bool) -> HeadMode:
        idle_for = now - self._state.idle_since
        if face_recent:
            return HeadMode.TRACK
        if idle_for >= IDLE_SCAN_AFTER:
            return HeadMode.SCAN
        if idle_for >= GLANCE_IDLE_MIN and random.random() < 0.35:
            return HeadMode.GLANCE
        return HeadMode.FACE_USER

    def _pick_active_mode(self, face_recent: bool) -> HeadMode:
        """While conversing, prefer tracking or facing the user."""
        if face_recent:
            return HeadMode.TRACK
        if self._state.activity == Activity.SPEAKING and random.random() < 0.08:
            return HeadMode.GLANCE
        return HeadMode.FACE_USER

    def _start_glance(self):
        self._state.glance_return_pan = self._state.pan
        self._state.glance_return_tilt = self._state.tilt
        pan_off = random.choice([-1, 1]) * random.randint(14, 28)
        tilt_off = random.choice([-1, 1]) * random.randint(6, 16)
        self._state.pan = _clamp(self._state.pan + pan_off, PAN_MIN, PAN_MAX)
        self._state.tilt = _clamp(self._state.tilt + tilt_off, TILT_MIN, TILT_MAX)
        self._state.glance_until = time.time() + GLANCE_HOLD
        self._set_face("thinking")

    def _start_scan(self):
        self._state.scan_pan = 55.0
        self._state.scan_dir = 1
        self._state.pan = self._state.scan_pan
        self._state.tilt = float(SCAN_TILT)
        self._set_face("scanning")

    async def _tick_manual(self, now: float):
        if now >= self._state.manual_until:
            self._state.mode = HeadMode.FACE_USER
            self._log("HEAD", "manual override ended -> face_user")

    async def _tick_glance(self, now: float):
        if now < self._state.glance_until:
            await self._send_look(self._state.pan, self._state.tilt)
            return
        await self._send_look(self._state.glance_return_pan, self._state.glance_return_tilt)
        self._state.mode = HeadMode.FACE_USER
        self._set_face("neutral")
        self._log("HEAD", "glance done -> face_user")

    async def _tick_scan(self):
        face = await self._capture_face()
        if face:
            self._state.last_face_ts = time.time()
            pan, tilt = face_to_angles(face, float(PAN_CENTER), float(SCAN_TILT))
            self._state.mode = HeadMode.TRACK
            self._state.pan, self._state.tilt = pan, tilt
            self._log("HEAD", "scan found face -> track")
            return

        self._state.scan_pan += self._state.scan_dir * SCAN_PAN_STEP
        if self._state.scan_pan >= 125:
            self._state.scan_dir = -1
        elif self._state.scan_pan <= 55:
            self._state.scan_dir = 1
            # completed a sweep with no face
            self._state.mode = HeadMode.FACE_USER
            self._state.idle_since = time.time()
            self._set_face("neutral")
            self._log("HEAD", "scan complete, no face -> face_user")
            await self._send_look(PAN_CENTER, TILT_CENTER)
            return

        self._state.pan = self._state.scan_pan
        await self._send_look(self._state.pan, self._state.tilt)

    async def _tick_track(self):
        face = await self._capture_face()
        if not face:
            # lost face — drift back to center unless we're mid-conversation
            if self._state.activity == Activity.IDLE:
                self._state.mode = HeadMode.FACE_USER
                self._log("HEAD", "lost face (idle) -> face_user")
            target_pan, target_tilt = float(PAN_CENTER), float(TILT_CENTER)
        else:
            self._state.last_face_ts = time.time()
            target_pan, target_tilt = face_to_angles(face, self._state.pan, self._state.tilt)

        pan = smooth_toward(self._state.pan, target_pan, MAX_STEP)
        tilt = smooth_toward(self._state.tilt, target_tilt, MAX_STEP)
        await self._send_look(pan, tilt)

    async def _tick_face_user(self):
        pan = smooth_toward(self._state.pan, float(PAN_CENTER), MAX_STEP)
        tilt = smooth_toward(self._state.tilt, float(TILT_CENTER), MAX_STEP)
        await self._send_look(pan, tilt)

    async def _run_loop(self):
        self._log("HEAD", "autonomous gaze loop started")
        while self._running:
            try:
                now = time.time()
                st = self._state

                if st.mode == HeadMode.MANUAL:
                    await self._tick_manual(now)
                    await asyncio.sleep(TRACK_INTERVAL)
                    continue

                face_recent = (now - st.last_face_ts) < 2.5
                conversing = st.activity != Activity.IDLE

                if st.mode not in (HeadMode.GLANCE, HeadMode.SCAN):
                    desired = (
                        self._pick_active_mode(face_recent)
                        if conversing
                        else self._pick_idle_mode(now, face_recent)
                    )
                    if desired != st.mode:
                        if desired == HeadMode.GLANCE:
                            self._start_glance()
                        elif desired == HeadMode.SCAN:
                            self._start_scan()
                        st.mode = desired
                        self._log("HEAD", f"mode -> {st.mode.value}")

                if st.mode == HeadMode.GLANCE:
                    await self._tick_glance(now)
                elif st.mode == HeadMode.SCAN:
                    await self._tick_scan()
                elif st.mode == HeadMode.TRACK:
                    await self._tick_track()
                else:
                    await self._tick_face_user()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log("HEAD", f"tick error: {e}")

            await asyncio.sleep(TRACK_INTERVAL)
