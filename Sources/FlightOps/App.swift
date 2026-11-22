import AppKit
import SwiftUI

enum LaunchOptions {
    static func value(_ flag: String) -> String? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
        return args[i + 1]
    }

    static var snapshotDir: String? { value("--snapshot") }
    static var statusFile: String? { value("--status") }
}

@main
struct FlightOpsApp: App {
    @StateObject private var m = Monitor(mode: LaunchOptions.snapshotDir == nil ? .live : .fixed(URL(fileURLWithPath: LaunchOptions.statusFile ?? Paths.sample.path)))

    init() {
        if let dir = LaunchOptions.snapshotDir {
            Snapshot.run(into: dir, status: URL(fileURLWithPath: LaunchOptions.statusFile ?? Paths.sample.path))
        }
    }

    var body: some Scene {
        MenuBarExtra {
            Panel(m: m)
        } label: {
            Image(nsImage: MenuBarIcon.image(open: m.status?.views["all"]?.counts.open, needsYou: (m.status?.views["all"]?.counts.needsYou ?? 0) > 0 && !m.demo,
                                             stuck: m.status?.views["all"]?.stages.contains { $0.verdict?.state == "stuck" } ?? false))
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor enum MenuBarIcon {
    static func image(open: Int?, needsYou: Bool, stuck: Bool) -> NSImage {
        let content = HStack(spacing: 3) {
            Image(nsImage: JetIcon.image).resizable().frame(width: 16, height: 16)
            Text(open.map(String.init) ?? "–")
            if stuck { Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 11)) }
            if needsYou { Circle().frame(width: 5, height: 5) }
        }
        .font(.system(size: 13, weight: .medium))
        .monospacedDigit()
        .foregroundStyle(.black)
        .padding(.vertical, 1)
        let renderer = ImageRenderer(content: content)
        renderer.scale = NSScreen.main?.backingScaleFactor ?? 2
        let image = renderer.nsImage ?? NSImage(size: NSSize(width: 18, height: 18))
        image.isTemplate = true
        return image
    }
}

@MainActor enum Snapshot {
    static func run(into dir: String, status: URL) {
        Task { @MainActor in
            let suite = "io.github.jammin8.flightops.snapshot"
            let store = UserDefaults(suiteName: suite) ?? UserDefaults()
            let m = Monitor(mode: .fixed(status))
            guard m.status != nil else {
                FileHandle.standardError.write(Data("flightops: unreadable status file \(status.path)\n".utf8))
                exit(2)
            }
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
                for tab in Tab.allCases {
                    store.set(tab.rawValue, forKey: "tab")
                    store.set("all", forKey: "repo")
                    let host = NSHostingView(rootView: Panel(m: m).defaultAppStorage(store).background(Color(nsColor: .windowBackgroundColor)))
                    let window = NSWindow(contentRect: NSRect(origin: .zero, size: host.fittingSize), styleMask: [.borderless], backing: .buffered, defer: false)
                    window.appearance = NSAppearance(named: appearance)
                    window.contentView = host
                    window.orderBack(nil)
                    window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
                    try? await Task.sleep(for: .milliseconds(700))
                    window.setContentSize(host.fittingSize)
                    host.layoutSubtreeIfNeeded()
                    try? await Task.sleep(for: .milliseconds(300))
                    if let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                        host.cacheDisplay(in: host.bounds, to: rep)
                        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name)-\(tab.title.lowercased()).png"))
                    }
                    window.orderOut(nil)
                }
            }
            store.removePersistentDomain(forName: suite)
            exit(0)
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
