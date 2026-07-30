"""Установка ffmpeg/nodejs/tesseract на Bothost без custom Dockerfile."""
from __future__ import annotations

import logging
import os
import re
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

# /tmp на Docker/Bothost часто noexec — node оттуда не запускает EJS → только storyboard.
_NODE_INSTALL_ROOTS = (
    Path(os.environ.get("DATA_DIR") or "/app/data"),
    Path("/app/data"),
    Path("./data"),
)


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
            "xz-utils",
        ]
    )
    _run(["rm", "-rf", "/var/lib/apt/lists"])


def _node_major(bin_path: str) -> int:
    try:
        out = subprocess.check_output(
            [bin_path, "-v"], text=True, stderr=subprocess.DEVNULL, timeout=10
        )
        return int(out.strip().lstrip("v").split(".", 1)[0])
    except Exception:  # noqa: BLE001
        return 0


def _node_can_exec(bin_path: str) -> bool:
    """Проверка, что бинарь реально исполняется (ловим noexec на /tmp)."""
    try:
        out = subprocess.check_output(
            [bin_path, "-e", "console.log('ejs-ok')"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
        return "ejs-ok" in out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Node exec failed (%s): %s", bin_path, exc)
        return False


def _preferred_node_bin() -> str:
    """Сначала наш tarball в /app/data, потом PATH."""
    folder = None
    machine = os.uname().machine
    suffix = _ARCH_SUFFIX.get(machine)
    if suffix:
        folder = f"node-v{NODE_VER}-{suffix}"
        for root in _NODE_INSTALL_ROOTS:
            candidate = root / folder / "bin" / "node"
            if candidate.is_file() and _node_can_exec(str(candidate)):
                return str(candidate)
    for name in ("node", "nodejs"):
        found = shutil.which(name)
        if found and _node_major(found) >= 18 and _node_can_exec(found):
            return found
    return ""


def _install_node_tarball() -> str:
    """
    yt-dlp-ejs нужен рабочий Node ≥18.
    Без него YouTube отдаёт только storyboard → «Requested format is not available».
    Всегда кладём свой tarball в /app/data (не /tmp: noexec). Apt-node — только fallback.
    """
    machine = os.uname().machine
    suffix = _ARCH_SUFFIX.get(machine)
    if not suffix:
        logger.warning("Node.js tarball: unsupported arch %s", machine)
        return _preferred_node_bin() or _which("node", "nodejs")

    folder = f"node-v{NODE_VER}-{suffix}"
    install_root = None
    for root in _NODE_INSTALL_ROOTS:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            install_root = root
            break
        except OSError:
            continue
    if install_root is None:
        logger.error("Cannot write Node.js under /app/data — EJS will fail")
        return _preferred_node_bin() or _which("node", "nodejs")

    node_bin = install_root / folder / "bin" / "node"
    if node_bin.is_file() and _node_can_exec(str(node_bin)):
        bin_dir = str(node_bin.parent)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["YTDLP_NODE"] = str(node_bin)
        logger.info("Node.js ok (cached): %s (v%s)", node_bin, _node_major(str(node_bin)))
        return str(node_bin)

    url = f"https://nodejs.org/dist/v{NODE_VER}/{folder}.tar.xz"
    archive = install_root / f"{folder}.tar.xz"
    logger.info("Downloading Node.js %s → %s …", NODE_VER, install_root)
    try:
        with urlopen(url, timeout=180) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(archive, "r:xz") as tar:
            tar.extractall(install_root)
    except Exception as exc:  # noqa: BLE001
        logger.error("Node.js download/extract failed: %s", exc)
        return _preferred_node_bin() or _which("node", "nodejs")
    finally:
        archive.unlink(missing_ok=True)

    if node_bin.is_file():
        try:
            node_bin.chmod(node_bin.stat().st_mode | 0o111)
        except OSError:
            pass
        if not _node_can_exec(str(node_bin)):
            logger.error(
                "Node.js at %s is not executable (noexec?). YouTube formats will fail.",
                node_bin,
            )
            return _preferred_node_bin() or _which("node", "nodejs")
        bin_dir = str(node_bin.parent)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["YTDLP_NODE"] = str(node_bin)
        logger.info(
            "Node.js tarball active: %s (v%s)", node_bin, _node_major(str(node_bin))
        )
        return str(node_bin)

    logger.error("Node.js binary missing after extract")
    return _preferred_node_bin() or _which("node", "nodejs")


def _probe_ytdlp_js_challenge(node_bin: str) -> None:
    """
    Один раз при старте: без рабочего EJS скачивание всегда даст format not available.
    Не качаем файл — только list formats.
    """
    if not node_bin:
        logger.error("yt-dlp EJS probe skipped: no node")
        return
    try:
        import yt_dlp_ejs  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        logger.error("yt-dlp-ejs not importable — pip install failed?")
        return

    vid = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # короткий публичный ролик
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--js-runtimes",
        f"node:{node_bin}",
        "--force-ipv4",
        "--no-download",
        "-F",
        vid,
    ]
    proxy = (os.environ.get("YTDLP_PROXY") or os.environ.get("YTMUSIC_PROXY") or "").strip()
    if proxy and proxy.lower() not in {"none", "off", "0", "false"}:
        cmd.extend(["--proxy", proxy])
    cookies = Path(os.environ.get("DATA_DIR") or "/app/data") / "youtube_cookies.txt"
    if not cookies.is_file():
        cookies = Path("/app/data/youtube_cookies.txt")
    if cookies.is_file():
        cmd.extend(["--cookies", str(cookies)])

    logger.info("yt-dlp EJS probe…")
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            env=os.environ.copy(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("yt-dlp EJS probe failed to run: %s", exc)
        return

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    low = out.lower()
    if "n challenge solving failed" in low or "only images are available" in low:
        logger.error(
            "yt-dlp EJS BROKEN on this host — YouTube вернёт только storyboard. "
            "node=%s tail=%s",
            node_bin,
            out[-400:].replace("\n", " | "),
        )
        return
    if "sign in to confirm" in low:
        logger.warning("yt-dlp EJS probe: bot-check (cookies/proxy), JS runtime may still be ok")
        return
    # любой не-storyboard формат
    if re.search(r"(?m)^(?:18|91|92|93|94|95|96|140|251)\s+", out):
        logger.info("yt-dlp EJS probe OK (real formats present)")
        return
    logger.warning(
        "yt-dlp EJS probe inconclusive exit=%s tail=%s",
        proc.returncode,
        out[-300:].replace("\n", " | "),
    )


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
    node = _install_node_tarball()
    _ensure_pip_ffmpeg()
    _upgrade_ytdlp()
    node = node or _preferred_node_bin() or _which("node", "nodejs") or ""
    node_ver = _node_major(node) if node else 0
    logger.info(
        "Deps: ffmpeg=%s node=%s (v%s) tesseract=%s",
        _which("ffmpeg") or "none",
        node or "none",
        node_ver or "?",
        _which("tesseract") or "none",
    )
    _probe_ytdlp_js_challenge(node)


# публичный алиас для вызова после записи cookies из B64
probe_ytdlp_js_challenge = _probe_ytdlp_js_challenge
preferred_node_bin = _preferred_node_bin
