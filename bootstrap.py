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
from typing import Optional
from urllib.request import urlopen

logger = logging.getLogger(__name__)

# yt-dlp EJS: Node minimum is 22 (uses `node --permission`). Node 20 = challenge fail.
NODE_VER = "22.23.2"
NODE_MIN_MAJOR = 22
DENO_VER = "2.9.4"
_ARCH_SUFFIX = {
    "x86_64": "linux-x64",
    "aarch64": "linux-arm64",
    "arm64": "linux-arm64",
}
_DENO_ARCH = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
    "arm64": "aarch64-unknown-linux-gnu",
}
_bootstrapped = False

# /tmp на Docker/Bothost часто noexec — бинарники только в /app/data.
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
    """Сначала наш tarball в /app/data (Node≥22), потом PATH."""
    machine = os.uname().machine
    suffix = _ARCH_SUFFIX.get(machine)
    if suffix:
        folder = f"node-v{NODE_VER}-{suffix}"
        for root in _NODE_INSTALL_ROOTS:
            candidate = root / folder / "bin" / "node"
            if (
                candidate.is_file()
                and _node_major(str(candidate)) >= NODE_MIN_MAJOR
                and _node_can_exec(str(candidate))
            ):
                return str(candidate)
    for name in ("node", "nodejs"):
        found = shutil.which(name)
        if (
            found
            and _node_major(found) >= NODE_MIN_MAJOR
            and _node_can_exec(found)
        ):
            return found
    return ""


def _install_root() -> Optional[Path]:
    for root in _NODE_INSTALL_ROOTS:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return root
        except OSError:
            continue
    return None


def _install_node_tarball() -> str:
    """
    yt-dlp EJS требует Node ≥22 (`node --permission`).
    Node 20 (apt на Bothost) → n challenge fail → только storyboard.
    """
    machine = os.uname().machine
    suffix = _ARCH_SUFFIX.get(machine)
    if not suffix:
        logger.warning("Node.js tarball: unsupported arch %s", machine)
        return _preferred_node_bin() or _which("node", "nodejs")

    install_root = _install_root()
    if install_root is None:
        logger.error("Cannot write Node.js under /app/data — EJS will fail")
        return _preferred_node_bin() or _which("node", "nodejs")

    folder = f"node-v{NODE_VER}-{suffix}"
    node_bin = install_root / folder / "bin" / "node"
    if (
        node_bin.is_file()
        and _node_major(str(node_bin)) >= NODE_MIN_MAJOR
        and _node_can_exec(str(node_bin))
    ):
        bin_dir = str(node_bin.parent)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ["YTDLP_NODE"] = str(node_bin)
        logger.info("Node.js ok (cached): %s (v%s)", node_bin, _node_major(str(node_bin)))
        return str(node_bin)

    # старый Node 20 в /app/data больше не подходит — качаем 22
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


def _install_deno() -> str:
    """Deno — recommended runtime для yt-dlp EJS (≥2.3)."""
    env_deno = (os.environ.get("YTDLP_DENO") or "").strip()
    if env_deno and Path(env_deno).is_file():
        return env_deno
    install_root = _install_root()
    if install_root is None:
        return shutil.which("deno") or ""
    deno_bin = install_root / "deno" / "deno"
    if deno_bin.is_file():
        try:
            deno_bin.chmod(deno_bin.stat().st_mode | 0o111)
        except OSError:
            pass
        try:
            subprocess.check_output([str(deno_bin), "-V"], timeout=10, text=True)
            os.environ["YTDLP_DENO"] = str(deno_bin)
            os.environ["PATH"] = str(deno_bin.parent) + os.pathsep + os.environ.get(
                "PATH", ""
            )
            logger.info("Deno ok (cached): %s", deno_bin)
            return str(deno_bin)
        except Exception:  # noqa: BLE001
            pass

    machine = os.uname().machine
    arch = _DENO_ARCH.get(machine)
    if not arch:
        return shutil.which("deno") or ""
    url = (
        f"https://github.com/denoland/deno/releases/download/"
        f"v{DENO_VER}/deno-{arch}.zip"
    )
    archive = install_root / f"deno-{DENO_VER}.zip"
    logger.info("Downloading Deno %s → %s …", DENO_VER, install_root)
    try:
        import zipfile

        with urlopen(url, timeout=180) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        dest = install_root / "deno"
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deno download failed (node-only fallback): %s", exc)
        return shutil.which("deno") or ""
    finally:
        archive.unlink(missing_ok=True)

    if deno_bin.is_file():
        try:
            deno_bin.chmod(deno_bin.stat().st_mode | 0o111)
        except OSError:
            pass
        os.environ["YTDLP_DENO"] = str(deno_bin)
        os.environ["PATH"] = str(deno_bin.parent) + os.pathsep + os.environ.get(
            "PATH", ""
        )
        logger.info("Deno active: %s", deno_bin)
        return str(deno_bin)
    return shutil.which("deno") or ""


