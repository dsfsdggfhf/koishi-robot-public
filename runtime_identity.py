"""Read-only process ownership checks; never treat an unrelated PID as a bot."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex

MODE_ALIASES = {'run': {'run', 'once'}, 'chat-run': {'chat-run', 'chat-once'},
                'health-run': {'health-run', 'health-once'}}

def command_matches(command: str, root: Path, mode: str) -> bool:
    if not command:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        argc = ctypes.c_int()
        argv = shell.CommandLineToArgvW(command, ctypes.byref(argc))
        if not argv:
            return False
        try:
            parts = [argv[i] for i in range(argc.value)]
        finally:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
    else:
        parts = shlex.split(command.replace('\0', ' '))
    script = 'supervisor.py' if mode == 'supervisor' else 'main.py'
    expected = (Path(root) / script).resolve()
    for index, value in enumerate(parts):
        candidate = Path(value)
        # All new launchers use absolute paths. A legacy relative script cannot
        # prove ownership of this directory and must not be killed on that basis.
        if candidate.is_absolute() and candidate.resolve() == expected:
            tail = parts[index + 1:]
            if mode == 'supervisor':
                return not tail or tail[0] in {'run', 'once', 'stop'}
            return any(x in MODE_ALIASES.get(mode, {mode}) for x in tail)
    return False

def _windows_command(pid: int) -> tuple[str, str] | None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER means there is no process with this PID.
        return ('', '') if ctypes.get_last_error() == 87 else None
    try:
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != 259:
            return '', ''
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return None
        image = buffer.value
        if Path(image).name.lower() not in {'python.exe', 'pythonw.exe', 'python3.exe'}:
            return image, ''
        class UnicodeString(ctypes.Structure):
            _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT), ('Buffer', ctypes.c_void_p)]
        nt = ctypes.WinDLL('ntdll')
        query = nt.NtQueryInformationProcess
        query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = ctypes.c_long
        needed = wintypes.ULONG()
        query(handle, 60, None, 0, ctypes.byref(needed))
        if not needed.value or needed.value > 1024 * 1024:
            return None
        data = ctypes.create_string_buffer(needed.value)
        if query(handle, 60, data, len(data), ctypes.byref(needed)) < 0:
            return None
        value = UnicodeString.from_buffer(data)
        return image, ctypes.wstring_at(value.Buffer, value.Length // 2)
    finally:
        kernel.CloseHandle(handle)

def process_matches(pid: int, root: Path, mode: str) -> bool | None:
    """True=verified owner, False=missing/unrelated, None=cannot verify safely."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        info = _windows_command(pid)
        if info is None:
            return None
        image, command = info
        if not image or Path(image).name.lower() not in {'python.exe', 'pythonw.exe', 'python3.exe'}:
            return False
        if not command:
            return None
        # Relative legacy commands cannot be associated with a directory.
        if not command_matches(command, root, mode):
            script = 'supervisor.py' if mode == 'supervisor' else 'main.py'
            if re.search(r'(?:^|\s|")' + re.escape(script) + r'(?:"|\s|$)', command):
                return None
            return False
        return True
    try:
        raw = (Path('/proc') / str(pid) / 'cmdline').read_bytes()
        return command_matches(raw.replace(b'\0', b' ').decode(), root, mode)
    except FileNotFoundError:
        return False
    except OSError:
        return None

def owned_pids(root: Path, mode: str) -> list[int]:
    if os.name != 'nt':
        return [int(p.name) for p in Path('/proc').iterdir()
                if p.name.isdigit() and process_matches(int(p.name), root, mode) is True]
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return []
    result = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() in {'python.exe', 'pythonw.exe', 'python3.exe'}:
                if process_matches(entry.th32ProcessID, root, mode) is True:
                    result.append(int(entry.th32ProcessID))
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    return result
