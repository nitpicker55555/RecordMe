"""按平台挑一个后端。

抽出来的四件事，每个平台各自实现：活动窗口、空闲秒数、盖子、定位。
主循环不认识平台，只认这四个函数。

导入是延迟的：Windows 上不能碰 Cocoa，macOS 上不能碰 ctypes.windll，
所以选中哪个才导哪个，import 失败不会连累另一边。
"""
import sys


def load():
    if sys.platform == "darwin":
        from . import macos
        return macos
    if sys.platform.startswith("win"):
        from . import windows
        return windows
    raise SystemExit(
        "RecordMe 目前只支持 macOS 和 Windows，当前平台是 %r。\n"
        "Linux 上 X11 能做（xdotool / python-xlib），Wayland 没有「活动窗口」"
        "这个概念，这套设计搬不过去。" % sys.platform)
