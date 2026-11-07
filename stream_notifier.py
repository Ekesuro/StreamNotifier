"""Stream Notifier - tray app that tells you when Twitch / Kick streamers go live.

UI: pywebview (Edge WebView2) pages in ./ui, tray icon via pystray, toasts via winotify.
Uses the official Twitch Helix API and the official Kick public API (app keys entered in Settings).
Settings and the streamer list are stored in %APPDATA%\\StreamNotifier\\config.json.
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg

import pystray
import webview
from PIL import Image, ImageDraw
from winotify import Notification, audio

APP = "Stream Notifier"
FLYOUT_TITLE = "Stream Notifier - Quick view"
FROZEN = getattr(sys, "frozen", False)  # True when running as a PyInstaller .exe
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
UI_DIR = os.path.join(getattr(sys, "_MEIPASS", HERE), "ui")
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "StreamNotifier")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
ICON_PNG = os.path.join(DATA_DIR, "icon.png")
ICON_ICO = os.path.join(DATA_DIR, "icon.ico")
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup",
                           "Stream Notifier.lnk")
UA = "StreamNotifier/2.0 (personal desktop app)"
PLATFORM_LABEL = {"twitch": "Twitch", "kick": "Kick"}
DEV_LINKS = {"twitch": "https://dev.twitch.tv/console/apps", "kick": "https://kick.com/settings/developer"}

DEFAULT_STREAMERS = [
    ("twitch", "vitkuson"), ("twitch", "barbee02"), ("twitch", "lada2much"), ("twitch", "meepochinno"),
    ("twitch", "lolsweetcz"), ("twitch", "wesperfey"), ("kick", "nezbedaaa"), ("kick", "snugglyvr"),
]


def stream_url(platform, name):
    return f"https://www.twitch.tv/{name}" if platform == "twitch" else f"https://kick.com/{name}"


# ---------------------------------------------------------------- config

def load_config():
    cfg = {
        "twitch": {"client_id": "", "client_secret": ""},
        "kick": {"client_id": "", "client_secret": ""},
        "interval": 60,
        "theme": "system",
        "streamers": [{"platform": p, "name": n, "notify": True} for p, n in DEFAULT_STREAMERS],
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- APIs

def http_json(url, form=None, headers=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


class OAuthApi:
    """Client-credentials (app access token) helper with automatic token refresh."""
    token_url = ""

    def __init__(self):
        self._token = None
        self._creds = None

    def headers(self, client_id, token):
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, creds, url):
        cid, secret = creds["client_id"].strip(), creds["client_secret"].strip()
        if (cid, secret) != self._creds:
            self._creds, self._token = (cid, secret), None
        for attempt in range(2):
            if not self._token:
                d = http_json(self.token_url, form={"client_id": cid, "client_secret": secret,
                                                    "grant_type": "client_credentials"})
                self._token = d["access_token"]
            try:
                return http_json(url, headers=self.headers(cid, self._token))
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._token = None
                    continue
                raise


def cache_bust(url):
    if not url:
        return url
    return url + ("&" if "?" in url else "?") + f"t={int(time.time() // 120)}"


class TwitchApi(OAuthApi):
    token_url = "https://id.twitch.tv/oauth2/token"

    def headers(self, client_id, token):
        return {**super().headers(client_id, token), "Client-Id": client_id}

    def check(self, creds, names):
        out = {}
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            q = "&".join("login=" + urllib.parse.quote(n) for n in chunk)
            users = {u["login"].lower(): u for u in self.get(creds, "https://api.twitch.tv/helix/users?" + q)["data"]}
            q = "&".join("user_login=" + urllib.parse.quote(n) for n in chunk)
            live = {s["user_login"].lower(): s
                    for s in self.get(creds, "https://api.twitch.tv/helix/streams?first=100&" + q)["data"]}
            for n in chunk:
                u = users.get(n)
                if not u:
                    out[n] = {"state": "notfound"}
                    continue
                base = {"display": u["display_name"], "avatar": u.get("profile_image_url", "")}
                s = live.get(n)
                if s:
                    thumb = (s.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")
                    out[n] = {**base, "state": "live", "title": s.get("title", ""), "game": s.get("game_name", ""),
                              "viewers": s.get("viewer_count", 0), "started": s.get("started_at", ""),
                              "thumb": cache_bust(thumb)}
                else:
                    out[n] = {**base, "state": "offline"}
        return out


class KickApi(OAuthApi):
    token_url = "https://id.kick.com/oauth/token"

    def __init__(self):
        super().__init__()
        self._avatars = {}  # broadcaster_user_id -> picture url ("" = unavailable)

    def _load_avatars(self, creds, ids):
        missing = [i for i in ids if i not in self._avatars]
        if not missing:
            return
        try:
            q = "&".join(f"id={i}" for i in missing)
            for u in self.get(creds, "https://api.kick.com/public/v1/users?" + q).get("data") or []:
                self._avatars[u.get("user_id")] = u.get("profile_picture") or ""
        except Exception:
            pass
        for i in missing:
            self._avatars.setdefault(i, "")

    def check(self, creds, names):
        out = {}
        for i in range(0, len(names), 50):
            chunk = names[i:i + 50]
            q = "&".join("slug=" + urllib.parse.quote(n) for n in chunk)
            data = self.get(creds, "https://api.kick.com/public/v1/channels?" + q).get("data") or []
            found = {(c.get("slug") or "").lower(): c for c in data}
            self._load_avatars(creds, [c.get("broadcaster_user_id") for c in data if c.get("broadcaster_user_id")])
            for n in chunk:
                c = found.get(n)
                if not c:
                    out[n] = {"state": "notfound"}
                    continue
                base = {"display": c.get("slug", n), "avatar": self._avatars.get(c.get("broadcaster_user_id"), "")}
                s = c.get("stream") or {}
                if s.get("is_live"):
                    out[n] = {**base, "state": "live", "title": c.get("stream_title", ""),
                              "game": (c.get("category") or {}).get("name", ""),
                              "viewers": s.get("viewer_count", 0), "started": s.get("start_time", ""),
                              "thumb": cache_bust(s.get("thumbnail") or c.get("banner_picture") or "")}
                else:
                    out[n] = {**base, "state": "offline"}
        return out


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (400, 401, 403):
            return f"API key problem (HTTP {e.code}) - check Settings"
        return f"Server error (HTTP {e.code})"
    if isinstance(e, urllib.error.URLError):
        return "No internet connection"
    return type(e).__name__


# ---------------------------------------------------------------- Windows helpers

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x1, 0x2, 0x4, 0x20, 0x40
GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000


def find_hwnd(title):
    return user32.FindWindowW(None, title)


def dwm_set(hwnd, attr, value):
    v = ctypes.c_int(value)
    dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))


def window_scale(hwnd):
    try:
        return user32.GetDpiForWindow(hwnd) / 96 or 1.0
    except Exception:
        return 1.0


def work_area():
    r = wt.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r


def system_uses_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def make_icon(live=False, size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    d.rounded_rectangle((s * 0.03, s * 0.1, s * 0.97, s * 0.84), radius=s * 0.19, fill=(124, 77, 255))
    d.polygon([(s * 0.38, s * 0.26), (s * 0.38, s * 0.62), (s * 0.68, s * 0.44)], fill="white")
    if live:
        d.ellipse((s * 0.6, s * 0.6, s * 0.98, s * 0.98), fill=(34, 197, 94), outline="white", width=max(2, s // 21))
    return img


def startup_enabled():
    return os.path.exists(STARTUP_LNK)


def set_startup(enable):
    if not enable:
        if os.path.exists(STARTUP_LNK):
            os.remove(STARTUP_LNK)
        return
    esc = lambda s: s.replace("'", "''")
    if FROZEN:
        target, args = sys.executable, "--hidden"
    else:
        target = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(target):
            target = sys.executable
        args = f'"{esc(os.path.abspath(__file__))}" --hidden'
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{esc(STARTUP_LNK)}');"
          f"$s.TargetPath='{esc(target)}';$s.Arguments='{args}';"
          f"$s.WorkingDirectory='{esc(HERE)}';$s.IconLocation='{esc(ICON_ICO)}';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def parse_streamer(text, default_platform):
    text = (text or "").strip()
    m = re.search(r"(twitch\.tv|kick\.com)/(?:popout/)?([A-Za-z0-9_\-]+)", text, re.I)
    if m:
        return ("twitch" if "twitch" in m.group(1).lower() else "kick"), m.group(2).lower()
    name = text.lstrip("@").lower()
    if re.fullmatch(r"[a-z0-9_\-]{2,30}", name):
        return default_platform, name
    return None


# ---------------------------------------------------------------- JS bridge

class JsApi:
    """Methods callable from the web UI as window.pywebview.api.<name>(...)."""

    def __init__(self, app):
        self._app = app

    def ready(self, which):
        self._app.on_page_ready(which)

    def get_state(self):
        return self._app.state()

    def add_streamer(self, text, platform):
        return self._app.add_streamer(text, platform)

    def remove_streamer(self, platform, name):
        self._app.remove_streamer(platform, name)

    def toggle_notify(self, platform, name):
        self._app.toggle_notify(platform, name)

    def open_stream(self, platform, name):
        webbrowser.open(stream_url(platform, name))
        self._app.hide_flyout()

    def open_dev_console(self, platform):
        if platform in DEV_LINKS:
            webbrowser.open(DEV_LINKS[platform])

    def refresh(self):
        self._app.wake.set()

    def get_settings(self):
        return self._app.get_settings()

    def save_settings(self, settings):
        return self._app.save_settings(settings)

    def set_theme(self, theme):
        self._app.set_theme(theme)

    def set_dark(self, dark):
        self._app.set_titlebar_dark(bool(dark))

    def flyout_size(self, width, height):
        self._app.set_flyout_size(width, height)

    def hide_flyout(self):
        self._app.hide_flyout()

    def open_main(self, page=""):
        self._app.show_main(page)

    def quit(self):
        threading.Thread(target=self._app.quit, daemon=True).start()


# ---------------------------------------------------------------- app

class App:
    def __init__(self, start_hidden):
        os.makedirs(DATA_DIR, exist_ok=True)
        make_icon(size=128).save(ICON_PNG)
        make_icon(size=256).save(ICON_ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        self.cfg = load_config()
        save_config(self.cfg)
        self.lock = threading.RLock()
        self.status = {}
        self.last_check = None
        self.wake = threading.Event()
        self.quitting = False
        self.ready_pages = set()
        self.flyout_css = (340, 420)
        self.flyout_visible = False
        self.flyout_hidden_at = 0.0
        self.tray = None
        self.twitch, self.kick = TwitchApi(), KickApi()

        api = JsApi(self)
        dark = self.effective_dark()
        self.main = webview.create_window(
            APP, url=os.path.join(UI_DIR, "main.html"), js_api=api, width=1180, height=760,
            min_size=(560, 420), hidden=start_hidden and self.has_any_keys(),
            background_color="#0f0f12" if dark else "#f4f4f6")
        self.flyout = webview.create_window(
            FLYOUT_TITLE, url=os.path.join(UI_DIR, "flyout.html"), js_api=api, width=340, height=420,
            frameless=True, easy_drag=False, resizable=False, on_top=True, hidden=True,
            background_color="#1a1a1f" if dark else "#ffffff")
        self.main.events.closing += self.on_main_closing

    # ----- state
    def has_any_keys(self):
        return any(self.cfg[p]["client_id"] and self.cfg[p]["client_secret"] for p in ("twitch", "kick"))

    def effective_dark(self):
        t = self.cfg.get("theme", "system")
        return t == "dark" or (t == "system" and system_uses_dark())

    def state(self):
        with self.lock:
            items = []
            for s in self.cfg["streamers"]:
                st = self.status.get((s["platform"], s["name"]), {"state": "checking"})
                items.append({**st, "platform": s["platform"], "name": s["name"], "notify": s["notify"],
                              "url": stream_url(s["platform"], s["name"])})
            return {"streamers": items, "theme": self.cfg.get("theme", "system"),
                    "last_check": self.last_check, "has_keys": self.has_any_keys()}

    def push_state(self):
        payload = json.dumps(self.state())
        for which, win in (("main", self.main), ("flyout", self.flyout)):
            if which in self.ready_pages:
                threading.Thread(target=self._eval, args=(win, f"window.onState && window.onState({payload})"),
                                 daemon=True).start()
        self.update_tray()

    @staticmethod
    def _eval(win, js):
        try:
            win.evaluate_js(js)
        except Exception:
            pass

    def on_page_ready(self, which):
        self.ready_pages.add(which)
        if which == "main" and not self.has_any_keys():
            threading.Thread(target=self._eval, args=(self.main, "window.openSettings && openSettings(true)"),
                             daemon=True).start()

    # ----- streamer list
    def add_streamer(self, text, platform):
        parsed = parse_streamer(text, platform if platform in PLATFORM_LABEL else "twitch")
        if not parsed:
            return {"ok": False, "error": "Paste a twitch.tv / kick.com link or type a channel name."}
        plat, name = parsed
        with self.lock:
            if any(s["platform"] == plat and s["name"] == name for s in self.cfg["streamers"]):
                return {"ok": False, "error": f"{name} ({PLATFORM_LABEL[plat]}) is already in your list."}
            self.cfg["streamers"].append({"platform": plat, "name": name, "notify": True})
            save_config(self.cfg)
        self.push_state()
        self.wake.set()
        return {"ok": True, "platform": plat, "name": name}

    def remove_streamer(self, platform, name):
        with self.lock:
            self.cfg["streamers"] = [s for s in self.cfg["streamers"]
                                     if not (s["platform"] == platform and s["name"] == name)]
            self.status.pop((platform, name), None)
            save_config(self.cfg)
        self.push_state()

    def toggle_notify(self, platform, name):
        with self.lock:
            for s in self.cfg["streamers"]:
                if s["platform"] == platform and s["name"] == name:
                    s["notify"] = not s["notify"]
            save_config(self.cfg)
        self.push_state()

    # ----- settings
    def get_settings(self):
        with self.lock:
            return {"twitch": dict(self.cfg["twitch"]), "kick": dict(self.cfg["kick"]),
                    "interval": self.cfg["interval"], "theme": self.cfg.get("theme", "system"),
                    "startup": startup_enabled()}

    def save_settings(self, s):
        with self.lock:
            for plat in ("twitch", "kick"):
                for field in ("client_id", "client_secret"):
                    self.cfg[plat][field] = str((s.get(plat) or {}).get(field, "")).strip()
            try:
                self.cfg["interval"] = max(30, min(900, int(s.get("interval", 60))))
            except (TypeError, ValueError):
                self.cfg["interval"] = 60
            if s.get("theme") in ("system", "light", "dark"):
                self.cfg["theme"] = s["theme"]
            save_config(self.cfg)
        error = None
        try:
            if bool(s.get("startup")) != startup_enabled():
                set_startup(bool(s.get("startup")))
        except Exception as e:
            error = f"Could not change the startup setting: {e}"
        self.push_state()
        self.wake.set()
        return {"ok": error is None, "error": error}

    def set_theme(self, theme):
        if theme not in ("system", "light", "dark"):
            return
        with self.lock:
            self.cfg["theme"] = theme
            save_config(self.cfg)
        self.push_state()

    def set_titlebar_dark(self, dark):
        hwnd = find_hwnd(APP)
        if hwnd:
            dwm_set(hwnd, 20, 1 if dark else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)

    # ----- polling
    def poll_loop(self):
        while not self.quitting:
            with self.lock:
                streamers = [dict(s) for s in self.cfg["streamers"]]
                creds = {p: dict(self.cfg[p]) for p in ("twitch", "kick")}
                interval = self.cfg["interval"]
            results = {}
            for plat, api in (("twitch", self.twitch), ("kick", self.kick)):
                names = [s["name"] for s in streamers if s["platform"] == plat]
                if not names:
                    continue
                if not (creds[plat]["client_id"] and creds[plat]["client_secret"]):
                    results.update({(plat, n): {"state": "nokeys"} for n in names})
                    continue
                try:
                    results.update({(plat, n): st for n, st in api.check(creds[plat], names).items()})
                except Exception as e:
                    err = describe_error(e)
                    results.update({(plat, n): {"state": "error", "error": err} for n in names})
            self.apply_results(results)
            self.wake.wait(interval)
            self.wake.clear()

    def apply_results(self, results):
        to_notify = []
        with self.lock:
            notify = {(s["platform"], s["name"]): s["notify"] for s in self.cfg["streamers"]}
            for key, st in results.items():
                if key not in notify:
                    continue  # removed while checking
                prev = self.status.get(key, {})
                # On a temporary error keep the last known state, so it doesn't re-trigger "went live" alerts
                if st["state"] == "error" and prev.get("state") in ("live", "offline"):
                    st = {**prev, "stale": st.get("error")}
                if st["state"] in ("live", "offline") and not st.get("avatar") and prev.get("avatar"):
                    st["avatar"] = prev["avatar"]
                if st["state"] == "live" and prev.get("state") != "live" and notify[key]:
                    to_notify.append((key, st))
                self.status[key] = st
            self.last_check = time.strftime("%H:%M:%S")
        for key, st in to_notify:
            self.notify(key, st)
        self.push_state()

    def notify(self, key, st):
        plat, name = key
        url = stream_url(plat, name)
        title = f"{st.get('display', name)} is live on {PLATFORM_LABEL[plat]}"
        msg = st.get("title", "").strip() or "Stream started"
        if st.get("game"):
            msg += f"\n{st['game']}"

        def show():
            try:
                n = Notification(app_id=APP, title=title[:100], msg=msg[:250], icon=ICON_PNG, launch=url)
                n.set_audio(audio.Default, loop=False)
                n.add_actions(label="Watch", launch=url)
                n.show()
            except Exception:
                pass
        threading.Thread(target=show, daemon=True).start()

    # ----- tray
    def update_tray(self):
        if not self.tray:
            return
        with self.lock:
            live = sum(1 for s in self.status.values() if s.get("state") == "live")
        try:
            self.tray.icon = make_icon(live=live > 0)
            self.tray.title = f"{APP} - {live} live" if live else f"{APP} - nobody live"
        except Exception:
            pass

    def run_tray(self):
        self.tray = pystray.Icon(APP, make_icon(), APP, menu=pystray.Menu(
            pystray.MenuItem("Quick view", lambda: self.toggle_flyout(), default=True, visible=False),
            pystray.MenuItem("Open Stream Notifier", lambda: self.show_main()),
            pystray.MenuItem("Refresh now", lambda: self.wake.set()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.quit()),
        ))
        self.update_tray()
        self.tray.run()

    # ----- flyout (tray quick view)
    def set_flyout_size(self, width, height):
        self.flyout_css = (max(260, int(width)), max(120, min(int(height), 900)))
        if self.flyout_visible:
            self.position_flyout()

    def position_flyout(self):
        hwnd = find_hwnd(FLYOUT_TITLE)
        if not hwnd:
            return None
        scale = window_scale(hwnd)
        w, h = int(self.flyout_css[0] * scale), int(self.flyout_css[1] * scale)
        wa = work_area()
        m = int(12 * scale)
        x = wa.left + m if wa.left > 0 else wa.right - w - m       # taskbar on the left?
        y = wa.top + m if wa.top > 0 else wa.bottom - h - m        # taskbar at the top?
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_SHOWWINDOW if self.flyout_visible else 0)
        return hwnd

    def prepare_flyout(self):
        hwnd = find_hwnd(FLYOUT_TITLE)
        if hwnd:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
            dwm_set(hwnd, 33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE = round
        return hwnd

    def toggle_flyout(self):
        if self.flyout_visible:
            self.hide_flyout()
        elif time.time() - self.flyout_hidden_at > 0.4:  # click on tray icon that just closed it
            self.show_flyout()

    def show_flyout(self):
        self.prepare_flyout()
        self.flyout_visible = True
        self.position_flyout()
        self.flyout.show()
        hwnd = self.position_flyout()
        if hwnd:
            user32.SetForegroundWindow(hwnd)
        self.push_state()
        threading.Thread(target=self._watch_flyout_focus, daemon=True).start()

    def _watch_flyout_focus(self):
        """Close the quick view when the user clicks anywhere else (like Windows flyouts)."""
        time.sleep(0.35)
        hwnd = find_hwnd(FLYOUT_TITLE)
        while self.flyout_visible and not self.quitting:
            if user32.GetForegroundWindow() != hwnd:
                self.hide_flyout()
                return
            time.sleep(0.1)

    def hide_flyout(self):
        if not self.flyout_visible:
            return
        self.flyout_visible = False
        self.flyout_hidden_at = time.time()
        try:
            self.flyout.hide()
        except Exception:
            pass

    # ----- main window
    def show_main(self, page=""):
        self.hide_flyout()
        self.main.show()
        hwnd = find_hwnd(APP)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        if page == "settings":
            threading.Thread(target=self._eval, args=(self.main, "window.openSettings && openSettings()"),
                             daemon=True).start()
        elif page == "add":
            threading.Thread(target=self._eval, args=(self.main, "window.openAdd && openAdd()"),
                             daemon=True).start()

    def on_main_closing(self):
        if self.quitting:
            return True
        threading.Thread(target=self.main.hide, daemon=True).start()
        return False  # keep running in the tray

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.wake.set()
        try:
            self.tray.stop()
        except Exception:
            pass
        for w in (self.flyout, self.main):
            try:
                w.destroy()
            except Exception:
                pass

    def on_start(self):
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.run_tray, daemon=True).start()
        self.flyout.events.loaded += lambda: self.prepare_flyout()

    def run(self):
        webview.start(self.on_start, gui="edgechromium", icon=ICON_ICO, private_mode=False,
                      storage_path=os.path.join(DATA_DIR, "webview"))


def already_running():
    """Single-instance guard: if the app is already running, bring its window up instead."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\StreamNotifierSingleInstance")
    if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return False
    hwnd = find_hwnd(APP)
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
    return True


if __name__ == "__main__":
    if not already_running():
        App(start_hidden="--hidden" in sys.argv).run()
