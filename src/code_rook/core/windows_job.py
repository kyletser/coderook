from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


# 返回配置过 KILL_ON_JOB_CLOSE 的 Win32 Job Object 并把指定进程加入其中
def create_kill_on_close_job(pid: int) -> WindowsJobObject:
    if os.name != "nt":
        raise OSError("Windows Job Objects are unavailable on this platform")
    kernel32 = _kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise _last_os_error("CreateJobObjectW")
    job = WindowsJobObject(kernel32, handle)
    try:
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not configured:
            raise _last_os_error("SetInformationJobObject")
        process = kernel32.OpenProcess(
            _PROCESS_TERMINATE
            | _PROCESS_SET_QUOTA
            | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not process:
            raise _last_os_error("OpenProcess")
        try:
            if not kernel32.AssignProcessToJobObject(handle, process):
                raise _last_os_error("AssignProcessToJobObject")
        finally:
            kernel32.CloseHandle(process)
    except BaseException:
        job.close()
        raise
    return job


# 加载 kernel32 并声明本模块使用的 Win32 函数签名
def _kernel32() -> Any:
    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


# 把 Win32 last-error 转换成带调用点的 Python OSError
def _last_os_error(operation: str) -> OSError:
    get_last_error = getattr(ctypes, "get_last_error")
    format_error = getattr(ctypes, "FormatError")
    error = get_last_error()
    return OSError(error, f"{operation} failed: {format_error(error)}")


class WindowsJobObject:
    # 保存 Win32 Job handle；关闭 handle 会终止仍存活的整棵受管进程树
    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    # 请求内核终止 Job 内的全部进程，根进程已退出时也保持幂等
    def terminate(self, exit_code: int = 1) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, exit_code)

    # 查询 Job 内累计 CPU、峰值内存和进程数，单位转换在 Win32 边界完成
    def usage(self) -> dict[str, int]:
        if not self._handle:
            return {}
        accounting = _JobObjectBasicAccountingInformation()
        limits = _JobObjectExtendedLimitInformation()
        accounting_ok = self._kernel32.QueryInformationJobObject(
            self._handle,
            1,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        )
        limits_ok = self._kernel32.QueryInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            None,
        )
        if not accounting_ok or not limits_ok:
            return {}
        return {
            "user_cpu_ms": int(accounting.TotalUserTime // 10_000),
            "system_cpu_ms": int(accounting.TotalKernelTime // 10_000),
            "peak_memory_bytes": int(limits.PeakJobMemoryUsed),
            "process_count": int(accounting.TotalProcesses),
        }

    # 关闭 Job handle；KILL_ON_JOB_CLOSE 保证后代无法在 daemon 退出后残留
    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = 0
