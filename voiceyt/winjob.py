"""Windows job object that ties child processes to our lifetime.

``assign_kill_on_close(handle)`` puts *handle* (an mpv process handle) into a
job object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when *our* process
dies - console window closed, crash, taskkill, anything - the kernel kills
every process in the job.  Without this, closing the voiceyt console leaves
mpv playing music with nobody left to stop it.
"""

from __future__ import annotations

import ctypes
import logging

LOGGER = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9

# ctypes.wintypes does not ship these structures.
_SIZEOF_PTR = ctypes.sizeof(ctypes.c_void_p)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", _SIZEOF_PTR and ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", _SIZEOF_PTR and ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def assign_kill_on_close(process_handle: int) -> bool:
    """Put *process_handle* into a kill-on-close job.  Never raises."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False
        limits = _EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            return False
        if not kernel32.AssignProcessToJobObject(
            job, ctypes.c_void_p(process_handle)
        ):
            return False
        # Leak the job handle on purpose: the kernel owns the lifetime now.
        return True
    except Exception:  # pragma: no cover - best effort, never fatal
        LOGGER.debug("kill-on-close job failed", exc_info=True)
        return False
