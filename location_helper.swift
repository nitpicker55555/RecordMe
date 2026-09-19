// 取一次当前坐标，打到 stdout，然后退出。
//
// 为什么是 .app 而不是一个命令行程序：CoreLocation 要查「责任进程」的 Info.plist
// 里有没有 NSLocationUsageDescription。命令行程序的责任进程是拉起它的终端或
// python，那边没有这个键 —— 系统既不授权也不弹框，authorizationStatus() 永远
// 停在 notDetermined，location() 永远是 nil。实测过运行时往 main bundle 注入
// CFBundleIdentifier + NSLocationUsageDescription，同样无效：CoreLocation 认的是
// 真正签名的 bundle。套上 .app 外壳，它才是自己的责任进程。
//
// 用法：open -a LocationHelper.app --args /tmp/xxx.txt
// 结果写进 argv[1] 指定的文件（成功写 "lat lon"，失败写 "ERR <原因>"）。
// 不走 stdout 是因为必须由 `open` 拉起——直接执行二进制的话责任进程还是 shell，
// CoreLocation 直接返回 kCLErrorDenied；而 `open` 不会把子进程的 stdout 传回来。

import Foundation
import CoreLocation

final class OneShot: NSObject, CLLocationManagerDelegate {
    private let mgr = CLLocationManager()
    private var done = false

    private func write(_ s: String) {
        guard CommandLine.arguments.count > 1 else { print(s); return }
        try? s.write(toFile: CommandLine.arguments[1], atomically: true, encoding: .utf8)
    }

    func run(timeout: TimeInterval) -> Never {
        mgr.delegate = self
        mgr.desiredAccuracy = kCLLocationAccuracyHundredMeters
        mgr.requestWhenInUseAuthorization()
        mgr.startUpdatingLocation()

        // 等待期间同时盯两条路：
        //   · delegate 回调 didUpdateLocations —— 新的 fix
        //   · mgr.location 这个属性 —— locationd 缓存的最后已知位置
        // 只靠回调不可靠：Mac 没有 GPS，要靠扫 WiFi 定位，冷的时候二十秒都未必
        // 回调一次；而缓存往往当场就有。实测只等回调时成功率约 1/3。
        let deadline = Date().addingTimeInterval(timeout)
        while !done && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
            if !done, let c = mgr.location?.coordinate {
                done = true
                write(String(format: "%.6f %.6f", c.latitude, c.longitude))
            }
        }
        if !done {
            write("ERR timeout \(Int(timeout))s authStatus=\(mgr.authorizationStatus.rawValue)")
            exit(1)
        }
        exit(0)
    }

    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let c = locs.last?.coordinate, !done else { return }
        done = true
        write(String(format: "%.6f %.6f", c.latitude, c.longitude))
    }

    func locationManager(_ m: CLLocationManager, didFailWithError e: Error) {
        write("ERR \(e.localizedDescription) authStatus=\(m.authorizationStatus.rawValue)")
        done = true
        exit(1)
    }
}

// 存成全局常量而不是临时对象：CLLocationManager.delegate 是 weak 引用，
// 临时对象会被立刻释放，delegate 变 nil，回调永远不来。
let shot = OneShot()
shot.run(timeout: 20)
