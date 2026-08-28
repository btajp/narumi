"""A macOS extended ACL can grant access despite POSIX mode 0700/0600."""

from __future__ import annotations

import ctypes
import errno
import sys
from functools import lru_cache

from narumi_server.transport_errors import TransportSecurityError

_ACL_TYPE_EXTENDED = 0x100
_ACL_EXTENDED_ALLOW = 1
_ACL_FIRST_ENTRY = 0
_ACL_NEXT_ENTRY = -1


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


def ensure_no_extended_allow_acl(fd: int) -> None:
    """Reject macOS allow ACEs; deny-only system entries do not broaden permissions.

    On POSIX ACL systems the group mask is represented by the already-checked mode bits.
    macOS allow entries are independent of that mask, so they require this extra check.
    """
    if sys.platform != "darwin":
        return
    try:
        library = _libc()
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
        if not acl:
            if ctypes.get_errno() in {errno.ENOENT, getattr(errno, "ENOATTR", -1)}:
                return
            raise TransportSecurityError()
        try:
            entry_kind = _ACL_FIRST_ENTRY
            for _ in range(256):
                entry = ctypes.c_void_p()
                ctypes.set_errno(0)
                result = library.acl_get_entry(acl, entry_kind, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    return  # Darwin reports the end of an ACL as EINVAL.
                if result != 0 or not entry.value:
                    raise TransportSecurityError()
                tag = ctypes.c_int()
                if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                    raise TransportSecurityError()
                if tag.value == _ACL_EXTENDED_ALLOW:
                    raise TransportSecurityError()
                entry_kind = _ACL_NEXT_ENTRY
            raise TransportSecurityError()
        finally:
            library.acl_free(acl)
    except (OSError, AttributeError):
        raise TransportSecurityError() from None
