"""Установка ffmpeg/nodejs/tesseract на Bothost без custom Dockerfile."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import urlopen

logger = logging.getLogger(__name__)

NODE_VER = "20.18.0"
_ARCH_SUFFIX = {
    "x86_64": "linux-x64",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
}
_bootstrapped = False


def _run(cmd: list[str]) -> int:
    try:
        return subprocess.run(cmd, check=False).returncode
    except FileNotFoundError:
        return 127


def _which(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _ensure_data_dir() -> None:
    for path in ("/app/data", "./data"):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            return
        except OSError:
            continue


def _install_apt_packages() -> None:
    if not shutil.which("apt-get"):
        return
    os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")
    _run(["apt-get", "update", "-qq"])
    _run(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "ffmpeg",
            "nodejs",
            "tesseract-ocr",
            "tesseract-ocr-eng",
            "tesseract-ocr-rus",
            "curl",
            "ca-certificates",
        ]
    )
    _run(["rm", "-rf", "/var/lib/apt/lists"])


def _install_node_tarball() -> None:
    if _which("node", "nodejs"):
        return
    machine = os.uname().machine
    suffix = _ARCH_SUFFIX.get(machine)
    if not suffix:
        logger.warning("Node.js tarball: unsupported arch %s", machine)
        return
    folder = f"node-v{NODE_VER}-{suffix}"
    node_bin = Path("/tmp") / folder / "bin" / "node"
    if not node_bin.is_file():
        url = f"https://nodejs.org/dist/v{NODE_VER}/{folder}.tar.xz"
        archive = Path("/tmp") / f"{folder}.tar.xz"
        logger.info("Downloading Node.js %s (%s)…", NODE_VER, machine)
        with urlopen(url, timeout=120) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(archive, "r:xz") as tar:
            tar.extractall("/tmp")
        archive.unlink(missing_ok=True)
    if node_bin.is_file():
        bin_dir = str(node_bin.parent)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def _ensure_pip_ffmpeg() -> None:
    if _which("ffmpeg"):
        return
    logger.info("ffmpeg not in PATH — installing imageio-ffmpeg via pip…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "imageio-ffmpeg"],
        check=False,
    )


def _upgrade_ytdlp() -> None:
    """Свежий yt-dlp критичен: старый не видит аудиоформаты YouTube."""
    logger.info("Upgrading yt-dlp…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"],
        check=False,
    )
    try:
        from yt_dlp.version import __version__ as ytdlp_ver  # type: ignore

        logger.info("yt-dlp version=%s", ytdlp_ver)
    except Exception:  # noqa: BLE001
        pass


def ensure_system_deps() -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    _ensure_data_dir()
    need_ffmpeg = not _which("ffmpeg")
    need_node = not _which("node", "nodejs")
    need_tesseract = not _which("tesseract")
    if need_ffmpeg or need_node or need_tesseract:
        logger.info("Bothost: installing system deps (ffmpeg, nodejs, tesseract)…")
        _install_apt_packages()
    _install_node_tarball()
    _ensure_pip_ffmpeg()
    _upgrade_ytdlp()
    logger.info(
        "Deps: ffmpeg=%s node=%s tesseract=%s",
        _which("ffmpeg") or "none",
        _which("node", "nodejs") or "none",
        _which("tesseract") or "none",
    )
