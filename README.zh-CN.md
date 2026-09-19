<div align="right">

[English](README.md) · **简体中文**

</div>

# RecordMe

一个很小的 macOS 活动记录器。单个 Python 进程，每当发生变化就往日志追加一行 JSON
—— 焦点切到别的窗口、人离开了、盖子开合。

## 原理

`0.5` 秒一轮，盯四件事，**只在翻转时写一行**。日志是状态变化流，不是采样流 ——
每一行的含义是「发生了什么」，不是「现在是什么」。

| 信号 | 怎么拿到的 |
|---|---|
| 活跃窗口 | 应用走 `NSWorkspace`，标题走无障碍 API，带 5 秒缓存 |
| 空闲 / AFK | `CGEventSourceSecondsSinceLastEventType` —— 系统自己维护的 HID 空闲计时器，不用挂键鼠钩子，也不需要额外权限 |
| 盖子 | `ioreg -k AppleClamshellState` |
| 位置 | `LocationHelper.app` —— 原生 CoreLocation，启动时和开盖时各取一次 |

两点借鉴自 [ActivityWatch](https://github.com/ActivityWatch/activitywatch)：
**app 和 title 分开成两个字段**，以及**显式记录 AFK**。其余没有。

**为什么 app 和 title 必须分开。** 揉成一个字符串后，应用名只能靠后缀猜，而后缀
会骗人 —— 标题叫 `Incident 1: Google's YouTube Kids App` 的页面并不来自 YouTube。

**为什么 AFK 要紧。** 没有它，日志里的空白无法解读 —— 可能是人走开、机器睡了、
或者记录器挂了，而数据本身区分不了这三种。有了 AFK 事件，第一种情况被显式标出，
剩下的空白才是诚实的空白。

### 位置为什么得套一个 app

`LocationHelper.app` 是个六十行的 Swift 一次性程序：打一个坐标出来然后退出。
它之所以是个 bundle 而不是 `main.py` 里的几行代码，是因为 macOS 把定位权限授予
「责任进程」，并且要查它 `Info.plist` 里的 `NSLocationUsageDescription`。裸 Python
进程没有这个键，于是 `authorizationStatus()` 永远停在 `notDetermined`，连授权框都
不会弹。运行时往 main bundle 注入这些键也没用 —— CoreLocation 认的是真正签名的 bundle。

有三处必须同时做对，每一处做错的表现都是 `kCLErrorDenied` 或者静默超时，
而系统不会告诉你原因：

- **用 `open` 拉起。** 直接执行 bundle 里的二进制，责任进程仍然是 shell。
- **用 `LSUIElement`，不能用 `LSBackgroundOnly`。** 纯后台 app 出不了授权框，
  也就永远拿不到授权。
- **除了等 `didUpdateLocations`，还要轮询 `manager.location`。** Mac 没有 GPS，
  靠扫 WiFi 定位，delegate 可能二十秒都不回调一次，而缓存的位置往往当场就有。
  只等回调的成功率大约三分之一。

没编助手的话，记录器会退回到 `CoreLocationCLI`（如果装了）。

## 日志结构

JSON Lines，追加写入 `app_switch.log`。每行都有 `timestamp` 和 `event_type`。

```jsonc
// 焦点切换
{"timestamp": "2026-09-19T23:10:14.080908",
 "event_type": "app_switch",
 "app": "Google Chrome",              // ← 顶层，方便分组统计
 "title": "RecordMe - Google Chrome",
 "from_app": {"name": "FleetView", "bundle_id": "ai.eigent.fleetview",
              "path": "/Applications/FleetView.app", "window_title": "main"},
 "to_app":   {"name": "Google Chrome", "bundle_id": "com.google.Chrome",
              "path": "/Applications/Google Chrome.app",
              "window_title": "RecordMe - Google Chrome"},
 "duration_seconds": 11.3}            // ← 在上一个窗口停留的时长

// 离开与回来
{"timestamp": "...", "event_type": "afk",     "idle_seconds": 181.4}
{"timestamp": "...", "event_type": "not-afk", "idle_seconds": 0.2}

// 盖子
{"timestamp": "...", "event_type": "lid_closed"}
{"timestamp": "...", "event_type": "lid_opened"}

// 位置，启动和开盖时各取一次
{"timestamp": "...", "event_type": "location_detected",
 "location": {"latitude": 48.1374, "longitude": 11.5755}, "context": "笔记本盖子打开"}

// 记录器启动
{"timestamp": "...", "event_type": "monitor_started", "description": "监控程序已启动"}
```

`duration_seconds` 属于你刚**离开**的那个窗口，不是刚进入的。一条 `app_switch`
既是上一段的结束，也是下一段的开始。

## 跑起来

```bash
pip install pyobjc
./build_location_helper.sh          # 位置功能才需要
python3 main.py
```

给你实际用的那个解释器开**辅助功能**权限 —— 权限按二进制授予，venv 的 python 和
系统 python 是两个不同的授予对象。跑在 `launchd` 下还要注意 `PATH` 只有
`/usr/bin:/bin:/usr/sbin:/sbin`，没有 Homebrew，调外部命令要写绝对路径。

## 限制

- 远程桌面会话只是一个窗口，里面做的一切都看不见。
- 约 10% 的标题是空字符串（锁屏，或者读不到标题）。
- 位置是城市级的，来自网络而非 GPS。
- 仅 macOS。Wayland 根本没有「活跃窗口」这个概念，这套设计过不去。
