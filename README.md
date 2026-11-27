# Stream Notifier

A small Windows tray app that notifies you when your favorite **Twitch** and **Kick** streamers go live.

- Card view of live streamers with preview image, title, game, viewers and uptime
- Windows notification when someone goes live, with a **Watch** button (instant for Twitch)
- Turn notifications on or off per streamer
- Quick view: left-click the tray icon to see who is live at a glance
- Light, dark, or follow Windows theme
- Optional start with Windows (runs quietly in the tray)

## Download

Get `StreamNotifier.exe` from the [Releases](../../releases) page and run it. No installation or Python needed.

Requirements: Windows 10 or 11 with the Microsoft Edge WebView2 Runtime (already included in Windows 11 and most up-to-date Windows 10 PCs).

> The exe is not code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time. Click **More info → Run anyway**.

## Setup

**Twitch:** open **Settings** and click **Sign in with Twitch**. Your browser opens twitch.tv/activate; check that the code matches the one in the app and click **Authorize**. That's it, no API keys needed. The app only reads who is live; it never posts or changes anything on your account. Being signed in also enables instant go-live alerts (Twitch EventSub).

<details>
<summary>Prefer your own Twitch API keys instead?</summary>

Open **Advanced** under Twitch in Settings, then:
1. Go to https://dev.twitch.tv/console/apps and click **Register Your Application**.
2. Name: anything. OAuth Redirect URL: `http://localhost`. Category: *Application Integration*.
3. Copy the **Client ID**, click **New Secret** and copy the **Client Secret**.

With keys, alerts come from regular checks (every minute by default) instead of instantly.
</details>

**Kick** (only needed if you follow Kick streamers). Kick needs your own free app keys, entered in **Settings**:
1. Go to https://kick.com/settings/developer and click **Create App**.
2. Name: letters and numbers only. Redirect URL: `http://localhost`. No extra scopes needed.
3. Copy the **Client ID** and **Client Secret**.

Your sign-in, keys and streamer list are stored locally in `%APPDATA%\StreamNotifier\config.json`.

## Running from source

```bash
pip install -r requirements.txt
python stream_notifier.py
```

## Building the exe

```bash
pip install -r requirements.txt pyinstaller
python build.py
```

The result is `dist/StreamNotifier.exe`.

## Built with

[pywebview](https://pywebview.flowrl.com/) · [pystray](https://github.com/moses-palmer/pystray) · [winotify](https://github.com/versa-syahptr/winotify) · [websocket-client](https://github.com/websocket-client/websocket-client) · [Pillow](https://python-pillow.org/)

Developed with help from [Claude](https://claude.com/claude-code) by Anthropic.
