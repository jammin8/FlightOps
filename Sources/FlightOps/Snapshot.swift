import AppKit
import SwiftUI





@MainActor enum Snapshot {
    static func run(into dir: String) {
        Task { @MainActor in
            let suite = "io.github.jammin8.flightops.snapshot"
            let store = UserDefaults(suiteName: suite) ?? UserDefaults()
            for key in ["openCards", "expandedInitiatives"] {
                store.set(UserDefaults.standard.object(forKey: key), forKey: key)
            }
            let m = Monitor(mode: .snapshot)
            for _ in 0..<80 where SourceID.allCases.contains(where: { m.status[$0]?.sampleAt == nil && (m.status[$0]?.failures ?? 0) == 0 }) {
                try? await Task.sleep(for: .milliseconds(500))
            }
            m.recompute()
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
            for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
                for tab in Tab.allCases.map(\.rawValue).sorted() {
                    store.set(tab, forKey: "tab")
                    let host = NSHostingView(rootView: Panel(m: m).defaultAppStorage(store).background(Color(nsColor: .windowBackgroundColor)))
                    let size = host.fittingSize
                    let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
                    window.appearance = NSAppearance(named: appearance)
                    window.backgroundColor = appearance == .darkAqua ? NSColor(white: 0.17, alpha: 1) : NSColor(white: 0.94, alpha: 1)
                    window.contentView = host
                    window.orderBack(nil)
                    window.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
                    try? await Task.sleep(for: .milliseconds(700))
                    window.setContentSize(host.fittingSize)
                    host.layoutSubtreeIfNeeded()
                    try? await Task.sleep(for: .milliseconds(300))
                    if let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                        host.cacheDisplay(in: host.bounds, to: rep)
                        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name)-\(tab).png"))
                    }
                    window.orderOut(nil)
                }

                let page = NSHostingView(rootView: ModelsTab(m: m).padding(16).frame(width: 520).defaultAppStorage(store)
                    .background(Color(nsColor: .windowBackgroundColor)))
                let pw = NSWindow(contentRect: NSRect(origin: .zero, size: page.fittingSize), styleMask: [.borderless], backing: .buffered, defer: false)
                pw.appearance = NSAppearance(named: appearance)
                pw.contentView = page
                pw.orderBack(nil)
                pw.setFrameOrigin(NSPoint(x: -10_000, y: -10_000))
                try? await Task.sleep(for: .milliseconds(700))
                pw.setContentSize(page.fittingSize)
                page.layoutSubtreeIfNeeded()
                if let rep = page.bitmapImageRepForCachingDisplay(in: page.bounds) {
                    page.cacheDisplay(in: page.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: "\(dir)/\(name)-models-page.png"))
                }
                pw.orderOut(nil)
            }
            store.removePersistentDomain(forName: suite)
            NSApplication.shared.terminate(nil)
        }
    }
}
