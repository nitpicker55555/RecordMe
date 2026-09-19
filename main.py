#!/usr/bin/env python3
"""RecordMe：一个小小的活动记录器。

0.5 秒一轮，盯四件事，**只在某件事翻转时**写一行 JSON。日志是一串「发生了什么」，
不是一串「现在是什么」——所以两行之间的间隔就是在上一个窗口停留的时长。

平台相关的部分全在 backend/ 里，这个文件不认识任何一个平台。
"""
import datetime
import gc
import json
import os
import sys
import time

import backend

_B = backend.load()

# AFK 判定阈值。取 180 秒和 ActivityWatch 对齐，方便两边数据互相比对。
AFK_SECONDS = 180


def _log(msg):
    """有 service.py 托着就写它的日志，否则打到 stdout。"""
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


class AppSwitchMonitor:
    def __init__(self, log_file="app_switch.log"):
        self.log_file = log_file
        self.current_app = None
        self.start_time = None
        self.is_lid_closed = False
        self.is_afk = False
        self.gc_counter = 0
        self.gc_interval = 1000
        self.log_app_start()
        self.log_location("程序启动")

    # ---------- 写日志 ----------

    def _write(self, entry):
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def log_app_start(self):
        self._write({'timestamp': datetime.datetime.now().isoformat(),
                     'event_type': 'monitor_started',
                     'platform': _B.NAME,
                     'description': '监控程序已启动'})

    def log_location(self, event_context=""):
        """位置。后端说不支持就安静跳过——Windows 上每次都记一条失败是噪声。"""
        loc = _B.location()
        if not loc:
            return
        self._write({'timestamp': datetime.datetime.now().isoformat(),
                     'event_type': 'location_detected',
                     'location': loc, 'context': event_context})
        print("位置: %.6f, %.6f" % (loc.get('latitude', 0), loc.get('longitude', 0)))

    def log_afk_event(self, is_afk, idle):
        """记录 AFK 状态翻转。

        只在状态变化时写一条，不是定时心跳——和别的事件保持同一种语义：
        日志里每一行都是「发生了什么」，不是「现在是什么」。
        """
        self._write({'timestamp': datetime.datetime.now().isoformat(),
                     'event_type': 'afk' if is_afk else 'not-afk',
                     'idle_seconds': round(idle, 1) if idle is not None else None})

    def log_lid_event(self, is_closed):
        self._write({'timestamp': datetime.datetime.now().isoformat(),
                     'event_type': 'lid_closed' if is_closed else 'lid_opened',
                     'description': '笔记本盖子关闭' if is_closed else '笔记本盖子打开'})

    def log_app_switch(self, old_app, new_app, duration):
        self._write({
            'timestamp': datetime.datetime.now().isoformat(),
            'event_type': 'app_switch',
            # 顶层的 app / title 是给读数据的人用的：不必钻进 to_app 就能分组统计。
            # 应用名和窗口标题从来是两个维度，揉成一个字符串之后就只能靠后缀猜，
            # 而后缀会骗人——标题里带 "YouTube" 的页面未必来自 YouTube。
            'app': (new_app or {}).get('name'),
            'title': (new_app or {}).get('window_title'),
            'from_app': old_app,
            'to_app': new_app,
            'duration_seconds': duration})
        print(new_app['name'])

    # ---------- 主循环 ----------

    @staticmethod
    def apps_equal(a, b):
        """应用名和窗口标题都相同才算没变——切标签页也是一次切换。"""
        if not a or not b:
            return False
        return (a.get('name') == b.get('name')
                and a.get('window_title', '') == b.get('window_title', ''))

    def monitor(self):
        print("开始监控（后端：%s）" % _B.NAME)
        print("日志将保存到: %s" % os.path.abspath(self.log_file))
        print("按 Ctrl+C 停止监控\n")

        self.current_app = _B.active_window()
        self.start_time = time.time()
        self.is_lid_closed = bool(_B.lid_closed())

        if self.current_app:
            print(self.current_app['name'])

        try:
            while True:
                time.sleep(0.5)

                self.gc_counter += 1
                if self.gc_counter >= self.gc_interval:
                    gc.collect()
                    self.gc_counter = 0

                # AFK 翻转。盖子关着时不判——那时本来就没人在，再记一条 afk 是噪声。
                if not self.is_lid_closed:
                    idle = _B.idle_seconds()
                    if idle is not None:
                        afk = idle >= AFK_SECONDS
                        if afk != self.is_afk:
                            self.log_afk_event(afk, idle)
                            self.is_afk = afk

                # 盖子。后端返回 None 表示这个平台查不了，整条线跳过。
                lid = _B.lid_closed()
                if lid is not None and lid != self.is_lid_closed:
                    self.log_lid_event(lid)
                    self.is_lid_closed = lid
                    if not lid:
                        self.log_location("笔记本盖子打开")

                if not self.is_lid_closed:
                    active = _B.active_window()
                    if active and not self.apps_equal(active, self.current_app):
                        now = time.time()
                        self.log_app_switch(self.current_app, active, now - self.start_time)
                        self.current_app = active
                        self.start_time = now

        except KeyboardInterrupt:
            print("\n\n监控已停止")


def main():
    print(_B.startup_note())
    print()
    AppSwitchMonitor().monitor()


if __name__ == "__main__":
    main()
