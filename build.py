"""Build StreamNotifier.exe with PyInstaller.

    pip install -r requirements.txt pyinstaller
    python build.py

Output: dist/StreamNotifier.exe (single file, no Python needed on the target PC).
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# Export the app icon so it can be embedded in the .exe
from PIL import Image, ImageDraw  # noqa: E402


def make_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    d.rounded_rectangle((s * 0.03, s * 0.1, s * 0.97, s * 0.84), radius=s * 0.19, fill=(124, 77, 255))
    d.polygon([(s * 0.38, s * 0.26), (s * 0.38, s * 0.62), (s * 0.68, s * 0.44)], fill="white")
    return img


os.makedirs("assets", exist_ok=True)
make_icon(256).save("assets/icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])

subprocess.run([
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile", "--windowed",
    "--name", "StreamNotifier",
    "--icon", "assets/icon.ico",
    "--add-data", f"ui{os.pathsep}ui",
    "--collect-all", "webview",
    "stream_notifier.py",
], check=True)

print("\nBuilt:", os.path.join(HERE, "dist", "StreamNotifier.exe"))
