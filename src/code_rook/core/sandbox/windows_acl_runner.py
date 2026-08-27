from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

from code_rook.core.windows_job import create_kill_on_close_job

_TOKEN_ASSIGN_PRIMARY = 0x0001
_TOKEN_DUPLICATE = 0x0002
_TOKEN_QUERY = 0x0008
_TOKEN_ADJUST_DEFAULT = 0x0080
_TOKEN_GROUPS = 2
_TOKEN_DEFAULT_DACL = 6
_SE_GROUP_LOGON_ID = 0xC0000000
_DISABLE_MAX_PRIVILEGE = 0x00000001
_LUA_TOKEN = 0x00000004
_WRITE_RESTRICTED = 0x00000008
_STARTF_USESHOWWINDOW = 0x00000001
_STARTF_USESTDHANDLES = 0x00000100
_SW_HIDE = 0
_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_HANDLE_FLAG_INHERIT = 0x00000001
_INFINITE = 0xFFFFFFFF
_RUNNER_FAILURE_EXIT = 127
_RUNNER_PREFIX = "windows-acl-run:"
_FILE_GRANT_MASK = 0x00110156
_SET_ACCESS = 2
_SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_GRANT_ACCESS = 1
_FILE_ALL_ACCESS = 0x001F01FF


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _TrusteeW(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class _ExplicitAccessW(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", _TrusteeW),
    ]


# 返回不受工作区同名包劫持的隔离 Python runner 前缀
def runner_command() -> list[str]:
    return [sys.executable, "-I", str(Path(__file__).resolve())]


# 返回当前 Windows API 绑定表，并为所有安全边界调用声明精确签名
def _windows_api() -> tuple[Any, Any]:
    if os.name != "nt":
        raise OSError("Windows ACL sandbox is unavailable on this platform")
    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    advapi32 = win_dll("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.GetVolumePathNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.CopySid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_SidAndAttributes),
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = wintypes.BOOL
    advapi32.SetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    advapi32.SetTokenInformation.restype = wintypes.BOOL
    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo),
        ctypes.POINTER(_ProcessInformation),
    ]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(_ExplicitAccessW),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    return kernel32, advapi32


# 把最后一个 Win32 错误转换成包含调用点和系统文本的异常
def _win32_error(operation: str) -> OSError:
    get_last_error = getattr(ctypes, "get_last_error")
    format_error = getattr(ctypes, "FormatError")
    code = get_last_error()
    return OSError(code, f"{operation} failed (Win32 {code}): {format_error(code)}")


# 返回与规范路径和用途绑定的 64 位 capability SID，避免跨工作区复用写权限
def capability_sid(path: Path, *, domain: str) -> str:
    canonical = str(path.resolve()).lower().encode("utf-8")
    digest = hashlib.sha256(domain.encode("ascii") + b"\0" + canonical).digest()
    first = int.from_bytes(digest[:4], "little")
    second = int.from_bytes(digest[4:8], "little")
    return f"S-1-4-{first}-{second}"


# 验证目标位于支持持久 ACL 的 NTFS/ReFS 文件系统，其他卷一律失败关闭
def _require_acl_filesystem(path: Path, kernel32: Any) -> None:
    volume = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetVolumePathNameW(str(path), volume, len(volume)):
        raise _win32_error(f"GetVolumePathNameW({path})")
    filesystem = ctypes.create_unicode_buffer(64)
    if not kernel32.GetVolumeInformationW(
        volume.value,
        None,
        0,
        None,
        None,
        None,
        filesystem,
        len(filesystem),
    ):
        raise _win32_error(f"GetVolumeInformationW({volume.value})")
    if filesystem.value.upper() not in {"NTFS", "REFS"}:
        raise OSError(f"Windows ACL sandbox requires NTFS/ReFS, got {filesystem.value}")


