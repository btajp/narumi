"""Reject macOS ACL grants that bypass an owner-only POSIX permission check."""

from __future__ import annotations

import ctypes
import errno
import sys
from functools import lru_cache

_ACL_TYPE_EXTENDED = 0x100
_ACL_EXTENDED_ALLOW = 1
_ACL_FIRST_ENTRY = 0
_ACL_NEXT_ENTRY = -1
_ERROR = "Provider path permissions could not be verified"


@lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_get_entry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    library.acl_get_tag_type.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    return library


def ensure_no_extended_allow_acl(descriptor: int) -> None:
    """Allow deny-only entries; reject any allow entry or failed inspection.

    POSIX ACL access is bounded by the checked group mode mask. Darwin extended
    allow entries are independent of that mask and require descriptor-based checks.
    """
    if sys.platform != "darwin":
        return
    try:
        library = _libc()
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(descriptor, _ACL_TYPE_EXTENDED)
        if not acl:
            if ctypes.get_errno() in {errno.ENOENT, getattr(errno, "ENOATTR", -1)}:
                return
            raise OSError(_ERROR)
        try:
            entry_kind = _ACL_FIRST_ENTRY
            for _ in range(256):
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                result = library.acl_get_entry(acl, entry_kind, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    return  # Darwin reports the end of an ACL with EINVAL.
                if result != 0 or not entry.value:
                    raise OSError(_ERROR)
                tag = ctypes.c_int()
                if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                    raise OSError(_ERROR)
                if tag.value == _ACL_EXTENDED_ALLOW:
                    raise OSError(_ERROR)
                entry_kind = _ACL_NEXT_ENTRY
            raise OSError(_ERROR)
        finally:
            library.acl_free(acl)
    except (OSError, AttributeError, ValueError, TypeError):
        raise OSError(_ERROR) from None
