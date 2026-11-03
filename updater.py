from __future__ import annotations

"""Check GitHub Releases for a newer build and install it without a reinstall.

The installer is per-user and keeps `data/` and `.env`, so running it silently is
an in-place upgrade. Downloads use the release-asset API because
`github.com/.../releases/download/...` is not reachable on some networks.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

from config import ROOT

REPO = os.getenv("UPDATE_REPO", "P-King999/Voice-Recording")
API = f"https://api.github.com/repos/{REPO}"
CHECK_CACHE = ROOT / "runtime" / "update-check.json"
UPDATE_DIR = ROOT / "runtime" / "updates"
CACHE_TTL_SECONDS = 6 * 3600
REQUEST_TIMEOUT = 15

logger = logging.getLogger("updater")


def _headers(accept: str) -> dict[str, str]:
    """A private repository needs a token; a public one works anonymously."""
    headers = {"Accept": accept, "User-Agent": "voice-recorder"}
    token = os.getenv("UPDATE_GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def current_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value or "")
    return tuple(int(part) for part in numbers[:3]) or (0,)


def is_newer(remote: str, local: str) -> bool:
    return _version_tuple(remote) > _version_tuple(local)


def _read_cache() -> dict[str, object]:
    try:
        loaded = json.loads(CHECK_CACHE.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache(data: dict[str, object]) -> None:
    CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CHECK_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_cached() -> dict[str, object]:
    """Cache-only lookup so the dashboard never blocks on the network."""
    return _read_cache()


def latest_release(timeout: float = REQUEST_TIMEOUT) -> dict[str, object] | None:
    try:
        response = requests.get(
            f"{API}/releases/latest",
            headers=_headers("application/vnd.github+json"),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        logger.info("Could not reach GitHub for an update check", exc_info=True)
        return None


def check(force: bool = False) -> dict[str, object]:
    """Return the current/latest version pair, cached for six hours."""
    cached = _read_cache()
    checked_at = float(cached.get("checked_at") or 0)
    if not force and cached and time.time() - checked_at < CACHE_TTL_SECONDS:
        return cached

    release = latest_release()
    if release is None:
        cached.setdefault("current", current_version())
        cached.setdefault("available", False)
        return cached

    tag = str(release.get("tag_name") or "").lstrip("vV")
    assets = release.get("assets") or []
    asset = next((item for item in assets if str(item.get("name", "")).endswith(".exe")), None)
    result: dict[str, object] = {
        "checked_at": time.time(),
        "current": current_version(),
        "latest": tag,
        "available": bool(tag) and is_newer(tag, current_version()),
        "notes": str(release.get("body") or ""),
        "html_url": str(release.get("html_url") or ""),
        "asset_id": int(asset["id"]) if asset else 0,
        "asset_name": str(asset["name"]) if asset else "",
        "asset_size": int(asset["size"]) if asset else 0,
    }
    try:
        _write_cache(result)
    except OSError:
        logger.exception("Could not cache the update check")
    return result


def download(asset_id: int, asset_name: str, on_progress: object | None = None) -> Path:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = UPDATE_DIR / (asset_name or "VoiceRecording-Setup.exe")
    with requests.get(
        f"{API}/releases/assets/{asset_id}",
        headers=_headers("application/octet-stream"),
        stream=True,
        timeout=120,
    ) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 512):
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                if callable(on_progress):
                    on_progress(done, total)
    return target


def launch_installer(installer: Path) -> None:
    """Run a detached helper so stopping services does not kill the download."""
    helper = ROOT / "update_helper.py"
    temporary = Path(tempfile_dir()) / "voice-update-helper.py"
    temporary.write_text(helper.read_text(encoding="utf-8"), encoding="utf-8")
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    executable = str(pythonw) if pythonw.exists() else sys.executable
    creation = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [executable, str(temporary), str(installer), str(ROOT)],
        cwd=str(ROOT),
        creationflags=creation,
        close_fds=True,
    )


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()