# 在跨进程命名互斥量内合并可继承 capability ACE，失败时不启动受限子进程
def _grant_write(path: Path, sid: str, kernel32: Any, advapi32: Any) -> None:
    mutex_name = "Local\\CodeRookAcl-" + hashlib.sha256(
        str(path.resolve()).lower().encode("utf-8")
    ).hexdigest()[:24]
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if not mutex:
        raise _win32_error(f"CreateMutexW({path})")
    descriptor = ctypes.c_void_p()
    merged_acl = ctypes.c_void_p()
    sid_pointer = ctypes.c_void_p()
    mutex_acquired = False
    try:
        wait_result = kernel32.WaitForSingleObject(mutex, 120_000)
        if wait_result not in {0, 0x80}:
            raise OSError(f"ACL lock wait failed for {path}: {wait_result}")
        mutex_acquired = True
        if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_pointer)):
            raise _win32_error(f"ConvertStringSidToSidW({sid})")
        old_acl = ctypes.c_void_p()
        result = advapi32.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_acl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise OSError(result, f"GetNamedSecurityInfoW failed for {path}")
        entry = _ExplicitAccessW()
        entry.grfAccessPermissions = _FILE_GRANT_MASK
        entry.grfAccessMode = _SET_ACCESS
        entry.grfInheritance = _SUB_CONTAINERS_AND_OBJECTS_INHERIT
        entry.Trustee.pMultipleTrustee = None
        entry.Trustee.MultipleTrusteeOperation = 0
        entry.Trustee.TrusteeForm = 0
        entry.Trustee.TrusteeType = 0
        entry.Trustee.ptstrName = sid_pointer
        result = advapi32.SetEntriesInAclW(
            1, ctypes.byref(entry), old_acl, ctypes.byref(merged_acl)
        )
        if result != 0 or not merged_acl.value:
            raise OSError(result, f"SetEntriesInAclW failed for {path}")
        result = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION,
            None,
            None,
            merged_acl,
            None,
        )
        if result != 0:
            raise OSError(result, f"SetNamedSecurityInfoW failed for {path}")
    finally:
        if sid_pointer.value:
            kernel32.LocalFree(sid_pointer)
        if descriptor.value:
            kernel32.LocalFree(descriptor)
        if merged_acl.value:
            kernel32.LocalFree(merged_acl)
        if mutex_acquired:
            kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


