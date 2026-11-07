# Stream Notifier

A small Windows tray app that notifies you when your favorite **Twitch** and **Kick** streamers go live.

- Card view of live streamers with preview image, title, game, viewers and uptime
- Windows notification when someone goes live, with a **Watch** button
- Turn notifications on or off per streamer
- Quick view: left-click the tray icon to see who is live at a glance
- Light, dark, or follow Windows theme
- Optional start with Windows (runs quietly in the tray)

## Download

Get `StreamNotifier.exe` from the [Releases](../../releases) page and run it. No installation or Python needed.

Requirements: Windows 10 or 11 with the Microsoft Edge WebView2 Runtime (already included in Windows 11 and most up-to-date Windows 10 PCs).

> The exe is not code-signed, so Windows SmartScreen may show "Windows protected your PC" the first time. Click **More info → Run anyway**.

## Setup: API keys

The app uses the official Twitch and Kick APIs. Each user needs their own free keys, entered in **Settings**:

**Twitch**
1. Go to https://dev.twitch.tv/console/apps and click **Register Your Application**.
2. Name: anything. OAuth Redirect URL: `http://localhost`. Category: *Application Integration*.
3. Copy the **Client ID**, click **New Secret** and copy the **Client Secret**.

**Kick**
1. Go to https://kick.com/settings/developer and click **Create App**.
2. Name: letters and numbers only. Redirect URL: `http://localhost`. No extra scopes needed.
3. Copy the **Client ID** and **Client Secret**.

Your keys and streamer list are stored locally in `%APPDATA%\StreamNotifier\config.json`.

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

[pywebview](https://pywebview.flowrl.com/) · [pystray](https://github.com/moses-palmer/pystray) · [winotify](https://github.com/versa-syahptr/winotify) · [Pillow](https://python-pillow.org/)

Developed with help from [Claude](https://claude.com/claude-code) by Anthropic.
