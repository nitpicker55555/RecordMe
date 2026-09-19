"""macOS 后端。

这里的每一处实现都是踩过坑之后的样子，原样从 main.py 搬过来，逻辑未改：
  · 窗口标题走 AppleScript，浏览器要特判——问 Chrome 要 front window 的名字
    拿到的是窗口标题，问 active tab 才是当前标签页，后者才是我们要的
  · 定位必须经由 LocationHelper.app，裸 Python 进程拿不到 CoreLocation 授权
  · 外部命令一律解析绝对路径，LaunchAgent 的 PATH 里没有 Homebrew
"""
import datetime
import os
import subprocess
import shutil
import sys
import tempfile
import time

import Quartz
from AppKit import NSWorkspace

NAME = "macos"

# 这个模块被 main.py 导入，_log 也定义在 main.py 里——延迟取，避免循环导入
def _log(msg):
    m = sys.modules.get("__main__")
    fn = getattr(m, "_log", None)
    if callable(fn):
        fn(msg)
    else:
        print("%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def _corelocationcli():
    """CoreLocationCLI 在哪。LaunchAgent 的 PATH 里没有 Homebrew，裸名字找不到。"""
    hit = shutil.which("CoreLocationCLI")
    if hit:
        return hit
    for cand in ("/opt/homebrew/bin/CoreLocationCLI", "/usr/local/bin/CoreLocationCLI"):
        if os.access(cand, os.X_OK):
            return cand
    return None


_title_cache = {}
_CACHE_TTL = 5.0


def active_window():
    """{name, bundle_id, path, window_title}"""
    active = NSWorkspace.sharedWorkspace().activeApplication()
    if not active:
        return None
    info = {
        'name': active.get('NSApplicationName', 'Unknown'),
        'bundle_id': active.get('NSApplicationBundleIdentifier', 'Unknown'),
        'path': active.get('NSApplicationPath', 'Unknown'),
    }
    title = _cached_title(info['name'])
    if title:
        info['window_title'] = title
    return info


def _cached_title(app_name):
    """标题查询要起 osascript 子进程，0.5 秒一次太贵，缓存 5 秒。"""
    now = time.time()
    hit = _title_cache.get(app_name)
    if hit and now - hit['timestamp'] < _CACHE_TTL:
        return hit['title']
    title = get_window_title_applescript(app_name) or get_window_title_system_events(app_name)
    _title_cache[app_name] = {'title': title, 'timestamp': now}
    for k in [k for k, v in _title_cache.items() if now - v['timestamp'] > _CACHE_TTL * 2]:
        del _title_cache[k]
    return title


def idle_seconds():
    """距上次键鼠输入过了多久。

    走 CGEventSourceSecondsSinceLastEventType，是系统自己维护的 HID 空闲计时器，
    不需要监听键鼠事件本身——所以不要求输入监控权限，也没有轮询开销。
    """
    try:
        return Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState,
            Quartz.kCGAnyInputEventType)
    except Exception:
        return None


def startup_note():
    return ("macOS 后端：需要「辅助功能」权限才能读到窗口标题。\n"
            "系统设置 → 隐私与安全性 → 辅助功能，把运行本程序的解释器打上勾。")


def get_window_title_applescript(app_name):
    """通过 AppleScript 获取应用程序的窗口标题"""
    try:
        # Chrome 特殊处理
        if 'Chrome' in app_name:
            script = '''
            tell application "Google Chrome"
                if (count of windows) > 0 then
                    return title of active tab of front window
                end if
            end tell
            '''
        # Safari 处理
        elif 'Safari' in app_name:
            script = '''
            tell application "Safari"
                if (count of windows) > 0 then
                    return name of current tab of front window
                end if
            end tell
            '''
        # Firefox 处理
        elif 'Firefox' in app_name:
            script = '''
            tell application "Firefox"
                if (count of windows) > 0 then
                    return name of front window
                end if
            end tell
            '''
        # 通用窗口标题获取
        else:
            script = f'''
            tell application "{app_name}"
                if (count of windows) > 0 then
                    return name of front window
                end if
            end tell
            '''
        
        # 使用 Popen 和 communicate 更好地管理资源
        with subprocess.Popen(['osascript', '-e', script], 
                             stdout=subprocess.PIPE, 
                             stderr=subprocess.PIPE, 
                             text=True) as proc:
            stdout, stderr = proc.communicate(timeout=2)
            if proc.returncode == 0 and stdout.strip():
                return stdout.strip()
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass
    return None