# 打开当前主令牌并复制日志会话 SID，供 restricted token 保持桌面和 DLL 初始化能力
def _current_token_and_logon_sid(
    kernel32: Any,
    advapi32: Any,
) -> tuple[int, ctypes.Array[ctypes.c_char]]:
    token = wintypes.HANDLE()
    access = (
        _TOKEN_ASSIGN_PRIMARY
        | _TOKEN_DUPLICATE
        | _TOKEN_QUERY
        | _TOKEN_ADJUST_DEFAULT
    )
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), access, ctypes.byref(token)
    ):
        raise _win32_error("OpenProcessToken")
    needed = wintypes.DWORD()
    advapi32.GetTokenInformation(token, _TOKEN_GROUPS, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        kernel32.CloseHandle(token)
        raise _win32_error("GetTokenInformation(TokenGroups size)")
    groups = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetTokenInformation(
        token,
        _TOKEN_GROUPS,
        groups,
        needed,
        ctypes.byref(needed),
    ):
        kernel32.CloseHandle(token)
        raise _win32_error("GetTokenInformation(TokenGroups)")
    count = ctypes.c_uint32.from_buffer(groups).value
    offset = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
    for index in range(count):
        entry = _SidAndAttributes.from_address(
            ctypes.addressof(groups) + offset + index * ctypes.sizeof(_SidAndAttributes)
        )
        if entry.Attributes & _SE_GROUP_LOGON_ID != _SE_GROUP_LOGON_ID:
            continue
        length = advapi32.GetLengthSid(entry.Sid)
        if length == 0:
            kernel32.CloseHandle(token)
            raise _win32_error("GetLengthSid(logon SID)")
        copied = ctypes.create_string_buffer(length)
        if not advapi32.CopySid(length, copied, entry.Sid):
            kernel32.CloseHandle(token)
            raise _win32_error("CopySid(logon SID)")
        token_value = int(token.value or 0)
        if token_value == 0:
            raise OSError("OpenProcessToken returned a null handle")
        return token_value, copied
    kernel32.CloseHandle(token)
    raise OSError("CreateRestrictedToken prerequisite failed: logon SID not found")


# 把字符串 SID 转成由 LocalAlloc 管理的原生 SID 指针
def _convert_sid(value: str, advapi32: Any) -> ctypes.c_void_p:
    pointer = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(value, ctypes.byref(pointer)):
        raise _win32_error(f"ConvertStringSidToSidW({value})")
    return pointer


# 给 restricted token 的默认 DACL 加入 restricting SID，允许新文件和匿名管道通过二次写检查
def _set_token_default_dacl(
    token: int,
    sid_pointer: ctypes.c_void_p,
    kernel32: Any,
    advapi32: Any,
) -> None:
    needed = wintypes.DWORD()
    advapi32.GetTokenInformation(
        token, _TOKEN_DEFAULT_DACL, None, 0, ctypes.byref(needed)
    )
    if needed.value == 0:
        raise _win32_error("GetTokenInformation(TokenDefaultDacl size)")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetTokenInformation(
        token,
        _TOKEN_DEFAULT_DACL,
        buffer,
        needed,
        ctypes.byref(needed),
    ):
        raise _win32_error("GetTokenInformation(TokenDefaultDacl)")
    current_acl_value = ctypes.c_void_p.from_buffer(buffer).value
    if not current_acl_value:
        raise OSError("restricted token carries no default DACL")
    entry = _ExplicitAccessW()
    entry.grfAccessPermissions = _FILE_ALL_ACCESS
    entry.grfAccessMode = _GRANT_ACCESS
    entry.grfInheritance = _SUB_CONTAINERS_AND_OBJECTS_INHERIT
    entry.Trustee.pMultipleTrustee = None
    entry.Trustee.MultipleTrusteeOperation = 0
    entry.Trustee.TrusteeForm = 0
    entry.Trustee.TrusteeType = 0
    entry.Trustee.ptstrName = sid_pointer
    merged_acl = ctypes.c_void_p()
    result = advapi32.SetEntriesInAclW(
        1,
        ctypes.byref(entry),
        ctypes.c_void_p(current_acl_value),
        ctypes.byref(merged_acl),
    )
    if result != 0 or not merged_acl.value:
        raise OSError(result, "SetEntriesInAclW failed for TokenDefaultDacl")
    try:
        default_dacl = ctypes.c_void_p(merged_acl.value)
        if not advapi32.SetTokenInformation(
            token,
            _TOKEN_DEFAULT_DACL,
            ctypes.byref(default_dacl),
            ctypes.sizeof(default_dacl),
        ):
            raise _win32_error("SetTokenInformation(TokenDefaultDacl)")
    finally:
        kernel32.LocalFree(merged_acl)


# 创建只约束写类访问的受限令牌，读权限和网络能力保持调用者原有边界
def _create_restricted_token(
    mode: str,
    write_sids: list[str],
    kernel32: Any,
    advapi32: Any,
) -> tuple[int, list[ctypes.c_void_p]]:
    current_token, logon_buffer = _current_token_and_logon_sid(kernel32, advapi32)
    allocated: list[ctypes.c_void_p] = []
    try:
        world = _convert_sid("S-1-1-0", advapi32)
        allocated.append(world)
        native_write_sids = [_convert_sid(value, advapi32) for value in write_sids]
        allocated.extend(native_write_sids)
        pointers = [ctypes.c_void_p(ctypes.addressof(logon_buffer)), world]
        if mode == "workspace-write":
            if not native_write_sids:
                raise OSError("workspace-write requires a capability SID")
            pointers.extend(native_write_sids)
        entries = (_SidAndAttributes * len(pointers))()
        for index, pointer in enumerate(pointers):
            entries[index].Sid = pointer
            entries[index].Attributes = 0
        restricted = wintypes.HANDLE()
        flags = _DISABLE_MAX_PRIVILEGE | _LUA_TOKEN | _WRITE_RESTRICTED
        if not advapi32.CreateRestrictedToken(
            current_token,
            flags,
            0,
            None,
            0,
            None,
            len(entries),
            entries,
            ctypes.byref(restricted),
        ):
            raise _win32_error("CreateRestrictedToken")
        restricted_value = int(restricted.value or 0)
        if restricted_value == 0:
            raise OSError("CreateRestrictedToken returned a null handle")
        default_sid = (
            native_write_sids[-1]
            if mode == "workspace-write" and native_write_sids
            else world
        )
        _set_token_default_dacl(
            restricted_value, default_sid, kernel32, advapi32
        )
        return restricted_value, allocated
    except BaseException:
        for pointer in allocated:
            kernel32.LocalFree(pointer)
        raise
    finally:
        kernel32.CloseHandle(current_token)


# 临时打开标准句柄继承位，确保受限子进程复用 daemon 已建立的匿名管道
def _inheritable_standard_handles(kernel32: Any) -> tuple[list[int], _StartupInfo]:
    handles: list[int] = []
    values: list[int] = []
    for identifier in (_STD_INPUT_HANDLE, _STD_OUTPUT_HANDLE, _STD_ERROR_HANDLE):
        handle = kernel32.GetStdHandle(identifier)
        value = int(handle or 0)
        values.append(value)
        if value and value != -1:
            if not kernel32.SetHandleInformation(
                handle, _HANDLE_FLAG_INHERIT, _HANDLE_FLAG_INHERIT
            ):
                raise _win32_error("SetHandleInformation(inherit)")
            handles.append(value)
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(_StartupInfo)
    startup.dwFlags = _STARTF_USESTDHANDLES | _STARTF_USESHOWWINDOW
    startup.wShowWindow = _SW_HIDE
    startup.hStdInput = values[0]
    startup.hStdOutput = values[1]
    startup.hStdError = values[2]
    return handles, startup


# 撤销 runner 临时设置的标准句柄继承位，避免后续无关进程持有管道
def _clear_inherit_flags(handles: list[int], kernel32: Any) -> None:
    for handle in handles:
        kernel32.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0)


