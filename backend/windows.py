"""Windows 后端。

全部走 ctypes 调 Win32 API，不装 pywin32、不装 psutil——这个工具太小，
不值得为它引依赖，而且 ctypes 是标准库，在任何 CPython 上都能直接跑。

四件事里只有三件能做：
  · 活动窗口  GetForegroundWindow + GetWindowTextW，进程名走 QueryFullProcessImageNameW
  · 空闲秒数  GetLastInputInfo，和 macOS 的 HID 空闲计时器同源，都是系统自己维护的
  · 盖子      做不了，见 lid_closed()
  · 定位      做不了，见 location()
"""
import ctypes
import ctypes.wintypes as w
import ntpath

NAME = "windows"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
version = ctypes.WinDLL("version", use_last_error=True)

user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.GetTickCount64.restype = ctypes.c_ulonglong

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000   # Vista+，比 QUERY_INFORMATION 权限低，
                                             # 够拿路径，且对多数进程不会被拒


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]


def _exe_path(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)      # 长路径上限
        size = w.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return None


def _friendly_name(path):
    """exe 的 FileDescription，也就是任务管理器里显示的那个名字。

    取它而不是 basename，是为了和 macOS 侧的 NSApplicationName 对齐：
    两边都该是 "Google Chrome"，不是一边 "Google Chrome" 一边 "chrome.exe"。
    读不到就退回文件名。
    """
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        blob = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, blob):
            return None
        # 先问这个 exe 带了哪些语言/代码页，直接写死 040904b0 在非英文版上会落空
        lang = ctypes.c_void_p()
        cb = w.UINT()
        if not version.VerQueryValueW(blob, u"\\VarFileInfo\\Translation",
                                      ctypes.byref(lang), ctypes.byref(cb)) or cb.value < 4:
            return None
        codepage = ctypes.cast(lang, ctypes.POINTER(w.WORD))
        sub = u"\\StringFileInfo\\%04x%04x\\FileDescription" % (codepage[0], codepage[1])
        val = ctypes.c_wchar_p()
        n = w.UINT()
        if version.VerQueryValueW(blob, sub, ctypes.byref(val), ctypes.byref(n)) and n.value:
            desc = (val.value or "").strip()
            if desc:
                return desc
    except Exception:
        pass
    return None


def active_window():
    """{name, bundle_id, path, window_title}，字段与 macOS 后端对齐。

    bundle_id 在 Windows 上没有对应物，填 exe 文件名——它是这边最接近
    「稳定的应用标识」的东西，至少比显示名更不容易随语言变。
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None                                   # 锁屏、切换桌面时会是 0

    n = user32.GetWindowTextLengthW(hwnd)
    title = ""
    if n:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value

    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    path = _exe_path(pid.value) if pid.value else None
    # 显式用 ntpath 而不是 os.path：在 Windows 上两者等价，但写死 ntpath
    # 才能在别的平台上被 mock 测试覆盖到
    exe = ntpath.basename(path) if path else None
    name = (_friendly_name(path) if path else None) or exe or "Unknown"

    info = {"name": name, "bundle_id": exe or "Unknown", "path": path or "Unknown"}
    if title:
        info["window_title"] = title
    return info


def idle_seconds():
    """距上次键鼠输入多久。GetLastInputInfo 是系统维护的，不需要装钩子。"""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    # dwTime 是 32 位、约 49.7 天回绕一次；GetTickCount64 不回绕。
    # 截到 32 位再相减，跨回绕时补一个周期，否则会算出负数或者天文数字。
    now32 = kernel32.GetTickCount64() & 0xFFFFFFFF
    delta = now32 - lii.dwTime
    if delta < 0:
        delta += 1 << 32
    return delta / 1000.0


def lid_closed():
    """Windows 没有可靠的盖子状态查询。

    GUID_LIDSWITCH_STATE_CHANGE 只能注册电源通知拿「翻转事件」，查不到当前值；
    而且合盖默认触发睡眠，进程本来就停了。与其猜，不如明说不知道——
    返回 None，主循环会跳过盖子这一路，日志里干脆不出现 lid 事件。
    """
    return None


def location():
    """Windows 上不采集定位。

    Win32 有 Windows.Devices.Geolocation（WinRT），但要走 WinRT 投影、要用户在
    「设置 → 隐私 → 位置」里为这个 app 单独开，而桌面 Python 脚本没有 app 身份，
    拿不到那个开关。硬上的结果是永远 Unauthorized——和 macOS 上裸 Python 进程
    调 CoreLocation 的处境一样。所以这边直接不做，日志里不会有 location 事件。

    需要位置的话，用 IP 归属地代替（精度只到城市，但不需要任何授权）。
    """
    return None


def startup_note():
    return ("Windows 后端：窗口标题和空闲检测开箱即用，不需要任何权限。\n"
            "盖子和定位在这个平台上不采集，日志里不会有对应事件。")
