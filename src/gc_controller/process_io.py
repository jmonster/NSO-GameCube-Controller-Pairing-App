"""Restore inherited IPC pipes in a Windows windowed executable.

A windowed bootloader/pythonw can leave Python standard streams as None even
though CreateProcess passed valid pipe handles. Do not substitute /dev/null for
required input/output: that would turn a broken child transport into false EOF.
"""
import io
import os
import stat
import sys


def _inherited_pipe(index):
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
            wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
            wintypes.BOOL, wintypes.DWORD]
        kernel.DuplicateHandle.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.GetStdHandle((-10 - index) & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value or kernel.GetFileType(handle) != 3:
            raise OSError('Expected an inherited IPC pipe')
        process, duplicate = kernel.GetCurrentProcess(), wintypes.HANDLE()
        if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate),
                                      0, False, 2):  # DUPLICATE_SAME_ACCESS
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # The CRT descriptor owns ONLY the duplicate, never the parent's
            # inherited standard handle. Binary mode preserves 0x0A/0x0D bytes.
            flags = (os.O_RDONLY if index == 0 else os.O_WRONLY) | os.O_BINARY | os.O_NOINHERIT
            return msvcrt.open_osfhandle(duplicate.value, flags)
        except BaseException:
            kernel.CloseHandle(duplicate)
            raise
    if not stat.S_ISFIFO(os.fstat(index).st_mode):
        raise OSError('Expected an inherited IPC pipe')
    return os.dup(index)


def prepare_standard_streams(*, ipc=False):
    """Keep usable streams; require real pipes for a no-console BLE child."""
    created = []
    try:
        for index, name in enumerate(('stdin', 'stdout', 'stderr')):
            stream = getattr(sys, name)
            if stream is not None:
                continue
            required = ipc and index < 2
            try:
                fd = _inherited_pipe(index) if ipc else None
            except OSError:
                if required:
                    raise
                fd = None
            if fd is None:
                stream = open(os.devnull, 'r' if index == 0 else 'w', encoding='utf-8')
            else:
                try:
                    raw = os.fdopen(fd, 'rb' if index == 0 else 'wb', buffering=0)
                except BaseException:
                    os.close(fd)
                    raise
                try:
                    stream = io.TextIOWrapper(raw, encoding='utf-8', newline='\n', write_through=True)
                except BaseException:
                    raw.close()
                    raise
            setattr(sys, name, stream)
            created.append((name, stream))
        if ipc and sys.platform == 'win32':
            import msvcrt
            for name in ('stdin', 'stdout'):
                msvcrt.setmode(getattr(sys, name).fileno(), os.O_BINARY)
    except BaseException:
        for name, stream in created:
            setattr(sys, name, None)
            stream.close()
        raise
