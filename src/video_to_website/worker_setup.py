"""Dependency guidance and local checks shared by the download page and helper."""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import sys
from pathlib import Path

from .util import run

SETUP_GUIDES = {
    "windows": {
        "label": "Windows",
        "command": None,
        "instructions": "Download and extract FFmpeg and whisper.cpp for Windows. For an RTX 3090, choose the CUDA/cublas x64 Whisper build. Keep its DLLs alongside whisper-cli.exe.",
        "links": [
            {"label": "FFmpeg downloads", "url": "https://ffmpeg.org/download.html#build-windows"},
            {"label": "Whisper downloads", "url": "https://github.com/ggml-org/whisper.cpp/releases"},
        ],
    },
    "macos": {
        "label": "macOS",
        "command": "brew install ffmpeg whisper.cpp",
        "instructions": "If the tools are missing and you use Homebrew, run the command below. On an M-series Mac, use the native Apple Silicon build for Metal acceleration.",
        "links": [
            {"label": "Homebrew", "url": "https://brew.sh/"},
            {"label": "Whisper package", "url": "https://formulae.brew.sh/formula/whisper.cpp"},
        ],
    },
    "linux": {
        "label": "Linux",
        "command": None,
        "instructions": "Install FFmpeg and whisper.cpp using your distribution's packages or the upstream instructions. NVIDIA transcription needs a CUDA-enabled Whisper build.",
        "links": [
            {"label": "FFmpeg downloads", "url": "https://ffmpeg.org/download.html#build-linux"},
            {"label": "Whisper setup", "url": "https://github.com/ggml-org/whisper.cpp#quick-start"},
        ],
    },
}


@contextlib.contextmanager
def tool_paths(paths):
    """Locate portable native tools without changing the user's system PATH."""
    original = os.environ.get("PATH")
    directories = [Path(path).expanduser().resolve() for path in paths]
    for path in directories:
        if not path.is_dir():
            raise ValueError(f"Tool directory does not exist: {path}")
    if Path(sys.argv[0]).suffix == ".pyz":
        portable = Path(sys.argv[0]).resolve().parent / "tools"
        # Explicitly scoped to an adjacent tools folder, never all Downloads.
        directories += [path for path in (portable, portable / "bin") if path.is_dir()]
        # Common release ZIP layouts retain their own bin/Release directories
        # and DLLs. Users can extract both downloads here without flattening them.
        for pattern in ("*", "*/bin", "*/Release", "*/build/bin"):
            for path in sorted(portable.glob(pattern)):
                if path.is_dir() and any((path / name).is_file() for name in ("ffmpeg", "ffmpeg.exe", "whisper-cli", "whisper-cli.exe")) and path not in directories:
                    directories.append(path)
    try:
        if directories:
            os.environ["PATH"] = os.pathsep.join([str(path) for path in directories] + ([original] if original else []))
        yield
    finally:
        if original is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original


def check_tools():
    """Read-only preflight: no pairing, downloads, or state directory creation."""
    print(f"Python {platform.python_version()} — helper ready")
    available = []
    for label, name, argument in (("FFmpeg", "ffmpeg", "-version"),
                                  ("Whisper", os.environ.get("V2W_WHISPER_BIN", "whisper-cli"), "--help")):
        path = shutil.which(name)
        if not path:
            print(f"{label}: missing")
            continue
        try:
            result = run([path, argument], check=False, timeout=10)
            if result.returncode != 0:
                print(f"{label}: found at {path}, but could not start (exit {result.returncode}). Check its runtime libraries.")
                continue
        except (OSError, RuntimeError) as exc:
            print(f"{label}: could not start: {exc}")
            continue
        print(f"{label}: ready ({path})")
        available.append(label)
    if len(available) < 2:
        key = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
        guide = SETUP_GUIDES[key]
        print("\n" + guide["instructions"])
        if guide["command"]:
            print("  " + guide["command"])
        for link in guide["links"]:
            print(f"  {link['label']}: {link['url']}")
        print("Native executables can also go in a tools folder beside the .pyz file, or use --tools DIRECTORY (repeat for separate folders).")
    if available:
        tasks = " and ".join("transcription" if tool == "Whisper" else "visual processing" for tool in available)
        print(f"\nReady to connect for {tasks}. Model weights arrive from the server when needed.")
        print("This check verifies the tools can start; it does not benchmark or verify GPU inference.")
        return 0
    print("\nInstall at least one native tool, then run this check again. No Git checkout or pip install is needed for the helper.")
    return 1