# 在 Restricted Token 下创建目标进程、绑定 kill-on-close Job 并镜像其退出码
def _spawn_and_wait(
    token: int,
    argv: list[str],
    cwd: Path,
    kernel32: Any,
    advapi32: Any,
) -> int:
    inherited, startup = _inheritable_standard_handles(kernel32)
    process_info = _ProcessInformation()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    try:
        created = advapi32.CreateProcessAsUserW(
            token,
            None,
            command_line,
            None,
            None,
            True,
            0,
            None,
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        )
    finally:
        _clear_inherit_flags(inherited, kernel32)
    if not created:
        raise _win32_error("CreateProcessAsUserW")
    job = None
    try:
        kernel32.CloseHandle(process_info.hThread)
        job = create_kill_on_close_job(int(process_info.dwProcessId))
        kernel32.WaitForSingleObject(process_info.hProcess, _INFINITE)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
            raise _win32_error("GetExitCodeProcess")
        return int(exit_code.value)
    finally:
        if job is not None:
            job.close()
        kernel32.CloseHandle(process_info.hProcess)


# 规范化并校验工作区与私有临时根不重叠，防止继承 ACE 扩大临时能力
def _validate_roots(workspace: Path, temp_root: Path) -> None:
    resolved_workspace = workspace.resolve()
    resolved_temp = temp_root.resolve()
    if resolved_workspace == resolved_temp:
        raise OSError("workspace and sandbox temp root must be disjoint")
    if resolved_temp.is_relative_to(resolved_workspace):
        raise OSError("sandbox temp root must not be inside the workspace")
    if resolved_workspace.is_relative_to(resolved_temp):
        raise OSError("workspace must not contain the sandbox temp root")


# 在继承父 DACL 的前提下创建随机私有目录，避免 Python 0o700 显式 ACL 使受限令牌自锁
def _create_private_temp(temp_root: Path, *, prefix: str = "coderook-") -> Path:
    for _attempt in range(32):
        candidate = temp_root / f"{prefix}{secrets.token_hex(12)}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise OSError(f"unable to allocate a unique sandbox temp directory under {temp_root}")


