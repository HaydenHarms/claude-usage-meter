"""Claude Usage Meter: a Windows XP style desktop widget for Claude Code plan usage.

Reads latest.json, which statusline.py writes whenever Claude Code refreshes
its status line, and checks the endpoint behind /usage every couple of minutes
(and when you click the refresh button) with the login Claude Code saved.
Right-click the window for options.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime, timedelta

import statusline

APP_TITLE = "Claude Usage Meter"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "latest.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
ICON_FILE = os.path.join(HERE, "icon.ico")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.CreateMutexW.restype = wintypes.HANDLE

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_MINIMIZEBOX = 0x00020000
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
SW_MINIMIZE, SW_RESTORE = 6, 9


# Own taskbar identity, so the button isn't grouped under Python with the
# Python icon and shows the window's live icon instead. Set before any window exists.
APP_ID = "HaydenHarms.ClaudeUsageMeter.Live"
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


WM_SETICON, WM_GETICON = 0x0080, 0x007F
user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL,
                                            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.CreateIconFromResourceEx.restype = ctypes.c_void_p


def to_hicon(img):
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    return user32.CreateIconFromResourceEx(data, len(data), True, 0x00030000,
                                           img.width, img.height, 0)


class TaskbarProgress:
    """Minimal ctypes binding for ITaskbarList3: fills the taskbar button like
    a progress bar (green = normal, yellow = paused, red = error)."""
    NONE, NORMAL, ERROR, PAUSED = 0, 2, 4, 8
    # vtable slots: IUnknown(0-2), ITaskbarList(3-7), ITaskbarList2(8), ITaskbarList3(9+)
    _HRINIT, _SET_VALUE, _SET_STATE, _SET_TOOLTIP = 3, 9, 10, 19

    def __init__(self):
        self.ptr = None
        try:
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)
            clsid, iid = (ctypes.c_byte * 16)(), (ctypes.c_byte * 16)()
            ole32.CLSIDFromString("{56FDF344-FD6D-11d0-958A-006097C9A090}", clsid)
            ole32.CLSIDFromString("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}", iid)
            ptr = ctypes.c_void_p()
            if ole32.CoCreateInstance(clsid, None, 1, iid, ctypes.byref(ptr)) == 0:
                self.ptr = ptr
                self._call(self._HRINIT, [])
        except OSError:
            self.ptr = None

    def _call(self, slot, argtypes, *args):
        vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
        return fn(self.ptr, *args)

    def set(self, hwnd, pct, state, tooltip):
        if not self.ptr:
            return
        self._call(self._SET_STATE, [wintypes.HWND, ctypes.c_int], hwnd, state)
        if state != self.NONE:
            self._call(self._SET_VALUE, [wintypes.HWND, ctypes.c_ulonglong, ctypes.c_ulonglong],
                       hwnd, max(1, round(pct)), 100)
        self._call(self._SET_TOOLTIP, [wintypes.HWND, wintypes.LPCWSTR], hwnd, tooltip)

# One instance only: a second launch brings the running window forward.
_mutex = kernel32.CreateMutexW(None, False, "ClaudeUsageMeter.SingleInstance")
if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    hwnd = user32.FindWindowW(None, APP_TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    sys.exit(0)

# Luna palette
BODY = "#ECE9D8"
FRAME = "#0055E5"
FRAME_EDGE = "#0831D9"
TITLE_STOPS = [(0.0, "#3D95FF"), (0.12, "#0A66F0"), (0.5, "#0055E5"),
               (0.88, "#0050DD"), (1.0, "#0A3FC2")]
GROUP_BORDER = "#D0D0BF"
GROUP_TEXT = "#0046D5"
TEXT = "#000000"
MUTED = "#6D6D6D"
BAR_BORDER = "#8E8F8F"
BAR_FILLS = {  # top, middle, bottom of each progress block
    "green": ("#C7F5C0", "#35CD2E", "#23A71D"),
    "amber": ("#FFE9A8", "#F2B70F", "#C98F00"),
    "red": ("#FFC0B0", "#E5401F", "#B32A10"),
}

W = 300
TITLE_H = 29
SIDE = 3
GROUP_H = 60
GROUP_GAP = 8
STATUS_H = 22
BTN = 21

FONT = ("Tahoma", 8)
FONT_BOLD = ("Tahoma", 8, "bold")
FONT_PCT = ("Tahoma", 10, "bold")
FONT_TITLE = ("Trebuchet MS", 10, "bold")


def lerp_hex(a, b, t):
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def gradient_color(stops, t):
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return lerp_hex(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 0)
    return stops[-1][1]


def work_area():
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def reset_text(resets_at, now):
    left = resets_at - now
    if left <= 0:
        return f"Reset at {clock(resets_at)}"
    if left >= 20 * 3600:
        return f"Resets {datetime.fromtimestamp(resets_at).strftime('%a')} {clock(resets_at)}"
    h, m = int(left // 3600), int(left % 3600 // 60)
    span = f"{h} hr {m} min" if h else f"{m} min {int(left % 60)} sec" if m < 5 else f"{m} min"
    return f"Resets in {span} ({clock(resets_at)})"


# Smallest request Claude Code can make on the subscription login: no tools,
# MCP servers, settings, or session file. Costs a few hundred Haiku tokens.
FETCH_ARGS = ["-p", "Reply with just: ok", "--model", "haiku", "--tools", "",
              "--strict-mcp-config", "--setting-sources", "", "--system-prompt", "Reply briefly.",
              "--disable-slash-commands", "--no-session-persistence",
              "--output-format", "stream-json", "--verbose"]
FETCH_TIMEOUT = 60


def claude_exe():
    path = shutil.which("claude")
    if path and path.lower().endswith(".cmd"):
        # Call the npm shim's target directly; cmd.exe mangles empty arguments.
        exe = os.path.join(os.path.dirname(path), "node_modules", "@anthropic-ai",
                           "claude-code", "bin", "claude.exe")
        if os.path.exists(exe):
            return exe
    return path


CREDENTIALS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
POLL_MINUTES = 0.5
FALLBACK_GAP = 15 * 60  # automatic checks run the headless request at most this often
KEEPALIVE_GAP = 10 * 60  # keep-alive sends at most one request per this long, even if it fails
WINDOW_SECONDS = 5 * 3600  # length of the session window keep-alive starts


class LoginExpired(RuntimeError):
    pass


def fetch_api():
    """Ask the endpoint behind /usage, with the login Claude Code saved. Free: no
    model runs. The token is only ever sent to api.anthropic.com. Raises
    LoginExpired when the token needs renewing, RuntimeError otherwise."""
    import urllib.error
    import urllib.request
    try:
        with open(CREDENTIALS, encoding="utf-8") as f:
            oauth = json.load(f).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        raise RuntimeError("no Claude Code login")
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("no Claude Code login")
    if oauth.get("expiresAt") and oauth["expiresAt"] / 1000 < time.time() + 60:
        raise LoginExpired("login expired")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LoginExpired("login expired")
        raise RuntimeError(f"HTTP {e.code}")
    except (OSError, ValueError):
        raise RuntimeError("offline")
    limits = {}
    for key in ("five_hour", "seven_day"):
        win = body.get(key) or {}
        if win.get("utilization") is None:
            continue
        resets_at = win.get("resets_at")
        if resets_at:
            resets_at = round(datetime.fromisoformat(resets_at).timestamp())
        limits[key] = {"used_percentage": float(win["utilization"]), "resets_at": resets_at}
    if not limits:
        raise RuntimeError("no usage in reply")
    return limits


def fetch_headless():
    """Run a headless Claude Code request and return its rate limits in the
    status line format. Claude Code renews an expired login as part of it.
    Raises RuntimeError with a short reason on failure."""
    exe = claude_exe()
    if not exe:
        raise RuntimeError("claude not found")
    try:
        proc = subprocess.run([exe] + FETCH_ARGS, cwd=HERE, capture_output=True,
                              timeout=FETCH_TIMEOUT, stdin=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError("timed out")
    except OSError:
        raise RuntimeError("couldn't start claude")
    limits, result = {}, None
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "rate_limit_event":
            windows = (event.get("rate_limit_info") or {}).get("unifiedWindows") or {}
            for key, win in windows.items():
                if win.get("utilization") is not None:
                    limits[key] = {"used_percentage": round(win["utilization"] * 100, 1),
                                   "resets_at": win.get("resetsAt")}
        elif event.get("type") == "result":
            result = event
    if limits:
        return limits
    if result and result.get("is_error"):
        raise RuntimeError(str(result.get("result") or "request failed")[:40])
    raise RuntimeError("no usage in reply")


def ago(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr ago"
    return f"{int(seconds // 86400)} days ago"


class Meter:
    WINDOWS = (("five_hour", "Current session (5-hour)"),
               ("seven_day", "Weekly limit (all models)"),
               ("spend_limit", "Spend limit"))

    def __init__(self):
        self.config = load_json(CONFIG_FILE, {})
        self.data = None
        self.data_mtime = None
        self.drag_from = None
        self.fetching = None  # background thread while a refresh runs
        self.fetch_result = None
        self.notice = None  # (text, shown until) for refresh errors
        self.fetch_manual = False
        self.last_fallback = 0.0
        self.last_keepalive = 0.0

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        if os.path.exists(ICON_FILE):
            self.root.iconbitmap(ICON_FILE)
        self.root.overrideredirect(True)
        self.root.configure(bg=FRAME_EDGE)
        self.topmost = tk.BooleanVar(value=self.config.get("topmost", True))
        self.root.attributes("-topmost", self.topmost.get())

        self.canvas = tk.Canvas(self.root, width=W, height=100, bg=BODY,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        self.menu = tk.Menu(self.root, tearoff=0, font=FONT)
        self.menu.add_checkbutton(label="Always on top", variable=self.topmost,
                                  command=self.toggle_topmost)
        self.keep_alive = tk.BooleanVar(value=self.config.get("keep_alive", False))
        self.settings_win = None
        self.fetch_keepalive = False
        self.menu.add_command(label="Settings...", command=self.open_settings)
        self.menu.add_command(label="Move to bottom-right", command=self.snap_corner)
        self.menu.add_command(label="Refresh now", command=self.refresh)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self.quit)
        self.canvas.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.taskbar = TaskbarProgress()
        self.taskbar_key = None
        self.icon_key = None
        self.hicons = {}  # icon level -> (big, small); never destroyed, the taskbar may still use them

        self.height = None
        self.reload(force=True)
        self.draw()
        self.place_initial()
        self.root.after(10, self.show_in_taskbar)
        self.root.after(1000, self.tick)
        self.root.after(2000, self.auto_refresh)

    # ---- window plumbing ----
    def hwnd(self):
        return user32.GetParent(self.root.winfo_id())

    def show_in_taskbar(self):
        # Borderless Tk windows are tool windows by default; make this a real
        # app window so it gets a taskbar button and can be minimized.
        hwnd = self.hwnd()
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX)
        self.root.withdraw()
        self.root.after(10, self.root.deiconify)
        # The taskbar button is recreated by the restyle; push progress again once it exists.
        self.taskbar_key = None
        self.root.after(500, self.draw)

    def minimize(self):
        user32.ShowWindow(self.hwnd(), SW_MINIMIZE)

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        self.save_config()

    def corner_position(self):
        left, top, right, bottom = work_area()
        return right - W - 12, bottom - self.height - 12

    def place_initial(self):
        x, y = self.corner_position()
        pos = self.config.get("pos")
        if pos:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            if -W // 2 <= pos[0] <= sw - 40 and 0 <= pos[1] <= sh - 40:
                x, y = pos
        self.root.geometry(f"{W}x{self.height}+{x}+{y}")

    def snap_corner(self):
        x, y = self.corner_position()
        self.root.geometry(f"{W}x{self.height}+{x}+{y}")
        self.config.pop("pos", None)
        self.save_config()

    def save_config(self):
        self.config["topmost"] = self.topmost.get()
        self.config["keep_alive"] = self.keep_alive.get()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f)
        except OSError:
            pass

    def start_drag(self, e):
        self.drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def do_drag(self, e):
        if self.drag_from:
            self.root.geometry(f"+{e.x_root - self.drag_from[0]}+{e.y_root - self.drag_from[1]}")

    def end_drag(self, e):
        if self.drag_from:
            self.drag_from = None
            self.config["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            self.save_config()

    def quit(self):
        self.root.destroy()

    # ---- data ----
    def reload(self, force=False):
        try:
            mtime = os.path.getmtime(DATA_FILE)
        except OSError:
            mtime = None
        if force or mtime != self.data_mtime:
            self.data_mtime = mtime
            self.data = load_json(DATA_FILE, None) if mtime else None

    def auto_refresh(self):
        minutes = self.config.get("poll_minutes", POLL_MINUTES)
        if minutes:
            self.refresh(manual=False)
            self.root.after(int(minutes * 60000), self.auto_refresh)

    def keepalive_hours(self):
        """Hours between keep-alive requests, or None to send one whenever the
        5-hour window has run out."""
        try:
            hours = float(self.config.get("keep_alive_hours") or 0)
        except (TypeError, ValueError):
            return None
        return hours if hours > 0 else None

    def keepalive_start(self):
        """Daily start time as "HH:MM", or None. Older configs stored a one-off
        timestamp; its time of day carries over."""
        start = self.config.get("keep_alive_start")
        if not start and self.config.get("keep_alive_anchor"):
            start = datetime.fromtimestamp(self.config["keep_alive_anchor"]).strftime("%H:%M")
        return start or None

    def cycle_bounds(self, now):
        """(today's cycle start, next cycle start) as timestamps around now, or
        None when no daily start time is set."""
        start = self.keepalive_start()
        if not start:
            return None
        try:
            hh, mm = (int(p) for p in start.split(":"))
            first = datetime.fromtimestamp(now).replace(hour=hh, minute=mm,
                                                        second=0, microsecond=0)
        except ValueError:
            return None
        if first.timestamp() > now:
            first -= timedelta(days=1)
        return first.timestamp(), (first + timedelta(days=1)).timestamp()

    def keepalive_due(self, now):
        """True when keep-alive is on and a request is due: every N hours if a
        cycle is set, otherwise once the 5-hour window's reset time has passed
        (or no window has ever been seen). With a daily start time, nothing is
        sent in the 5 hours before it, so no window is still running then."""
        if not self.keep_alive.get() or now - self.last_keepalive < KEEPALIVE_GAP:
            return False
        hours = self.keepalive_hours()
        bounds = self.cycle_bounds(now)
        if bounds:
            start, next_start = bounds
            if now >= next_start - WINDOW_SECONDS:
                return False  # quiet until the next start time
            if hours:
                # slots at start + k * hours, restarting each day; send once per slot
                slot = start + (now - start) // (hours * 3600) * hours * 3600
                return self.config.get("last_keepalive", 0) < slot
        if hours:
            return now - self.config.get("last_keepalive", 0) >= hours * 3600
        win = ((self.data or {}).get("rate_limits") or {}).get("five_hour")
        resets_at = (win or {}).get("resets_at")
        return not resets_at or resets_at <= now

    def refresh(self, manual=True, keepalive=False):
        if self.fetching:
            return
        if manual:
            self.notice = None
        self.fetch_manual = manual
        self.fetch_result = None
        # An expired login is renewed by one headless request; automatic checks
        # don't repeat that more than every FALLBACK_GAP if it keeps failing.
        fallback = manual or time.time() - self.last_fallback > FALLBACK_GAP
        self.fetch_keepalive = keepalive
        if keepalive:
            self.last_keepalive = time.time()
        self.fetching = threading.Thread(target=self.fetch_worker,
                                         args=(fallback, manual, keepalive), daemon=True)
        self.fetching.start()
        if manual:
            self.draw()
        self.root.after(100, self.poll_fetch)

    def fetch_worker(self, fallback, manual, keepalive=False):
        # Runs off the Tk thread; poll_fetch picks up the result. Automatic
        # checks fall back to the headless request only for an expired login;
        # the button falls back whenever the endpoint check fails. Keep-alive
        # goes straight to the headless request: it's a real request, and that
        # is what starts a new 5-hour window.
        try:
            if keepalive:
                self.fetch_result = ("ok", fetch_headless())
                return
            try:
                self.fetch_result = ("ok", fetch_api())
            except RuntimeError as e:
                if not fallback or not (manual or isinstance(e, LoginExpired)):
                    raise
                self.last_fallback = time.time()
                self.fetch_result = ("ok", fetch_headless())
        except RuntimeError as e:
            self.fetch_result = ("error", str(e))

    def poll_fetch(self):
        if self.fetching.is_alive():
            self.root.after(100, self.poll_fetch)
            return
        self.fetching = None
        status, value = self.fetch_result or ("error", "request failed")
        if status == "ok":
            statusline.save(value, fresh=True)
            self.notice = None
            if self.fetch_keepalive:
                self.config["last_keepalive"] = time.time()
                self.save_config()
        elif self.fetch_manual:
            self.notice = (f"Refresh failed: {value}", time.time() + 15)
        self.reload(force=True)
        self.draw()

    def tick(self):
        self.reload()
        if not self.fetching and self.keepalive_due(time.time()):
            self.refresh(manual=False, keepalive=True)
        self.draw()
        self.root.after(1000, self.tick)

    def rows(self, now):
        limits = (self.data or {}).get("rate_limits") or {}
        out = []
        for key, label in self.WINDOWS:
            win = limits.get(key)
            if not win or win.get("used_percentage") is None:
                continue
            pct = float(win["used_percentage"])
            resets_at = win.get("resets_at")
            if resets_at and resets_at <= now:
                pct = 0.0
            out.append((label, pct, resets_at))
        return out

    # ---- drawing ----
    def draw(self):
        now = time.time()
        rows = self.rows(now)
        n = max(len(rows), 1)
        height = TITLE_H + GROUP_GAP + n * (GROUP_H + GROUP_GAP) + STATUS_H + SIDE
        if height != self.height:
            self.height = height
            self.canvas.config(height=height)
            if self.root.winfo_ismapped():
                self.root.geometry(f"{W}x{height}")

        c = self.canvas
        c.delete("all")
        self.draw_frame(height)
        y = TITLE_H + GROUP_GAP
        if rows:
            for label, pct, resets_at in rows:
                self.draw_group(y, label, pct, reset_text(resets_at, now) if resets_at else "")
                y += GROUP_H + GROUP_GAP
        else:
            self.draw_empty(y)
        self.draw_status(height, now)
        self.update_taskbar(rows)

    def update_taskbar(self, rows):
        session = next((r for r in rows if r[0] == self.WINDOWS[0][1]), None)
        pct = session[1] if session else None
        if pct is None:
            state = TaskbarProgress.NONE
        else:
            state = (TaskbarProgress.ERROR if pct >= 90 else
                     TaskbarProgress.PAUSED if pct >= 75 else TaskbarProgress.NORMAL)
        tooltip = "  |  ".join(f"{label.split(' (')[0]}: {p:.0f}%" for label, p, _ in rows) or APP_TITLE
        key = (None if pct is None else round(pct), state, tooltip)
        if key != self.taskbar_key and self.root.winfo_ismapped():
            self.taskbar_key = key
            self.taskbar.set(self.hwnd(), pct or 0, state, tooltip)
        self.update_icon(pct)
        self.send_icon()  # no-op unless something replaced our icon

    def update_icon(self, pct):
        # Taskbar icon: a mini XP progress bar filled to the session %.
        key = None if pct is None else round(pct / 5) * 5
        if key == self.icon_key:
            return
        self.icon_key = key
        if key in self.hicons:
            self.send_icon()
            return
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return
        fill = BAR_FILLS["red" if (pct or 0) >= 90 else "amber" if (pct or 0) >= 75 else "green"]
        images = []
        for size in (48, 32, 16):
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            s = size / 32
            d.rounded_rectangle((0, 3 * s, size - 1, size - 3 * s - 1), max(1, round(3 * s)),
                                fill="#0055E5", outline="#0831D9")
            x0, y0, x1, y1 = round(3 * s), round(8 * s), size - round(3 * s) - 1, size - round(8 * s) - 1
            d.rectangle((x0, y0, x1, y1), fill="white", outline="#8E8F8F")
            if pct:
                end = x0 + 2 + max(1, round((x1 - x0 - 3) * min(pct, 100) / 100))
                block, gap = (max(2, round(4 * s)), max(1, round(s))) if size >= 32 else (end, 0)
                bx = x0 + 2
                while bx < end:
                    bw = min(block, end - bx)
                    d.rectangle((bx, y0 + 2, bx + bw - 1, y1 - 2), fill=fill[1])
                    d.line((bx, y0 + 2, bx + bw - 1, y0 + 2), fill=fill[0])
                    bx += block + gap
            images.append(img)
        # Set the icons with Win32 directly: Tk's iconphoto frees the previous
        # icon, and the taskbar falls back to the Python icon when that happens.
        self.hicons[key] = (to_hicon(images[0]), to_hicon(images[2]))
        self.send_icon()

    def send_icon(self):
        icons = self.hicons.get(self.icon_key)
        hwnd = self.hwnd()
        if icons and user32.SendMessageW(hwnd, WM_GETICON, 1, 0) != icons[0]:
            user32.SendMessageW(hwnd, WM_SETICON, 1, icons[0])  # ICON_BIG
            user32.SendMessageW(hwnd, WM_SETICON, 0, icons[1])  # ICON_SMALL

    def draw_frame(self, height):
        c = self.canvas
        # title bar gradient
        for i in range(TITLE_H):
            c.create_line(0, i, W, i, fill=gradient_color(TITLE_STOPS, i / (TITLE_H - 1)),
                          tags="title")
        c.create_line(0, 0, W, 0, fill="#7EB4FF", tags="title")
        # side and bottom frame
        c.create_rectangle(0, TITLE_H, SIDE - 1, height, fill=FRAME, outline="", tags="title")
        c.create_rectangle(W - SIDE, TITLE_H, W, height, fill=FRAME, outline="", tags="title")
        c.create_rectangle(0, height - SIDE, W, height, fill=FRAME, outline="", tags="title")
        c.create_rectangle(0, 0, W - 1, height - 1, outline=FRAME_EDGE)
        # icon + caption
        self.draw_mini_icon(7, 7)
        c.create_text(28, TITLE_H // 2 + 1, text=APP_TITLE, anchor="w",
                      font=FONT_TITLE, fill="#0A1E6E", tags="title")
        c.create_text(27, TITLE_H // 2, text=APP_TITLE, anchor="w",
                      font=FONT_TITLE, fill="white", tags="title")
        for tag in ("title",):
            c.tag_bind(tag, "<ButtonPress-1>", self.start_drag)
            c.tag_bind(tag, "<B1-Motion>", self.do_drag)
            c.tag_bind(tag, "<ButtonRelease-1>", self.end_drag)
        # buttons
        by = (TITLE_H - BTN) // 2
        close_x = W - SIDE - 3 - BTN
        min_x = close_x - BTN - 2
        refresh_x = min_x - BTN - 2
        settings_x = refresh_x - BTN - 2
        self.draw_button(settings_x, by, ("#6FA8FF", "#2863E4"), "settings", self.open_settings)
        self.draw_button(refresh_x, by, ("#6FA8FF", "#2863E4"), "refresh", self.refresh)
        self.draw_button(min_x, by, ("#6FA8FF", "#2863E4"), "min", self.minimize)
        self.draw_button(close_x, by, ("#F09A7C", "#C9431F"), "close", self.quit)

    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = self.settings_win = tk.Toplevel(self.root)
        win.title("Settings")
        win.resizable(False, False)
        win.transient(self.root)
        win.attributes("-topmost", True)
        hours = self.keepalive_hours()
        mode = tk.StringVar(value="interval" if hours else "reset")
        hours_var = tk.StringVar(value=f"{hours:g}" if hours else "5")

        box = tk.Frame(win, padx=12, pady=10)
        box.pack()
        tk.Checkbutton(box, text="Keep my 5-hour window running", font=FONT_BOLD,
                       variable=self.keep_alive).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(box, font=FONT, justify="left", fg="#555555", text=(
            "Sends one tiny headless Claude request so a new window starts\n"
            "even when you're away. Uses a sliver of your plan each time."
        )).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        tk.Radiobutton(box, text="When the window runs out", font=FONT, variable=mode,
                       value="reset").grid(row=2, column=0, columnspan=3, sticky="w")
        tk.Radiobutton(box, text="Every", font=FONT, variable=mode,
                       value="interval").grid(row=3, column=0, sticky="w")
        tk.Spinbox(box, from_=0.5, to=24, increment=0.5, width=5, font=FONT,
                   textvariable=hours_var, command=lambda: mode.set("interval")
                   ).grid(row=3, column=1)
        tk.Label(box, text="hours", font=FONT).grid(row=3, column=2, sticky="w")

        start = self.keepalive_start()
        use_start = tk.BooleanVar(value=bool(start))
        start_var = tk.StringVar(value=start or "05:00")
        tk.Checkbutton(box, text="Start the cycle daily at", font=FONT, variable=use_start
                       ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        tk.Entry(box, width=6, font=FONT, textvariable=start_var
                 ).grid(row=4, column=1, pady=(8, 0))
        tk.Label(box, text="(24-hour)", font=FONT, fg="#555555"
                 ).grid(row=4, column=2, sticky="w", pady=(8, 0))
        if start:
            quiet = (datetime.strptime(start, "%H:%M")
                     - timedelta(seconds=WINDOW_SECONDS)).strftime("%H:%M")
            when = f"Nothing is sent from {quiet} to {start} each day."
        else:
            when = "No start time: runs around the clock from when you save."
        tk.Label(box, text=when, font=FONT, fg="#555555"
                 ).grid(row=5, column=0, columnspan=3, sticky="w")

        def save():
            if use_start.get():
                try:
                    hh, mm = (int(p) for p in start_var.get().strip().split(":"))
                    value = f"{hh:02d}:{mm:02d}"
                    datetime.strptime(value, "%H:%M")
                except ValueError:
                    start_var.set("05:00")
                    return
                if value != self.keepalive_start():
                    self.config["last_keepalive"] = 0
                self.config["keep_alive_start"] = value
            else:
                self.config.pop("keep_alive_start", None)
            self.config.pop("keep_alive_anchor", None)
            if mode.get() == "interval":
                try:
                    value = float(hours_var.get())
                except ValueError:
                    value = 0
                if not 0.25 <= value <= 24:
                    hours_var.set("5")
                    return
                self.config["keep_alive_hours"] = value
            else:
                self.config.pop("keep_alive_hours", None)
            self.save_config()
            win.destroy()

        row = tk.Frame(box)
        row.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
        tk.Button(row, text="Save", width=8, font=FONT, command=save).pack(side="left", padx=4)
        tk.Button(row, text="Cancel", width=8, font=FONT,
                  command=lambda: (self.keep_alive.set(self.config.get("keep_alive", False)),
                                   win.destroy())).pack(side="left")
        win.update_idletasks()
        win.geometry(f"+{max(self.root.winfo_x() - win.winfo_width() + W, 0)}"
                     f"+{max(self.root.winfo_y() - win.winfo_height() - 8, 0)}")

    def draw_mini_icon(self, x, y):
        c = self.canvas
        c.create_rectangle(x, y, x + 15, y + 15, fill="white", outline="#0A3FC2", tags="title")
        c.create_rectangle(x + 2, y + 9, x + 13, y + 13, fill="#35CD2E", outline="", tags="title")
        c.create_rectangle(x + 2, y + 4, x + 9, y + 7, fill="#35CD2E", outline="", tags="title")

    def draw_button(self, x, y, colors, kind, action):
        c = self.canvas
        tag = f"btn_{kind}"
        for i in range(BTN):
            c.create_line(x, y + i, x + BTN, y + i,
                          fill=lerp_hex(colors[0], colors[1], i / (BTN - 1)), tags=tag)
        c.create_rectangle(x, y, x + BTN - 1, y + BTN - 1, outline="white", tags=tag)
        if kind == "close":
            c.create_line(x + 6, y + 6, x + 15, y + 15, fill="white", width=2, tags=tag)
            c.create_line(x + 15, y + 6, x + 6, y + 15, fill="white", width=2, tags=tag)
        elif kind == "settings":
            # gear: ring, hub and eight teeth
            cx, cy = x + 10, y + 10
            for dx, dy in ((0, -7), (0, 7), (-7, 0), (7, 0), (-5, -5), (5, 5), (-5, 5), (5, -5)):
                c.create_line(cx + dx * 0.6, cy + dy * 0.6, cx + dx, cy + dy,
                              fill="white", width=2, tags=tag)
            c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="white", width=2, tags=tag)
            c.create_oval(cx - 1, cy - 1, cx + 1, cy + 1, fill="white", outline="", tags=tag)
        elif kind == "refresh":
            # circular arrow, gap at the top right
            cx, cy = x + 10, y + 11
            c.create_arc(cx - 5, cy - 5, cx + 5, cy + 5, start=70, extent=290, style="arc",
                         outline="white", width=2, tags=tag)
            c.create_polygon(cx + 2, cy - 1, cx + 8, cy - 1, cx + 5, cy - 5,
                             fill="white", outline="", tags=tag)
        else:
            c.create_rectangle(x + 5, y + 13, x + 11, y + 15, fill="white", outline="", tags=tag)
        c.tag_bind(tag, "<ButtonRelease-1>", lambda e: action())

    def draw_groupbox(self, y, h, label):
        c = self.canvas
        x0, x1 = SIDE + 8, W - SIDE - 8
        c.create_rectangle(x0, y + 6, x1, y + h, outline=GROUP_BORDER)
        text_id = c.create_text(x0 + 8, y + 6, text=label, anchor="w", font=FONT, fill=GROUP_TEXT)
        bx0, _, bx1, _ = c.bbox(text_id)
        c.create_rectangle(bx0 - 2, y + 5, bx1 + 1, y + 7, fill=BODY, outline="")
        c.tag_raise(text_id)
        return x0, x1

    def draw_group(self, y, label, pct, reset):
        c = self.canvas
        x0, x1 = self.draw_groupbox(y, GROUP_H, label)
        bar_x0, bar_x1 = x0 + 10, x1 - 52
        bar_y0, bar_y1 = y + 18, y + 34
        self.draw_bar(bar_x0, bar_y0, bar_x1, bar_y1, pct)
        c.create_text(x1 - 10, (bar_y0 + bar_y1) // 2, text=f"{pct:.0f}%", anchor="e",
                      font=FONT_PCT, fill="#C9431F" if pct >= 90 else TEXT)
        c.create_text(bar_x0, y + 46, text=reset, anchor="w", font=FONT, fill=MUTED)

    def draw_bar(self, x0, y0, x1, y1, pct):
        c = self.canvas
        c.create_rectangle(x0, y0, x1, y1, fill="white", outline=BAR_BORDER)
        c.create_line(x0 + 1, y0 + 1, x1, y0 + 1, fill="#E6E6E6")
        fill = BAR_FILLS["red" if pct >= 90 else "amber" if pct >= 75 else "green"]
        inner_x0, inner_x1 = x0 + 3, x1 - 2
        width = (inner_x1 - inner_x0) * min(max(pct, 0), 100) / 100
        block, gap = 7, 2
        bx = inner_x0
        top, bottom = y0 + 3, y1 - 2
        mid = (top + bottom) // 2
        while bx < inner_x0 + width - 1:
            bw = min(block, inner_x0 + width - bx)
            for yy in range(top, bottom):
                t = (yy - top) / max(bottom - top - 1, 1)
                col = lerp_hex(fill[0], fill[1], t * 2) if yy < mid else lerp_hex(fill[1], fill[2], (t - 0.5) * 2)
                c.create_line(bx, yy, bx + bw, yy, fill=col)
            bx += block + gap

    def draw_empty(self, y):
        c = self.canvas
        x0, x1 = self.draw_groupbox(y, GROUP_H, "Waiting for usage data")
        c.create_text(x0 + 10, y + 26, anchor="w", font=FONT, fill=TEXT,
                      text="Send a message in Claude Code and the")
        c.create_text(x0 + 10, y + 42, anchor="w", font=FONT, fill=TEXT,
                      text="meter fills in from the status line.")

    def draw_status(self, height, now):
        c = self.canvas
        top = height - SIDE - STATUS_H
        c.create_line(SIDE, top, W - SIDE, top, fill="#ACA899")
        c.create_line(SIDE, top + 1, W - SIDE, top + 1, fill="white")
        if self.notice and now > self.notice[1]:
            self.notice = None
        if self.fetching and self.fetch_manual:
            text, color = "Checking usage...", TEXT
        elif self.notice:
            text, color = self.notice[0], "#C9431F"
        elif self.data:
            age = now - self.data.get("captured_at", now)
            text = f"Updated {ago(age)}"
            if self.data.get("model"):
                text += f"   |   {self.data['model']}"
            color = MUTED if age > 1800 else TEXT
        else:
            text, color = "No data yet", MUTED
        c.create_text(SIDE + 8, top + STATUS_H // 2 + 1, text=text, anchor="w",
                      font=FONT, fill=color)
        # size grip dots
        gx, gy = W - SIDE - 4, height - SIDE - 4
        for dx, dy in ((0, 0), (-4, 0), (-8, 0), (0, -4), (-4, -4), (0, -8)):
            c.create_rectangle(gx + dx - 1, gy + dy - 1, gx + dx, gy + dy, fill="#ACA899", outline="")

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Meter().run()
