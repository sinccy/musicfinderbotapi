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


def _node_major(bin_path: str) -> int:
    try:
        out = subprocess.check_output(
            [bin_path, "-v"], text=True, stderr=subprocess.DEVNULL, timeout=10
        )
        # v20.18.0 → 20
        return int(out.strip().lstrip("v").split(".", 1)[0])
    except Exception:  # noqa: BLE001
        return 0


def _install_node_tarball() -> None:
    """
    yt-dlp-ejs нужен Node ≥18. Apt на Bothost часто даёт старый nodejs —
    тогда YouTube отдаёт только storyboard → «Requested format is not available».
    """
    existing = _which("node", "nodejs")
    if existing and _node_major(existing) >= 18:
        logger.info("Node.js ok: %s (%s)", existing, _node_major(existing))
        return
    if existing:
        logger.warning(
            "Node.js too old for yt-dlp-ejs (%s) — installing %s tarball",
            existing,
            NODE_VER,
        )
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
        logger.info("Node.js tarball active: %s", node_bin)


def _ensure_pip_ffmpeg() -> None:
    if _which("ffmpeg"):
        return
    logger.info("ffmpeg not in PATH — installing imageio-ffmpeg via pip…")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "imageio-ffmpeg"],
        check=False,
    )


def _upgrade_ytdlp() -> None:
    """Свежий yt-dlp + ejs + curl_cffi — иначе только images / bot-check."""
    logger.info("Upgrading yt-dlp + yt-dlp-ejs + curl_cffi…")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "-U",
            "yt-dlp",
            "yt-dlp-ejs",
            "curl_cffi",
        ],
        check=False,
    )
    try:
        from yt_dlp.version import __version__ as ytdlp_ver  # type: ignore

        logger.info("yt-dlp version=%s", ytdlp_ver)
    except Exception:  # noqa: BLE001
        pass
    try:
        import yt_dlp_ejs  # type: ignore  # noqa: F401

        logger.info("yt-dlp-ejs=ok")
    except Exception:  # noqa: BLE001
        logger.warning("yt-dlp-ejs missing — YouTube formats may fail")


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
    node = _which("node", "nodejs") or "none"
    node_ver = _node_major(node) if node != "none" else 0
    logger.info(
        "Deps: ffmpeg=%s node=%s (v%s) tesseract=%s",
        _which("ffmpeg") or "none",
        node,
        node_ver or "?",
        _which("tesseract") or "none",
    )