# 供沙箱包装入口执行一次受限命令并管理工作区与随机临时 capability
def run_confined(
    *,
    workspace: Path,
    temp_root: Path,
    mode: str,
    argv: list[str],
) -> int:
    if mode not in {"read-only", "workspace-write"}:
        raise ValueError(f"unsupported Windows sandbox mode: {mode}")
    if not argv:
        raise ValueError("Windows sandbox command is empty")
    workspace = workspace.resolve(strict=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    _validate_roots(workspace, temp_root)
    kernel32, advapi32 = _windows_api()
    _require_acl_filesystem(workspace, kernel32)
    _require_acl_filesystem(temp_root, kernel32)
    private_temp = _create_private_temp(temp_root)
    write_sids: list[str] = []
    if mode == "workspace-write":
        workspace_sid = capability_sid(workspace, domain="workspace-write")
        temp_sid = capability_sid(private_temp, domain="private-temp")
        _grant_write(workspace, workspace_sid, kernel32, advapi32)
        _grant_write(private_temp, temp_sid, kernel32, advapi32)
        write_sids.extend((workspace_sid, temp_sid))
    token = 0
    allocated: list[ctypes.c_void_p] = []
    previous_temp = os.environ.get("TEMP")
    previous_tmp = os.environ.get("TMP")
    previous_marker = os.environ.get("CODEROOK_WINDOWS_ACL")
    previous_pythonpath = os.environ.get("PYTHONPATH")
    try:
        token, allocated = _create_restricted_token(
            mode, write_sids, kernel32, advapi32
        )
        os.environ["TEMP"] = str(private_temp)
        os.environ["TMP"] = str(private_temp)
        os.environ["CODEROOK_WINDOWS_ACL"] = "1"
        os.environ["PYTHONPATH"] = str(
            Path(__file__).with_name("windows_python_compat").resolve()
        )
        return _spawn_and_wait(token, argv, workspace, kernel32, advapi32)
    finally:
        if previous_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = previous_temp
        if previous_tmp is None:
            os.environ.pop("TMP", None)
        else:
            os.environ["TMP"] = previous_tmp
        if previous_marker is None:
            os.environ.pop("CODEROOK_WINDOWS_ACL", None)
        else:
            os.environ["CODEROOK_WINDOWS_ACL"] = previous_marker
        if previous_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous_pythonpath
        if token:
            kernel32.CloseHandle(token)
        for pointer in allocated:
            kernel32.LocalFree(pointer)
        shutil.rmtree(private_temp, ignore_errors=True)


# 在真实 Restricted Token 中验证工作区写入成功、外部写入和只读写入均被内核拒绝
def probe() -> bool:
    if os.name != "nt":
        return False
    root = _create_private_temp(
        Path(tempfile.gettempdir()), prefix="coderook-windows-acl-probe-"
    )
    try:
        workspace = root / "workspace"
        outside = root / "outside"
        temp_root = root / "private-temp-root"
        workspace.mkdir()
        outside.mkdir()
        child = (
            "from pathlib import Path; import sys, tempfile; "
            "Path(sys.argv[1]).write_text('ok'); "
            "temp=Path(tempfile.mkdtemp()); (temp/'child.txt').write_text('ok'); "
            "denied=False; "
            "\ntry: Path(sys.argv[2]).write_text('escape')"
            "\nexcept OSError: denied=True"
            "\nsys.exit(0 if denied else 41)"
        )
        writable_result = run_confined(
            workspace=workspace,
            temp_root=temp_root,
            mode="workspace-write",
            argv=[
                sys.executable,
                "-c",
                child,
                str(workspace / "inside.txt"),
                str(outside / "escape.txt"),
            ],
        )
        read_only_child = (
            "from pathlib import Path; import sys; "
            "\ntry: Path(sys.argv[1]).write_text('escape')"
            "\nexcept OSError: sys.exit(0)"
            "\nsys.exit(42)"
        )
        read_only_result = run_confined(
            workspace=workspace,
            temp_root=temp_root,
            mode="read-only",
            argv=[sys.executable, "-c", read_only_child, str(workspace / "readonly.txt")],
        )
        return writable_result == 0 and read_only_result == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


# 解析稳定 runner argv，双横线之后的参数不再由沙箱层解释
def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--temp-root")
    parser.add_argument("--mode", choices=("read-only", "workspace-write"))
    if "--" in argv:
        separator = argv.index("--")
        options = argv[:separator]
        command = argv[separator + 1 :]
    else:
        options = argv
        command = []
    parsed = parser.parse_args(options)
    parsed.command = command
    return parsed


# 运行独立 fail-closed 包装器，所有基础设施失败使用稳定前缀和退出码 127
def main(argv: list[str] | None = None) -> int:
    try:
        parsed = _parse_args(list(sys.argv[1:] if argv is None else argv))
        if parsed.probe:
            return 0 if probe() else _RUNNER_FAILURE_EXIT
        if not parsed.workspace or not parsed.temp_root or not parsed.mode:
            raise ValueError("--workspace, --temp-root and --mode are required")
        return run_confined(
            workspace=Path(parsed.workspace),
            temp_root=Path(parsed.temp_root),
            mode=str(parsed.mode),
            argv=list(parsed.command),
        )
    except BaseException as exc:
        print(f"{_RUNNER_PREFIX} {exc}", file=sys.stderr, flush=True)
        return _RUNNER_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
