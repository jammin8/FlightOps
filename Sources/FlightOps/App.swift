import AppKit
import ServiceManagement
import SwiftUI


enum LaunchOptions {

    static var snapshotDir: String? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count else { return nil }
        return args[i + 1]
    }
}

@main
struct FlightOpsApp: App {

    @StateObject private var m = Monitor(mode: LaunchOptions.snapshotDir == nil ? .live : .inert)

    init() {
        if let dir = LaunchOptions.snapshotDir {
            Snapshot.run(into: dir)
            return
        }

        PanelCapture.install()
    }

    var body: some Scene {
        MenuBarExtra {
            Panel(m: m)
        } label: {


            Image(nsImage: MenuBarIcon.image(agents: m.agentsWorking, ci: m.ciRunning, offline: m.rigOffline,
                                             symbol: m.verdict.menuSymbol, notice: m.verdict.menuNotice))
        }
        .menuBarExtraStyle(.window)

        Settings { SettingsView(m: m) }
    }
}







@MainActor enum MenuBarIcon {
    static func image(agents: Int?, ci: Int?, offline: Bool, symbol: String?, notice: Bool) -> NSImage {
        let agentsText: String = agents.map { String($0) } ?? "–"
        let ciText: String = ci.map { String($0) } ?? "–"
        let content = HStack(spacing: 3) {
            if !offline {
                Image(nsImage: JetIcon.image).resizable().frame(width: 16, height: 16)
                Text(agentsText)
                Color.clear.frame(width: 5, height: 1)
                Image(systemName: "testtube.2").font(.system(size: 13, weight: .medium))
                Text(ciText)
                if let symbol {
                    Color.clear.frame(width: 3, height: 1)
                    Image(systemName: symbol).font(.system(size: 12, weight: .medium))
                }
                if notice { Circle().frame(width: 4, height: 4) }
            } else {
                Image(systemName: "exclamationmark.triangle.fill")
                Text("Rig offline")
            }
        }
        .font(.system(size: 13, weight: .medium))
        .monospacedDigit()
        .foregroundStyle(.black)
        .padding(.vertical, 1)
        let r = ImageRenderer(content: content)
        r.scale = NSScreen.main?.backingScaleFactor ?? 2
        let img = r.nsImage ?? NSImage(size: NSSize(width: 18, height: 18))
        img.isTemplate = true
        return img
    }
}




@MainActor enum PanelCapture {
    static func install() {
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main) { note in
            guard UserDefaults.standard.bool(forKey: "captureNextOpen"), let win = note.object as? NSWindow else { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                guard let frameView = win.contentView?.superview ?? win.contentView,
                      let rep = frameView.bitmapImageRepForCachingDisplay(in: frameView.bounds) else { return }
                frameView.cacheDisplay(in: frameView.bounds, to: rep)
                let tab = UserDefaults.standard.integer(forKey: "tab")
                try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "/tmp/flightops-panel-\(tab).png"))
                try? "\(win.className) frame \(win.frame) content \(frameView.bounds)".write(toFile: "/tmp/flightops-panel-\(tab).txt", atomically: true, encoding: .utf8)
            }
        }
    }
}



enum JetIcon {
    private static let half: [(CGFloat, CGFloat)] = [
        (0.000, 0.000), (0.035, 0.080), (0.070, 0.190), (0.110, 0.300), (0.150, 0.400),
        (0.165, 0.450), (0.480, 0.660), (0.480, 0.735), (0.215, 0.755),
        (0.190, 0.800), (0.370, 0.905), (0.370, 0.975), (0.130, 0.965),
        (0.085, 1.010), (0.000, 1.030),
    ]

    static let image: NSImage = {
        let side: CGFloat = 18
        let img = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            let t = NSAffineTransform()
            t.translateX(by: side / 2, yBy: side / 2)
            t.rotate(byDegrees: -40)
            t.concat()
            let h = side * 1.02, w = h * 0.66
            let r = NSRect(x: -w / 2, y: -h / 2, width: w, height: h)
            func pt(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: r.midX + x * r.width, y: r.maxY - y * r.height / 1.03) }
            let p = NSBezierPath()
            p.move(to: pt(half[0].0, half[0].1))
            for (x, y) in half.dropFirst() { p.line(to: pt(x, y)) }
            for (x, y) in half.reversed().dropFirst() { p.line(to: pt(-x, y)) }
            p.close()

            let cw = r.width * 0.11, ch = r.height * 0.19
            p.append(NSBezierPath(ovalIn: NSRect(x: r.midX - cw / 2, y: r.maxY - r.height * 0.38, width: cw, height: ch)))
            p.windingRule = .evenOdd
            NSColor.black.setFill()
            p.fill()
            return true
        }
        img.isTemplate = true
        return img
    }()
}