def get_window_title_system_events(app_name):
    """通过 System Events 获取窗口标题"""
    try:
        script = f'''
        tell application "System Events"
            tell process "{app_name}"
                if (count of windows) > 0 then
                    return title of front window
                end if
            end tell
        end tell
        '''
        # 使用 Popen 和 communicate 更好地管理资源
        with subprocess.Popen(['osascript', '-e', script], 
                             stdout=subprocess.PIPE, 
                             stderr=subprocess.PIPE, 
                             text=True) as proc:
            stdout, stderr = proc.communicate(timeout=2)
            if proc.returncode == 0 and stdout.strip():
                return stdout.strip()
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass
    return None

def lid_closed():
    """检查笔记本盖子是否关闭"""
    try:
        # 使用 ioreg 命令检查 clamshell 状态
        with subprocess.Popen(['ioreg', '-r', '-k', 'AppleClamshellState', '-d', '4'], 
                             stdout=subprocess.PIPE, 
                             stderr=subprocess.PIPE, 
                             text=True) as proc:
            stdout, stderr = proc.communicate(timeout=5)
            if proc.returncode == 0:
                # 查找 AppleClamshellState
                if '"AppleClamshellState" = Yes' in stdout:
                    return True
                elif '"AppleClamshellState" = No' in stdout:
                    return False
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass
    return None

def location():
    """当前坐标。优先用随仓库带的 LocationHelper.app（原生 CoreLocation）。

    为什么是个 .app 而不是直接在这个进程里调 CoreLocation：系统查的是「责任
    进程」的 Info.plist 有没有 NSLocationUsageDescription。裸 Python 进程没有，
    于是 authorizationStatus() 永远停在 notDetermined、location() 永远是 nil，
    而且连授权框都不会弹。实测过运行时往 main bundle 注入 CFBundleIdentifier +
    NSLocationUsageDescription，同样无效——CoreLocation 认的是真正签名的 bundle。

    另外两个踩过的坑，都写在 Info.plist 和启动方式里了：
      · 必须用 `open` 拉起。直接执行 .app 里的二进制，责任进程还是 shell，
        CoreLocation 返回 kCLErrorDenied。
      · Info.plist 里不能写 LSBackgroundOnly。纯后台 app 出不了 UI，也就拿不到
        授权，同样是 kCLErrorDenied。要用 LSUIElement：能弹框，但不占 Dock。
    """
    app = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "LocationHelper.app")
    if os.path.isdir(app):
        out = os.path.join(tempfile.gettempdir(),
                           "recordme-loc-%d.txt" % os.getpid())
        try:
            if os.path.exists(out):
                os.remove(out)
            # -n 强制起新实例：不加的话，上一次的助手还没退干净时，open 只会去
            #    激活那个旧实例，新的 --args 根本传不进去，于是白等到超时。
            subprocess.run(["/usr/bin/open", "-n", "-a", app, "--args", out],
                           timeout=10, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # 比助手自己的 20 秒超时多等一点，否则等待方先放弃、助手白跑一趟
            for _ in range(240):                      # 最多等 24 秒
                if os.path.exists(out):
                    break
                time.sleep(0.1)
            else:
                _log("定位：LocationHelper 24 秒没写出结果，转兜底")
                raise TimeoutError("helper silent")
            with open(out, encoding="utf-8") as f:
                txt = f.read().strip()
            os.remove(out)
            if txt.startswith("ERR"):
                # 冷启动时 locationd 常要十几秒才出第一个 fix，别当致命错误。
                _log("定位：LocationHelper 说 %s，转兜底" % txt[4:])
                raise ValueError(txt)
            lat, lon = txt.split()
            return {"latitude": float(lat), "longitude": float(lon)}
        except (subprocess.TimeoutExpired, TimeoutError):
            pass                                  # 已经记过日志了
        except ValueError:
            pass                                  # 同上
        except (subprocess.SubprocessError, OSError) as e:
            _log("定位：LocationHelper 起不来 %s: %s" % (type(e).__name__, e))

    # 兜底：Homebrew 的 CoreLocationCLI，装了才有。
    # 注意 LaunchAgent 的 PATH 里没有 /opt/homebrew/bin，必须自己解析绝对路径。
    cli = _corelocationcli()
    if not cli:
        _log("定位：没有 LocationHelper.app，也找不到 CoreLocationCLI")
        return None
    try:
        with subprocess.Popen([cli, "-once"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as proc:
            out_s, err_s = proc.communicate(timeout=10)
            if proc.returncode != 0 or not out_s.strip():
                _log("定位：CoreLocationCLI 退出码 %s stderr=%r"
                     % (proc.returncode, (err_s or "").strip()[:200]))
                return None
            parts = out_s.split()
            return {"latitude": float(parts[0]), "longitude": float(parts[1])}
    except subprocess.TimeoutExpired:
        _log("定位：CoreLocationCLI 10 秒没返回")
    except (subprocess.SubprocessError, OSError, ValueError, IndexError) as e:
        _log("定位：CoreLocationCLI 失败 %s: %s" % (type(e).__name__, e))
    return None

