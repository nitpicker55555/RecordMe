#!/usr/bin/env python3
import Cocoa
import Quartz
import time
import datetime
import json
import os
import subprocess
import gc
import weakref
# import IOKit
from AppKit import NSWorkspace, NSWorkspaceApplicationKey
import objc
import shutil
import tempfile
import sys


def _log(msg):
    """有 service.py 托着就写它的日志，否则打到 stdout（launchd 收进 /tmp/recordme.log）。"""
    for _name in ("service", "__main__"):
        svc = sys.modules.get(_name)
        # service.py 的签名：既有 log() 又有 LOG_PATH。它是以 __main__ 跑起来的。
        if svc is not None and hasattr(svc, "log") and hasattr(svc, "LOG_PATH"):
            try:
                svc.log(msg)
                return
            except Exception:
                pass
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


# AFK 判定阈值。取 180 秒和 ActivityWatch 对齐，方便两边数据互相比对。
AFK_SECONDS = 180


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


class AppSwitchMonitor:
    def __init__(self, log_file="app_switch.log"):
        self.log_file = log_file
        self.current_app = None
        self.start_time = None
        self.is_lid_closed = False
        self.is_afk = False
        self.window_title_cache = {}
        self.cache_expire_time = 5.0
        self.gc_counter = 0
        self.gc_interval = 1000
        self.log_app_start()
        self.log_location("程序启动")
        
    def get_active_app(self):
        """获取当前活跃的应用程序"""
        workspace = NSWorkspace.sharedWorkspace()
        active_app = workspace.activeApplication()
        if active_app:
            app_info = {
                'name': active_app.get('NSApplicationName', 'Unknown'),
                'bundle_id': active_app.get('NSApplicationBundleIdentifier', 'Unknown'),
                'path': active_app.get('NSApplicationPath', 'Unknown')
            }
            
            # 获取当前窗口标题
            window_title = self.get_window_title(app_info['name'])
            if window_title:
                app_info['window_title'] = window_title
                    
            return app_info
        return None
    
    def get_window_title(self, app_name):
        """通过多种方法获取应用程序的窗口标题，使用缓存优化"""
        current_time = time.time()
        cache_key = app_name
        
        # 检查缓存
        if cache_key in self.window_title_cache:
            cached_data = self.window_title_cache[cache_key]
            if current_time - cached_data['timestamp'] < self.cache_expire_time:
                return cached_data['title']
        
        # 缓存过期或不存在，重新获取
        title = self.get_window_title_applescript(app_name)
        if not title:
            title = self.get_window_title_system_events(app_name)
        
        # 更新缓存
        self.window_title_cache[cache_key] = {
            'title': title,
            'timestamp': current_time
        }
        
        # 清理过期缓存
        self.cleanup_cache(current_time)
        
        return title
    
    def get_window_title_applescript(self, app_name):
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
    
    def get_window_title_system_events(self, app_name):
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
    
    def cleanup_cache(self, current_time):
        """清理过期的缓存条目"""
        expired_keys = []
        for key, data in self.window_title_cache.items():
            if current_time - data['timestamp'] > self.cache_expire_time * 2:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.window_title_cache[key]
    
    
    def get_location(self):
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
    
    def log_location(self, event_context=""):
        """记录地理位置到日志"""
        location = self.get_location()
        if location:
            timestamp = datetime.datetime.now().isoformat()
            log_entry = {
                'timestamp': timestamp,
                'event_type': 'location_detected',
                'location': location,
                'context': event_context
            }
            
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            
            print(f"位置: {location.get('latitude', 0):.6f}, {location.get('longitude', 0):.6f}")
    
    def log_app_start(self):
        """记录程序启动事件"""
        timestamp = datetime.datetime.now().isoformat()
        log_entry = {
            'timestamp': timestamp,
            'event_type': 'monitor_started',
            'description': '监控程序已启动'
        }
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    
    def apps_equal(self, app1, app2):
        """检查两个应用是否相同（名称和窗口标题都相同）"""
        if not app1 or not app2:
            return False
        
        name_equal = app1.get('name') == app2.get('name')
        title1 = app1.get('window_title', '')
        title2 = app2.get('window_title', '')
        title_equal = title1 == title2
        
        return name_equal and title_equal
    
    def check_lid_status(self):
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
    
    def log_afk_event(self, is_afk, idle):
        """记录 AFK 状态翻转。

        只在状态变化时写一条，不是定时心跳——和别的事件保持同一种语义：
        日志里每一行都是「发生了什么」，不是「现在是什么」。
        """
        log_entry = {
            'timestamp': datetime.datetime.now().isoformat(),
            'event_type': 'afk' if is_afk else 'not-afk',
            'idle_seconds': round(idle, 1) if idle is not None else None,
        }
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

    def log_lid_event(self, is_closed):
        """记录盖子开合事件"""
        timestamp = datetime.datetime.now().isoformat()
        log_entry = {
            'timestamp': timestamp,
            'event_type': 'lid_closed' if is_closed else 'lid_opened',
            'description': '笔记本盖子关闭' if is_closed else '笔记本盖子打开'
        }
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        
        # 不打印盖子状态
    
    def log_app_switch(self, old_app, new_app, duration):
        """记录应用切换到日志文件"""
        timestamp = datetime.datetime.now().isoformat()
        log_entry = {
            'timestamp': timestamp,
            'event_type': 'app_switch',
            # 顶层的 app / title 是给读数据的人用的：不必钻进 to_app 就能分组统计。
            # 应用名和窗口标题从来是两个维度，揉成一个字符串之后就只能靠后缀猜，
            # 而后缀会骗人——标题里带 "YouTube" 的页面未必来自 YouTube。
            'app': (new_app or {}).get('name'),
            'title': (new_app or {}).get('window_title'),
            'from_app': old_app,
            'to_app': new_app,
            'duration_seconds': duration
        }
        
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        
        # 只打印应用名称
        print(f"{new_app['name']}")
    
    def monitor(self):
        """主监控循环"""
        print("开始监控应用切换和笔记本盖子状态...")
        print(f"日志将保存到: {os.path.abspath(self.log_file)}")
        print("按 Ctrl+C 停止监控\n")
        
        self.current_app = self.get_active_app()
        self.start_time = time.time()
        self.is_lid_closed = self.check_lid_status()
        
        if self.current_app:
            print(f"{self.current_app['name']}")
        
        try:
            while True:
                time.sleep(0.5)
                
                # 定期垃圾回收
                self.gc_counter += 1
                if self.gc_counter >= self.gc_interval:
                    gc.collect()
                    self.gc_counter = 0
                
                # AFK 翻转。盖子关着时不判——那时本来就没人在，
                # 再记一条 afk 是噪声。
                if not self.is_lid_closed:
                    idle = idle_seconds()
                    if idle is not None:
                        afk = idle >= AFK_SECONDS
                        if afk != self.is_afk:
                            self.log_afk_event(afk, idle)
                            self.is_afk = afk

                # 检查盖子状态
                lid_status = self.check_lid_status()
                if lid_status is not None and lid_status != self.is_lid_closed:
                    self.log_lid_event(lid_status)
                    self.is_lid_closed = lid_status
                    
                    # 如果盖子关闭，记录当前应用的使用时长
                    if lid_status and self.current_app:
                        duration = time.time() - self.start_time
                    
                    # 如果盖子打开，记录地理位置
                    if not lid_status:
                        self.log_location("笔记本盖子打开")
                
                # 只有在盖子打开时才检测应用切换
                if not self.is_lid_closed:
                    active_app = self.get_active_app()
                    # 使用自定义比较方法，避免记录相同应用和窗口标题的切换
                    if active_app and not self.apps_equal(active_app, self.current_app):
                        end_time = time.time()
                        duration = end_time - self.start_time
                        
                        self.log_app_switch(self.current_app, active_app, duration)
                        
                        self.current_app = active_app
                        self.start_time = end_time
                    
        except KeyboardInterrupt:
            print("\n\n监控已停止")

def main():
    print("提示：此程序需要辅助功能权限才能正常工作")
    print("请在 系统偏好设置 > 安全性与隐私 > 隐私 > 辅助功能 中添加此程序\n")
    
    monitor = AppSwitchMonitor()
    monitor.monitor()

if __name__ == "__main__":
    main()
