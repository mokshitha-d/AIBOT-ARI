"""
robot_actions.py - Mac ("Robot") actions the AI can trigger.

All actions run as your normal user - no admin, no Homebrew needed. Safe by design:
  - open_app is whitelisted (won't launch arbitrary apps)
  - save_note only writes into ~/RobotNotes/ (filename sanitized)
  - search_web only opens a browser search URL
"""

import datetime
import os
import re
import subprocess
import urllib.parse

NOTES_DIR = os.path.expanduser("~/RobotNotes")

# apps the bot is allowed to open (lowercase). Add your own here.
APP_ALLOWLIST = {
    "safari", "google chrome", "notes", "calendar", "reminders", "music",
    "spotify", "finder", "terminal", "cursor", "arduino ide", "messages",
    "mail", "preview", "system settings", "visual studio code",
}


def current_time():
    """Human-readable current local time (used for both the tool and per-turn context)."""
    now = datetime.datetime.now()
    hour = now.strftime("%I").lstrip("0") or "12"
    return now.strftime(f"%A, %B %d, %Y, {hour}:%M %p")


def search_web(query):
    query = (query or "").strip()
    if not query:
        return "No search query given."
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    try:
        subprocess.run(["open", url], check=False)
        return f"Opened a browser search for: {query}"
    except Exception as e:
        return f"Could not open browser: {e}"


def save_note(title, text):
    title = (title or "note").strip()
    text = (text or "").strip()
    os.makedirs(NOTES_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip() or "note"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(NOTES_DIR, f"{stamp}_{safe}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{text}\n")
        return f"Saved note: {path}"
    except Exception as e:
        return f"Could not save note: {e}"


def open_app(name):
    name = (name or "").strip()
    if name.lower() not in APP_ALLOWLIST:
        return f"'{name}' isn't in the allowed apps list."
    try:
        subprocess.run(["open", "-a", name], check=False)
        return f"Opened {name}"
    except Exception as e:
        return f"Could not open {name}: {e}"


def _run(cmd, timeout=5):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def system_status():
    """Battery, storage, CPU load, and memory. All read-only."""
    parts = []
    try:
        out = _run(["pmset", "-g", "batt"], 3)
        m = re.search(r"(\d+)%", out)
        pct = m.group(1) + "%" if m else "?"
        state = "charging" if ("AC Power" in out or "charging" in out.lower()) else "on battery"
        parts.append(f"Battery {pct} ({state})")
    except Exception:
        parts.append("Battery: n/a")
    try:
        cols = _run(["df", "-h", "/"], 3).strip().splitlines()[1].split()
        parts.append(f"Disk {cols[2]} used of {cols[1]} ({cols[4]} full, {cols[3]} free)")
    except Exception:
        parts.append("Disk: n/a")
    try:
        out = _run(["top", "-l", "1", "-n", "0"], 6)
        load = re.search(r"Load Avg:\s*([\d.,\s]+)", out)
        memline = re.search(r"PhysMem:\s*(.+)", out)
        if load:
            parts.append(f"CPU load {load.group(1).strip()}")
        if memline:
            parts.append("Memory: " + memline.group(1).strip())
    except Exception:
        pass
    return "; ".join(parts)


def get_wifi():
    """Current WiFi network (best effort) + IP address. Read-only."""
    info = []
    try:
        out = _run(["networksetup", "-getairportnetwork", "en0"], 3).strip()
        if "Current Wi-Fi Network:" in out:
            info.append("Network: " + out.split("Current Wi-Fi Network:")[1].strip())
        elif out:
            info.append(out)  # e.g. "You are not associated..."
    except Exception:
        pass
    for iface in ("en0", "en1"):
        try:
            ip = _run(["ipconfig", "getifaddr", iface], 3).strip()
            if ip:
                info.append(f"IP {ip}")
                break
        except Exception:
            pass
    return "; ".join(info) if info else "WiFi info not available (newer macOS may block the network name)"


def list_running_apps():
    """Open user apps (best effort) + top CPU processes. Read-only."""
    result = []
    try:
        script = ('tell application "System Events" to get name of '
                  '(every process whose background only is false)')
        out = _run(["osascript", "-e", script], 5).strip()
        apps = [a.strip() for a in out.split(",") if a.strip()]
        if apps:
            result.append("Open apps: " + ", ".join(apps))
    except Exception:
        pass
    try:
        lines = _run(["ps", "-Ao", "pcpu,comm", "-r"], 3).strip().splitlines()[1:6]
        tops = []
        for ln in lines:
            cpu, _, comm = ln.strip().partition(" ")
            name = os.path.basename(comm.strip())
            tops.append(f"{name} {cpu}%")
        if tops:
            result.append("Top CPU: " + ", ".join(tops))
    except Exception:
        pass
    return " | ".join(result) if result else "No process info available"


if __name__ == "__main__":
    print(current_time())
    print(system_status())
    print(get_wifi())
    print(list_running_apps())
