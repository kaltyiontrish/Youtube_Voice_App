"""CUDA / cuDNN DLL bootstrap.

Both CTranslate2 (faster-whisper) and ONNX Runtime load cuBLAS / cuDNN at
runtime.  When those libraries come from the pip wheels (``nvidia-*-cu12``)
instead of a system-wide CUDA installation their DLLs live in
``site-packages/nvidia/<lib>/bin`` and are *not* on ``PATH``, so the loaders
fail with errors such as ``cublas64_12.dll not found``.

Adding those directories before the first import of ctranslate2 / onnxruntime
keeps this project pip-only: no system CUDA toolkit, no manual PATH editing.
Call :func:`enable_cuda_dlls` once, very early in the process.
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

_ALREADY_ENABLED = False


def _site_packages_dirs() -> list[Path]:
    """Return every ``site-packages`` directory visible to this interpreter."""
    dirs: list[Path] = []
    try:
        dirs.extend(Path(p) for p in site.getsitepackages())
    except Exception:  # pragma: no cover - exotic environments
        pass
    try:
        user_site = site.getusersitepackages()
        if user_site:
            dirs.append(Path(user_site))
    except Exception:  # pragma: no cover
        pass
    # A virtualenv keeps its packages next to the interpreter.
    dirs.append(Path(sys.prefix) / "Lib" / "site-packages")
    return dirs


def cuda_dll_dirs() -> list[Path]:
    """Existing ``nvidia/*/bin`` directories inside the installed wheels."""
    found: list[Path] = []
    seen: set[str] = set()
    for sp in _site_packages_dirs():
        nvidia = sp / "nvidia"
        if not nvidia.is_dir():
            continue
        for candidate in sorted(nvidia.glob("*/bin")):
            key = str(candidate).lower()
            if candidate.is_dir() and key not in seen:
                seen.add(key)
                found.append(candidate)
    return found


def enable_cuda_dlls(verbose: bool = False) -> list[str]:
    """Make the pip-installed CUDA libraries loadable and return their paths.

    On Windows ``os.add_dll_directory`` is the supported mechanism; on POSIX
    systems the dynamic loader honours ``LD_LIBRARY_PATH``, so the directories
    are prepended to it.  Safe to call more than once.
    """
    global _ALREADY_ENABLED

    dirs = cuda_dll_dirs()
    added: list[str] = []
    for directory in dirs:
        text = str(directory)
        added.append(text)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(text)
            except OSError:
                # Already registered or not a directory any more; PATH still helps.
                pass
        current = os.environ.get("PATH", "")
        if text.lower() not in current.lower():
            os.environ["PATH"] = text + os.pathsep + current

    if added and verbose:
        print("CUDA library directories:", *added, sep="\n  ")

    _ALREADY_ENABLED = True
    return added


def cuda_available() -> bool:
    """True when at least one pip-provided CUDA library directory is present."""
    return bool(cuda_dll_dirs())