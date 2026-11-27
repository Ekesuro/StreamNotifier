"""Stream Notifier - tray app that tells you when Twitch / Kick streamers go live.

UI: pywebview (Edge WebView2) pages in ./ui, tray icon via pystray, toasts via winotify.
Twitch: the user signs in with their Twitch account (no API keys needed); EventSub WebSockets give instant
"went live" alerts, Helix polling supplies viewers/previews. The user's own Twitch app keys still work as a fallback.
Kick: official public API with the user's own app keys (entered in Settings).
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
import websocket
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
TWITCH_ID = "https://id.twitch.tv/oauth2"
# Client ID of the app's own Twitch application, registered with Client Type "Public".
# A Client ID is not a secret (public clients have none), so it is fine to ship it in the exe.
TWITCH_PUBLIC_CLIENT_ID = "3o48mwlklz6v0hxd9jqeeoluhp95oe"

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
        "twitch_login": {},
        "kick": {"client_id": "", "client_secret": ""},
        "interval": 60,
        "theme": "system",
        "flyout": {"pos": None, "pinned": False},  # quick view: saved top-left [x, y] in screen pixels
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

def http_json(url, form=None, headers=None, body=None, method=None):
    headers = {"User-Agent": UA, **(headers or {})}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


def http_error_message(e):
    try:
        return json.loads(e.read().decode("utf-8")).get("message", "")
    except Exception:
        return ""


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


class TwitchAuth:
    """Twitch credentials. Preferred: the user signs in with their Twitch account (Device Code Flow on a
    public client, so no secret ships with the app). Fallback: the user's own app keys (client credentials)."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self._app_token = None
        self._app_creds = None
        self.pending = None  # sign-in in progress: {"user_code", "uri", "device_code", ...}
        self.error = ""

    @property
    def login(self):
        return self.app.cfg.get("twitch_login") or {}

    def mode(self):
        if self.login.get("access_token"):
            return "user"
        c = self.app.cfg["twitch"]
        if c["client_id"].strip() and c["client_secret"].strip():
            return "app"
        return None

    def client_id(self):
        return self.login["client_id"] if self.mode() == "user" else self.app.cfg["twitch"]["client_id"].strip()

    def token(self):
        with self.lock:
            mode = self.mode()
            if mode == "user":
                return self.login["access_token"]
            if mode == "app":
                c = self.app.cfg["twitch"]
                creds = (c["client_id"].strip(), c["client_secret"].strip())
                if creds != self._app_creds or not self._app_token:
                    d = http_json(f"{TWITCH_ID}/token", form={"client_id": creds[0], "client_secret": creds[1],
                                                              "grant_type": "client_credentials"})
                    self._app_creds, self._app_token = creds, d["access_token"]
                return self._app_token
            raise RuntimeError("Twitch is not set up")

    def _invalidate(self, failed_token):
        """Called after a 401: refresh the token unless another thread already did."""
        with self.lock:
            if self.mode() == "user":
                if self.login.get("access_token") == failed_token:
                    self._refresh_user()
            elif self._app_token == failed_token:
                self._app_token = None

    def _refresh_user(self):
        login = self.login
        try:
            d = http_json(f"{TWITCH_ID}/token", form={"client_id": login["client_id"], "grant_type": "refresh_token",
                                                      "refresh_token": login.get("refresh_token", "")})
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):  # refresh token expired or revoked -> user has to sign in again
                self.app.twitch_signed_out("Your Twitch sign-in expired. Please sign in again.")
            raise
        with self.app.lock:
            self.app.cfg["twitch_login"] = {**login, "access_token": d["access_token"],
                                            "refresh_token": d.get("refresh_token", login.get("refresh_token"))}
            save_config(self.app.cfg)

    def request(self, url, method=None, body=None):
        for attempt in range(2):
            tok = self.token()
            try:
                return http_json(url, method=method, body=body,
                                 headers={"Authorization": f"Bearer {tok}", "Client-Id": self.client_id(),
                                          "Accept": "application/json"})
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._invalidate(tok)
                    continue
                raise

    # ----- sign-in (Device Code Flow)
    def start_signin(self):
        if not TWITCH_PUBLIC_CLIENT_ID:
            return {"ok": False, "error": "Twitch sign-in is not available in this build. Use your own API keys below."}
        try:
            d = http_json(f"{TWITCH_ID}/device", form={"client_id": TWITCH_PUBLIC_CLIENT_ID, "scopes": ""})
        except Exception as e:
            return {"ok": False, "error": f"Could not reach Twitch ({describe_error(e)})."}
        with self.lock:
            self.error = ""
            self.pending = {"user_code": d["user_code"], "uri": d["verification_uri"], "device_code": d["device_code"],
                            "interval": max(1, int(d.get("interval", 5))),
                            "expires": time.time() + int(d.get("expires_in", 1800))}
            pending = self.pending
        webbrowser.open(pending["uri"])
        threading.Thread(target=self._wait_for_signin, args=(pending,), daemon=True).start()
        return {"ok": True}

    def cancel_signin(self):
        with self.lock:
            self.pending = None

    def _wait_for_signin(self, p):
        form = {"client_id": TWITCH_PUBLIC_CLIENT_ID, "scopes": "", "device_code": p["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        interval = p["interval"]
        while self.pending is p and not self.app.quitting:
            time.sleep(interval)
            if self.pending is not p:
                return
            if time.time() > p["expires"]:
                return self._signin_failed(p, "The sign-in code expired. Please try again.")
            try:
                tok = http_json(f"{TWITCH_ID}/token", form=form)
            except urllib.error.HTTPError as e:
                msg = http_error_message(e)
                if msg == "authorization_pending":
                    continue
                if msg == "slow_down":
                    interval += 5
                    continue
                return self._signin_failed(p, "Sign-in was cancelled or denied on Twitch." if e.code in (400, 403)
                                           else f"Twitch sign-in failed (HTTP {e.code}).")
            except Exception:
                continue  # network hiccup: keep trying until the code expires
            try:
                info = http_json(f"{TWITCH_ID}/validate", headers={"Authorization": f"OAuth {tok['access_token']}"})
            except Exception as e:
                return self._signin_failed(p, f"Could not read your Twitch account ({describe_error(e)}).")
            with self.lock:
                if self.pending is not p:
                    return
                self.pending = None
            self.app.twitch_signed_in({"client_id": TWITCH_PUBLIC_CLIENT_ID, "access_token": tok["access_token"],
                                       "refresh_token": tok.get("refresh_token", ""), "login": info.get("login", ""),
                                       "user_id": info.get("user_id", "")})
            return

    def _signin_failed(self, p, msg):
        with self.lock:
            if self.pending is p:
                self.pending, self.error = None, msg
        self.app.push_state()

    def revoke(self, login):
        try:
            http_json(f"{TWITCH_ID}/revoke", form={"client_id": login["client_id"], "token": login["access_token"]})
        except Exception:
            pass

    def info(self):
        with self.lock:
            p = self.pending
            return {"mode": self.mode(), "login": self.login.get("login", ""), "available": bool(TWITCH_PUBLIC_CLIENT_ID),
                    "pending": {"user_code": p["user_code"], "uri": p["uri"]} if p else None, "error": self.error}


class TwitchEventSub:
    """Instant "went live" alerts over Twitch EventSub WebSockets (needs a signed-in user).
    Polling keeps running alongside it for viewer counts, previews and offline detection."""
    URL = "wss://eventsub.wss.twitch.tv/ws"
    SUBS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"

    def __init__(self, app):
        self.app = app
        self.wake = threading.Event()
        self.ws = None
        self.session_id = None
        self.subs = {}       # broadcaster user_id -> subscription id
        self.ids = {}        # login -> user_id
        self.limit_hit = False
        self.connected = False
        self.sync_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def restart(self):
        """Drop the connection (e.g. after sign-in / sign-out); the loop reconnects if still signed in."""
        ws = self.ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self.wake.set()

    def _run(self):
        backoff = 5
        while not self.app.quitting:
            if self.app.twitch_auth.mode() != "user":
                self.wake.wait(3600)
                self.wake.clear()
                continue
            started = time.time()
            try:
                self._session()
            except Exception:
                pass
            self.connected, self.session_id, self.subs, self.limit_hit = False, None, {}, False
            self.app.push_state()
            if self.app.quitting:
                return
            backoff = 5 if time.time() - started > 120 else min(backoff * 2, 300)
            self.wake.wait(backoff)
            self.wake.clear()

    def _session(self):
        ws =websocket.create_connection(self.URL, timeout=15)
        self.ws = ws
        keepalive = 10
        try:
            while not self.app.quitting and self.app.twitch_auth.mode() == "user":
                ws.settimeout(keepalive + 10)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    return  # no keepalive from Twitch -> reconnect
                if not raw:
                    return
                msg = json.loads(raw)
                kind = msg.get("metadata", {}).get("message_type")
                payload = msg.get("payload", {})
                if kind == "session_welcome":
                    sess = payload["session"]
                    keepalive = int(sess.get("keepalive_timeout_seconds") or 10)
                    if self.session_id is None:  # fresh connection (not a reconnect hand-over)
                        self.session_id = sess["id"]
                        self.connected = True
                        threading.Thread(target=self.sync, daemon=True).start()
                elif kind == "session_reconnect":
                    # Twitch moves us to another server; subscriptions carry over to the new connection
                    new = websocket.create_connection(payload["session"]["reconnect_url"], timeout=15)
                    old, ws, self.ws = ws, new, new
                    try:
                        old.close()
                    except Exception:
                        pass
                elif kind == "notification" and payload.get("subscription", {}).get("type") == "stream.online":
                    ev = payload.get("event", {})
                    threading.Thread(target=self.app.on_twitch_online, daemon=True,
                                     args=(ev.get("broadcaster_user_login", ""), ev.get("broadcaster_user_id", ""),
                                           ev.get("started_at", ""))).start()
                elif kind == "revocation":
                    sub = payload.get("subscription", {})
                    uid = sub.get("condition", {}).get("broadcaster_user_id")
                    if uid and self.subs.get(uid) == sub.get("id"):
                        self.subs.pop(uid, None)
        finally:
            try:
                ws.close()
            except Exception:
                pass
            self.ws = None

    def sync(self):
        """Make the subscriptions match the Twitch streamers that have notifications on."""
        with self.sync_lock:
            sid = self.session_id
            if not sid or self.app.twitch_auth.mode() != "user":
                return
            auth = self.app.twitch_auth
            with self.app.lock:
                wanted = [s["name"] for s in self.app.cfg["streamers"] if s["platform"] == "twitch" and s["notify"]]
            try:
                missing = [n for n in wanted if n not in self.ids]
                for i in range(0, len(missing), 100):
                    q = "&".join("login=" + urllib.parse.quote(n) for n in missing[i:i + 100])
                    for u in auth.request("https://api.twitch.tv/helix/users?" + q)["data"]:
                        self.ids[u["login"].lower()] = u["id"]
            except Exception:
                pass
            wanted_ids = {self.ids[n] for n in wanted if n in self.ids}
            for uid in [u for u in self.subs if u not in wanted_ids]:
                try:
                    auth.request(f"{self.SUBS_URL}?id={self.subs[uid]}", method="DELETE")
                except Exception:
                    pass
                self.subs.pop(uid, None)
                self.limit_hit = False
            for uid in wanted_ids - set(self.subs):
                if self.session_id != sid:
                    return
                try:
                    r = auth.request(self.SUBS_URL, method="POST", body={
                        "type": "stream.online", "version": "1", "condition": {"broadcaster_user_id": uid},
                        "transport": {"method": "websocket", "session_id": sid}})
                    self.subs[uid] = r["data"][0]["id"]
                except urllib.error.HTTPError as e:
                    if e.code in (429, 400, 403):  # per-user subscription limit reached: polling covers the rest
                        self.limit_hit = True
                        break
                except Exception:
                    pass
        self.app.push_state()

    def info(self):
        with self.app.lock:
            wanted = sum(1 for s in self.app.cfg["streamers"] if s["platform"] == "twitch" and s["notify"])
        return {"connected": self.connected, "instant": len(self.subs), "wanted": wanted, "limit": self.limit_hit}


class TwitchApi:
    def __init__(self, auth):
        self.auth = auth

    def channel_info(self, user_id):
        d = self.auth.request("https://api.twitch.tv/helix/channels?broadcaster_id=" + urllib.parse.quote(user_id))
        return (d.get("data") or [{}])[0]

    def check(self, names):
        out = {}
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            q = "&".join("login=" + urllib.parse.quote(n) for n in chunk)
            users = {u["login"].lower(): u for u in self.auth.request("https://api.twitch.tv/helix/users?" + q)["data"]}
            q = "&".join("user_login=" + urllib.parse.quote(n) for n in chunk)
            live = {s["user_login"].lower(): s
                    for s in self.auth.request("https://api.twitch.tv/helix/streams?first=100&" + q)["data"]}
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

# Own handle (not ctypes.windll.user32) so the argtypes below don't affect pywebview's calls.
# Without argtypes, HWND_TOPMOST (-1) is passed as a 32-bit int and SetWindowPos silently fails.
user32 = ctypes.WinDLL("user32")
dwmapi = ctypes.windll.dwmapi
_HWND = wt.HWND
for _name, _args, _res in (
        ("FindWindowW", [wt.LPCWSTR, wt.LPCWSTR], _HWND),
        ("GetForegroundWindow", [], _HWND),
        ("SetForegroundWindow", [_HWND], wt.BOOL),
        ("ShowWindow", [_HWND, ctypes.c_int], wt.BOOL),
        ("SetWindowPos", [_HWND, _HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT], wt.BOOL),
        ("GetWindowLongW", [_HWND, ctypes.c_int], ctypes.c_long),
        ("SetWindowLongW", [_HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long),
        ("GetWindowRect", [_HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        ("GetDpiForWindow", [_HWND], wt.UINT),
        ("GetAsyncKeyState", [ctypes.c_int], ctypes.c_short)):
    _f = getattr(user32, _name)
    _f.argtypes, _f.restype = _args, _res
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x1, 0x2, 0x4, 0x10, 0x20, 0x40
GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
VK_LBUTTON = 0x01
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_work_area(x, y):
    """Work area (screen minus taskbar) of the monitor nearest to the point."""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2), ctypes.byref(mi))  # NEAREST
    return mi.rcWork


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


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
        self._app.dismiss_flyout()

    def open_dev_console(self, platform):
        if platform in DEV_LINKS:
            webbrowser.open(DEV_LINKS[platform])

    def refresh(self):
        self._app.wake.set()

    def twitch_signin(self):
        r = self._app.twitch_auth.start_signin()
        self._app.push_state()
        return r

    def twitch_cancel_signin(self):
        self._app.twitch_auth.cancel_signin()
        self._app.push_state()

    def twitch_open_activate(self):
        p = self._app.twitch_auth.pending
        webbrowser.open(p["uri"] if p else "https://www.twitch.tv/activate")

    def twitch_signout(self):
        self._app.twitch_signed_out()

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

    def flyout_pin(self, pinned):
        self._app.set_flyout_pinned(bool(pinned))

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
        self.twitch_auth = TwitchAuth(self)
        self.twitch, self.kick = TwitchApi(self.twitch_auth), KickApi()
        self.eventsub = TwitchEventSub(self)

        api = JsApi(self)
        dark = self.effective_dark()
        self.main = webview.create_window(
            APP, url=os.path.join(UI_DIR, "main.html"), js_api=api, width=1180, height=760,
            min_size=(560, 420), hidden=start_hidden and self.has_any_keys(),
            background_color="#0f0f12" if dark else "#f4f4f6")
        # x/y given so Windows doesn't re-center it on first show; position_flyout() places it properly
        self.flyout = webview.create_window(
            FLYOUT_TITLE, url=os.path.join(UI_DIR, "flyout.html"), js_api=api, width=340, height=420, x=0, y=0,
            frameless=True, easy_drag=False, resizable=False, on_top=False, hidden=True,
            background_color="#1a1a1f" if dark else "#ffffff")
        self.flyout_xy = None  # where position_flyout() last put it (to notice user drags)
        self.main.events.closing += self.on_main_closing

    # ----- state
    def kick_ready(self):
        return bool(self.cfg["kick"]["client_id"] and self.cfg["kick"]["client_secret"])

    def has_any_keys(self):
        return bool(self.twitch_auth.mode()) or self.kick_ready()

    # ----- Twitch sign-in
    def twitch_signed_in(self, login):
        with self.lock:
            self.cfg["twitch_login"] = login
            save_config(self.cfg)
        self.eventsub.restart()
        self.push_state()
        self.wake.set()

    def twitch_signed_out(self, error=""):
        with self.lock:
            login = self.cfg.get("twitch_login") or {}
            self.cfg["twitch_login"] = {}
            save_config(self.cfg)
        self.twitch_auth.error = error
        if login.get("access_token") and not error:
            threading.Thread(target=self.twitch_auth.revoke, args=(login,), daemon=True).start()
        self.eventsub.restart()
        self.push_state()
        self.wake.set()

    def on_twitch_online(self, login, user_id, started):
        """EventSub says a stream just started: alert right away instead of waiting for the next poll."""
        key = ("twitch", (login or "").lower())
        with self.lock:
            entry = next((s for s in self.cfg["streamers"] if (s["platform"], s["name"]) == key), None)
            prev = self.status.get(key, {})
        if not entry or prev.get("state") == "live":
            return
        try:
            ch = self.twitch.channel_info(user_id)
        except Exception:
            ch = {}
        st = {"display": prev.get("display") or ch.get("broadcaster_name") or login, "avatar": prev.get("avatar", ""),
              "state": "live", "title": ch.get("title", ""), "game": ch.get("game_name", ""), "viewers": 0,
              "started": started, "thumb": "", "pending_until": time.time() + 300}
        with self.lock:
            if self.status.get(key, {}).get("state") == "live":
                return
            self.status[key] = st
            notify = entry["notify"]
        if notify:
            self.notify(key, st)
        self.push_state()
        t = threading.Timer(90, self.wake.set)  # fetch viewers + preview once Twitch lists the stream
        t.daemon = True
        t.start()

    def effective_dark(self):
        t = self.cfg.get("theme", "system")
        return t == "dark" or (t == "system" and system_uses_dark())

    def twitch_info(self):
        # Must not be called while holding self.lock (TwitchAuth takes its own lock first, then self.lock)
        return {**self.twitch_auth.info(), "eventsub": self.eventsub.info()}

    def state(self):
        twitch = self.twitch_info()
        with self.lock:
            items = []
            for s in self.cfg["streamers"]:
                st = self.status.get((s["platform"], s["name"]), {"state": "checking"})
                items.append({**st, "platform": s["platform"], "name": s["name"], "notify": s["notify"],
                              "url": stream_url(s["platform"], s["name"])})
            return {"streamers": items, "theme": self.cfg.get("theme", "system"),
                    "last_check": self.last_check, "has_keys": self.has_any_keys(),
                    "flyout_pinned": bool(self.cfg["flyout"].get("pinned")),
                    "twitch_auth": twitch}

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
        self.sync_eventsub(plat)
        return {"ok": True, "platform": plat, "name": name}

    def remove_streamer(self, platform, name):
        with self.lock:
            self.cfg["streamers"] = [s for s in self.cfg["streamers"]
                                     if not (s["platform"] == platform and s["name"] == name)]
            self.status.pop((platform, name), None)
            save_config(self.cfg)
        self.push_state()
        self.sync_eventsub(platform)

    def toggle_notify(self, platform, name):
        with self.lock:
            for s in self.cfg["streamers"]:
                if s["platform"] == platform and s["name"] == name:
                    s["notify"] = not s["notify"]
            save_config(self.cfg)
        self.push_state()
        self.sync_eventsub(platform)

    def sync_eventsub(self, platform):
        if platform == "twitch":
            threading.Thread(target=self.eventsub.sync, daemon=True).start()

    # ----- settings
    def get_settings(self):
        twitch = self.twitch_info()
        with self.lock:
            return {"twitch": dict(self.cfg["twitch"]), "kick": dict(self.cfg["kick"]), "twitch_auth": twitch,
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
            checks = (("twitch", self.twitch_auth.mode(), lambda names: self.twitch.check(names)),
                      ("kick", self.kick_ready(), lambda names: self.kick.check(creds["kick"], names)))
            for plat, ready, check in checks:
                names = [s["name"] for s in streamers if s["platform"] == plat]
                if not names:
                    continue
                if not ready:
                    results.update({(plat, n): {"state": "nokeys"} for n in names})
                    continue
                try:
                    results.update({(plat, n): st for n, st in check(names).items()})
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
                # EventSub reported "live" but Helix doesn't list the stream yet: trust EventSub for a few minutes
                if st["state"] == "offline" and prev.get("pending_until", 0) > time.time():
                    st = prev
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
            pystray.MenuItem("Reset quick view position", lambda: self.reset_flyout_position()),
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
        pos = self.cfg["flyout"].get("pos")
        if pos:  # where the user dragged it, kept fully on that monitor's screen
            wa = monitor_work_area(pos[0] + w // 2, pos[1] + 20)
            x = max(wa.left, min(int(pos[0]), wa.right - w))
            y = max(wa.top, min(int(pos[1]), wa.bottom - h))
        else:    # default: next to the tray, on the main screen
            wa = work_area()
            m = int(12 * scale)
            x = wa.left + m if wa.left > 0 else wa.right - w - m   # taskbar on the left?
            y = wa.top + m if wa.top > 0 else wa.bottom - h - m    # taskbar at the top?
        after = HWND_TOPMOST if self.cfg["flyout"].get("pinned") else HWND_NOTOPMOST
        user32.SetWindowPos(hwnd, after, x, y, w, h, SWP_SHOWWINDOW if self.flyout_visible else SWP_NOACTIVATE)
        self.flyout_xy = (x, y)
        return hwnd

    def remember_flyout_pos(self, hwnd):
        """If the user dragged the quick view somewhere else, save that spot."""
        if not hwnd or self.flyout_xy is None or user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000:
            return  # still dragging
        r = window_rect(hwnd)
        if (r.left, r.top) != self.flyout_xy and r.right > r.left:
            self.flyout_xy = (r.left, r.top)
            with self.lock:
                self.cfg["flyout"]["pos"] = [r.left, r.top]
                save_config(self.cfg)

    def reset_flyout_position(self):
        with self.lock:
            self.cfg["flyout"]["pos"] = None
            save_config(self.cfg)
        if self.flyout_visible:
            self.position_flyout()
        else:
            self.show_flyout()  # show where it is now, so the reset is visible

    def set_flyout_pinned(self, pinned):
        with self.lock:
            self.cfg["flyout"]["pinned"] = pinned
            save_config(self.cfg)
        hwnd = find_hwnd(FLYOUT_TITLE)
        if hwnd:
            user32.SetWindowPos(hwnd, HWND_TOPMOST if pinned else HWND_NOTOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self.push_state()

    def dismiss_flyout(self):
        """Close the quick view after an action, unless it's pinned."""
        if not self.cfg["flyout"].get("pinned"):
            self.hide_flyout()

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
        """Remember drags, and close the quick view when the user clicks anywhere else (unless pinned)."""
        time.sleep(0.35)
        hwnd = find_hwnd(FLYOUT_TITLE)
        while self.flyout_visible and not self.quitting:
            self.remember_flyout_pos(hwnd)
            if not self.cfg["flyout"].get("pinned") and user32.GetForegroundWindow() != hwnd:
                self.hide_flyout()
                return
            time.sleep(0.1)

    def hide_flyout(self):
        if not self.flyout_visible:
            return
        self.remember_flyout_pos(find_hwnd(FLYOUT_TITLE))
        self.flyout_visible = False
        self.flyout_hidden_at = time.time()
        try:
            self.flyout.hide()
        except Exception:
            pass

    # ----- main window
    def show_main(self, page=""):
        self.dismiss_flyout()
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
        self.eventsub.start()
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