def _js_runtime_args(node_bin: str = "", deno_bin: str = "") -> list[str]:
    """
    Только Node ≥22 + скрипты из pip yt-dlp-ejs.
    Deno первым + ejs:npm/github на Bothost даёт ложный «n challenge solving failed»
    (npm/GitHub с NL VPS недоступны / deno падает, yt-dlp не успевает взять node).
    """
    prefer_deno = (os.environ.get("YTDLP_PREFER_DENO") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    parts: list[str] = []
    deno_bin = deno_bin or (os.environ.get("YTDLP_DENO") or "").strip()
    node_bin = node_bin or (os.environ.get("YTDLP_NODE") or "").strip()
    if prefer_deno and deno_bin and Path(deno_bin).is_file():
        parts.append(f"deno:{deno_bin}")
    if node_bin and Path(node_bin).is_file():
        parts.append(f"node:{node_bin}")
    elif deno_bin and Path(deno_bin).is_file():
        parts.append(f"deno:{deno_bin}")
    if not parts:
        return []
    # НЕ включаем --remote-components: на Bothost github/npm часто недоступны
    # и ломают solve, хотя yt-dlp-ejs уже установлен через pip.
    return ["--js-runtimes", ",".join(parts)]


def _probe_ytdlp_js_challenge(node_bin: str) -> None:
    """
    Один раз при старте: без рабочего EJS скачивание всегда даст format not available.
    Не качаем файл — только list formats.
    """
    deno_bin = (os.environ.get("YTDLP_DENO") or "").strip()
    if not node_bin and not deno_bin:
        logger.error("yt-dlp EJS probe skipped: no node/deno")
        return
    try:
        import yt_dlp_ejs  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        logger.error("yt-dlp-ejs not importable — pip install failed?")
        return

    # Быстрая проверка, что node --permission (как у yt-dlp) жив
    if node_bin and Path(node_bin).is_file():
        try:
            proc_n = subprocess.run(
                [node_bin, "--permission", "-e", "console.log('perm-ok')"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if "perm-ok" not in (proc_n.stdout or ""):
                logger.error(
                    "node --permission broken: rc=%s out=%s err=%s",
                    proc_n.returncode,
                    (proc_n.stdout or "")[:120],
                    (proc_n.stderr or "")[:200],
                )
            else:
                logger.info("node --permission OK")
        except Exception as exc:  # noqa: BLE001
            logger.error("node --permission test failed: %s", exc)

    vid = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # короткий публичный ролик
    runtime = _js_runtime_args(node_bin=node_bin, deno_bin=deno_bin)
    cookies = Path(os.environ.get("DATA_DIR") or "/app/data") / "youtube_cookies.txt"
    if not cookies.is_file():
        cookies = Path("/app/data/youtube_cookies.txt")
    proxy = (os.environ.get("YTDLP_PROXY") or os.environ.get("YTMUSIC_PROXY") or "").strip()
    if proxy.lower() in {"none", "off", "0", "false"}:
        proxy = ""

    def _run_probe(label: str, *, use_proxy: bool, use_cookies: bool) -> str:
        cmd = [sys.executable, "-m", "yt_dlp", "-v"]
        cmd.extend(runtime)
        cmd.extend(["--force-ipv4", "--no-download", "-F", vid])
        if use_proxy and proxy:
            cmd.extend(["--proxy", proxy])
        if use_cookies and cookies.is_file():
            cmd.extend(["--cookies", str(cookies)])
        logger.info(
            "yt-dlp EJS probe [%s] proxy=%s cookies=%s …",
            label,
            "yes" if (use_proxy and proxy) else "no",
            "yes" if (use_cookies and cookies.is_file()) else "no",
        )
        env = os.environ.copy()
        # только --proxy; env-прокси ломают child node/deno
        for key in list(env):
            if key.lower() in {
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
                "http_proxy",
            }:
                env.pop(key, None)
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("yt-dlp EJS probe [%s] failed to run: %s", label, exc)
            return ""
        return (proc.stdout or "") + "\n" + (proc.stderr or "")

    def _judge(label: str, out: str) -> bool:
        low = out.lower()
        jsc = " | ".join(
            ln.strip()
            for ln in out.splitlines()
            if "jsc" in ln.lower() or "challenge" in ln.lower() or "JS runtime" in ln
        )[:500]
        if jsc:
            logger.info("yt-dlp EJS probe [%s] jsc: %s", label, jsc)
        if "solving js challenges" in low and "n challenge solving failed" not in low:
            if re.search(r"(?m)^(?:18|91|92|93|94|95|96|140|251|395)\s+", out) or "m3u8" in low:
                logger.info("yt-dlp EJS probe OK [%s]", label)
                return True
        if re.search(r"(?m)^(?:18|91|92|93|94|95|96|140|251|395)\s+", out):
            logger.info("yt-dlp EJS probe OK [%s] (formats present)", label)
            return True
        if "sign in to confirm" in low:
            logger.warning("yt-dlp EJS probe [%s]: bot-check", label)
            return False
        if "n challenge solving failed" in low or "only images are available" in low:
            logger.error(
                "yt-dlp EJS BROKEN [%s] tail=%s",
                label,
                out[-500:].replace("\n", " | "),
            )
            return False
        logger.warning(
            "yt-dlp EJS probe [%s] inconclusive tail=%s",
            label,
            out[-300:].replace("\n", " | "),
        )
        return False

    # 1) cookies, без прокси — проверяем solver отдельно от Webshare
    # 2) cookies + proxy — как в проде
    ok = False
    if cookies.is_file():
        ok = _judge("cookies-noproxy", _run_probe("cookies-noproxy", use_proxy=False, use_cookies=True)) or ok
    ok = _judge(
        "cookies-proxy" if cookies.is_file() else "proxy-only",
        _run_probe(
            "cookies-proxy" if cookies.is_file() else "proxy-only",
            use_proxy=True,
            use_cookies=cookies.is_file(),
        ),
    ) or ok
    if not ok:
        logger.error(
            "yt-dlp EJS still broken after probes — downloads will fail until solver works"
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
    """Свежий yt-dlp[default] + ejs + curl_cffi — иначе только images / bot-check."""
    logger.info("Upgrading yt-dlp[default] + yt-dlp-ejs + curl_cffi…")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "-U",
            "yt-dlp[default]",
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
        from pathlib import Path as _P

        solver = _P(yt_dlp_ejs.__file__).parent / "yt" / "solver" / "core.min.js"
        logger.info(
            "yt-dlp-ejs=ok solver=%s",
            "yes" if solver.is_file() else f"MISSING ({solver})",
        )
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
    deno = _install_deno()
    node = _install_node_tarball()
    _ensure_pip_ffmpeg()
    _upgrade_ytdlp()
    node = node or _preferred_node_bin() or _which("node", "nodejs") or ""
    node_ver = _node_major(node) if node else 0
    if node and node_ver < NODE_MIN_MAJOR:
        logger.error(
            "Node.js v%s < %s — yt-dlp EJS will FAIL (need Node≥22). path=%s",
            node_ver,
            NODE_MIN_MAJOR,
            node,
        )
    logger.info(
        "Deps: ffmpeg=%s node=%s (v%s) deno=%s tesseract=%s",
        _which("ffmpeg") or "none",
        node or "none",
        node_ver or "?",
        deno or "none",
        _which("tesseract") or "none",
    )
    # EJS probe — после cookies в bot.py (иначе ложный BROKEN без LOGIN)


# публичный алиас для вызова после записи cookies из B64
probe_ytdlp_js_challenge = _probe_ytdlp_js_challenge
preferred_node_bin = _preferred_node_bin
js_runtime_args = _js_runtime_args
