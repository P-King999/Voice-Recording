from __future__ import annotations

"""Move files to the operating system's recycle bin instead of erasing them.

The app deletes silent fragments and user-removed recordings. Sending them to
the Windows Recycle Bin keeps a safety net: the user can restore them until the
bin is emptied.
"""

import ctypes
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_void_p),
    ]


def _windows_recycle(paths: Sequence[Path]) -> bool:
    """One shell call for the whole batch; returns False when it could not finish."""
    if os.name != "nt":
        return False
    joined = "\0".join(str(path) for path in paths) + "\0\0"
    buffer = ctypes.create_unicode_buffer(joined, len(joined) + 2)
    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = ctypes.addressof(buffer)
    operation.pTo = None
    operation.fFlags = FLAGS
    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except OSError:
        logger.exception("SHFileOperation failed for %s file(s)", len(paths))
        return False
    if result != 0 or operation.fAnyOperationsAborted:
        logger.warning("Recycle bin refused %s file(s): code %s", len(paths), result)
        return False
    return True


def _send2trash(path: Path) -> bool:
    try:
        from send2trash import send2trash
    except ImportError:
        return False
    try:
        send2trash(str(path))
        return True
    except Exception:
        logger.exception("send2trash failed for %s", path)
        return False


def recycle_paths(paths: Iterable[Path]) -> tuple[list[Path], int]:
    """Send files to the recycle bin.

    Returns the paths that are no longer on disk (including ones that were
    already missing) and the number of bytes released.
    """
    existing: list[Path] = []
    sizes: dict[Path, int] = {}
    for path in paths:
        try:
            sizes[path] = path.stat().st_size
            existing.append(path)
        except OSError:
            continue
    if not existing:
        return [], 0

    handled = _windows_recycle(existing)
    if not handled:
        for path in existing:
            _send2trash(path)
    removed = [path for path in existing if not path.exists()]
    if len(removed) < len(existing):
        logger.warning("Could not move %s file(s) to the recycle bin", len(existing) - len(removed))
    return removed, sum(sizes.get(path, 0) for path in removed)


def recycle(path: Path) -> bool:
    removed, _ = recycle_paths([path])
    return path in removed or not path.exists()
