"""Memory helpers for small (300-400 MB) hosts."""

from __future__ import annotations

import ctypes
import gc


def trim_memory() -> None:
    """Run GC and hand freed heap pages back to the OS (glibc only)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
