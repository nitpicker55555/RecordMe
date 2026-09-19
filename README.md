<div align="right">

**English** · [简体中文](README.zh-CN.md)

</div>

# RecordMe

A small activity recorder for **macOS and Windows**. One Python process appends a
JSON line every time something changes — the focus moves to another window, you go
idle, the lid opens or closes.

## How it works

A `0.5 s` loop watches four things and writes a line **only when one of them flips**.
The log is a stream of transitions, not a stream of samples — a row means
"something happened", never "this is the current state".

| Signal | macOS | Windows |
|---|---|---|
| Active window | `NSWorkspace` for the app, Accessibility API for the title, 5 s cache | `GetForegroundWindow` + `GetWindowTextW`; app name from the exe's `FileDescription` |
| Idle / AFK | `CGEventSourceSecondsSinceLastEventType` | `GetLastInputInfo` |
| Lid | `ioreg -k AppleClamshellState` | — not available |
| Location | `LocationHelper.app`, native CoreLocation | — not available |

Both idle sources are timers the OS already maintains, so neither platform needs
key/mouse hooks or an input-monitoring permission.

The Windows side is pure `ctypes` against Win32 — no `pywin32`, no `psutil`, nothing
to install beyond CPython itself. Platform code lives in `backend/`; `main.py` knows
nothing about either OS.

Events a platform cannot produce are simply absent from the log rather than written
as failures, so on Windows there are no `lid_*` or `location_detected` rows at all.

Two ideas are borrowed from [ActivityWatch](https://github.com/ActivityWatch/activitywatch):
**app and title are separate fields**, and **AFK is recorded explicitly**. Nothing else.

**Why app and title must be separate.** Glued into one string, the app can only be
guessed from a suffix — and suffixes lie. A page titled
`Incident 1: Google's YouTube Kids App` is not a YouTube page.

**Why AFK matters.** Without it a gap in the log is unreadable. It could mean you
walked away, the machine slept, or the recorder died — and nothing in the data tells
them apart. AFK events make the first case explicit, so the remaining gaps are
honest gaps.

### Location needs an app bundle

`LocationHelper.app` is a ~60-line Swift one-shot: it prints a coordinate and exits.
It exists as a bundle rather than a few lines inside `main.py` because macOS grants
location to the *responsible process*, and looks up `NSLocationUsageDescription` in
its `Info.plist`. A bare Python process has none, so `authorizationStatus()` stays
`notDetermined` forever and no prompt is ever shown. Injecting the keys into the main
bundle at runtime does not help either — CoreLocation wants a real signed bundle.

Three things had to be right, each of which fails as `kCLErrorDenied` or a silent
timeout, with nothing explaining why:

- Launch it with `open`. Executing the binary inside the bundle directly leaves the
  shell as the responsible process.
- Use `LSUIElement`, not `LSBackgroundOnly`. A background-only app cannot present the
  authorization prompt, so it can never be granted.
- Poll `manager.location` as well as waiting for `didUpdateLocations`. A Mac has no
  GPS and locates by scanning WiFi; the delegate may not fire for twenty seconds,
  while the cached fix is usually there immediately. Waiting only on the callback
  succeeded about one time in three.

If the helper is not built, the recorder falls back to `CoreLocationCLI` when present.

## Log format

JSON Lines, appended to `app_switch.log`. Every row has `timestamp` and `event_type`.

```jsonc
// focus moved
{"timestamp": "2026-09-19T23:10:14.080908",
 "event_type": "app_switch",
 "app": "Google Chrome",              // ← flat, for grouping
 "title": "RecordMe - Google Chrome",
 "from_app": {"name": "FleetView", "bundle_id": "ai.eigent.fleetview",
              "path": "/Applications/FleetView.app", "window_title": "main"},
 "to_app":   {"name": "Google Chrome", "bundle_id": "com.google.Chrome",
              "path": "/Applications/Google Chrome.app",
              "window_title": "RecordMe - Google Chrome"},
 "duration_seconds": 11.3}            // ← time spent in from_app

// went idle / came back
{"timestamp": "...", "event_type": "afk",     "idle_seconds": 181.4}
{"timestamp": "...", "event_type": "not-afk", "idle_seconds": 0.2}

// lid
{"timestamp": "...", "event_type": "lid_closed"}
{"timestamp": "...", "event_type": "lid_opened"}

// location, on startup and lid-open
{"timestamp": "...", "event_type": "location_detected",
 "location": {"latitude": 48.1374, "longitude": 11.5755}, "context": "笔记本盖子打开"}

// recorder started
{"timestamp": "...", "event_type": "monitor_started", "description": "监控程序已启动"}
```

`duration_seconds` belongs to the window you just **left**, not the one you entered.
An `app_switch` row closes the previous interval and opens the next.

## Run

```bash
pip install pyobjc
./build_location_helper.sh          # optional, for location
python3 main.py
```

Grant **Accessibility** to the interpreter you run it with — the permission is granted
per binary, so a venv python and the system python are two different grantees. Under
`launchd` also note that `PATH` is only `/usr/bin:/bin:/usr/sbin:/sbin`, with no
Homebrew in it; call external tools by absolute path.

## Limits

- A remote-desktop session is one window. Everything done inside it is invisible.
- About 10% of titles come back empty (lock screen, or the title can't be read).
- Location is city-level: it comes from the network, not GPS.
- No lid or location on Windows. The lid has no queryable state (only a power
  notification for the flip, and closing it suspends the process anyway); location
  needs a WinRT app identity that a plain Python script does not have, so it would
  always come back Unauthorized. Use IP geolocation instead if you need a position.
- Linux is not supported. X11 would be workable, but Wayland has no notion of an
  active window at all, and this design would not port.
