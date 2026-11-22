import Foundation
import UserNotifications
import SwiftUI

struct AvailableRepo: Decodable, Identifiable, Hashable {
    var name: String
    var isPrivate: Bool
    var pushedAt: String?
    var archived: Bool

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, archived
        case isPrivate = "private"
        case pushedAt = "pushed_at"
    }
}

private struct RepoListing: Decodable {
    var repos: [AvailableRepo]?
    var error: String?
}

enum Paths {
    static let home = URL(fileURLWithPath: NSHomeDirectory())

    static var status: URL {
        if let custom = ProcessInfo.processInfo.environment["FLIGHTOPS_STATUS_FILE"], !custom.isEmpty { return URL(fileURLWithPath: custom) }
        return home.appendingPathComponent(".local/state/flightops/status.json")
    }

    static var config: URL {
        if let custom = ProcessInfo.processInfo.environment["FLIGHTOPS_CONFIG"], !custom.isEmpty { return URL(fileURLWithPath: custom) }
        return home.appendingPathComponent(".config/flightops/config.json")
    }

    static var collectors: URL {
        if let custom = ProcessInfo.processInfo.environment["FLIGHTOPS_COLLECTORS"], !custom.isEmpty { return URL(fileURLWithPath: custom, isDirectory: true) }
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("collectors"),
           FileManager.default.fileExists(atPath: bundled.appendingPathComponent("flightops.py").path) {
            return bundled
        }
        var folder = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().deletingLastPathComponent()
        for _ in 0..<5 {
            let candidate = folder.appendingPathComponent("collectors")
            if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("flightops.py").path) { return candidate }
            folder = folder.deletingLastPathComponent()
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("collectors")
    }

    static var sample: URL { collectors.appendingPathComponent("sample-status.json") }
}

enum Collector {
    static func run(_ arguments: [String], timeout: TimeInterval) async -> (code: Int32, output: Data) {
        await withCheckedContinuation { done in
            DispatchQueue.global(qos: .utility).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
                process.arguments = ["python3", "-I", Paths.collectors.appendingPathComponent("flightops.py").path] + arguments
                var env = ProcessInfo.processInfo.environment
                env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
                process.environment = env
                let out = Pipe()
                process.standardOutput = out
                process.standardError = FileHandle.nullDevice
                process.standardInput = FileHandle.nullDevice
                do {
                    try process.run()
                } catch {
                    done.resume(returning: (-1, Data()))
                    return
                }
                let timer = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: timer)
                let data = out.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                timer.cancel()
                done.resume(returning: (process.terminationStatus, data))
            }
        }
    }
}

@MainActor final class Monitor: ObservableObject {
    enum Mode { case live, fixed(URL) }

    @Published private(set) var status: StatusFile?
    @Published private(set) var demo = false
    @Published private(set) var problem: String?
    @Published private(set) var collecting = false
    @Published private(set) var loadedAt: Date?
    @Published var available: [AvailableRepo] = []
    @Published var availableError: String?
    @Published var loadingAvailable = false

    private let mode: Mode
    private var timer: Timer?
    private var lastCollect = Date.distantPast
    private var statusStamp: Date?
    static let collectEvery: TimeInterval = 120

    init(mode: Mode) {
        self.mode = mode
        switch mode {
        case .fixed(let url):
            status = Monitor.decode(url)
            Clock.anchor = Clock.date(status?.generatedAt)
        case .live:
            reload()
            timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
            Task { await collect() }
        }
    }

    var configured: Bool { FileManager.default.fileExists(atPath: Paths.config.path) && (status?.config?.repos ?? 0) > 0 }

    static func decode(_ url: URL) -> StatusFile? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let file = try? JSONDecoder().decode(StatusFile.self, from: data), file.schema == "flightops-status/2" else { return nil }
        return file
    }

    func reload() {
        guard case .live = mode else { return }
        let stamp = (try? FileManager.default.attributesOfItem(atPath: Paths.status.path)[.modificationDate]) as? Date
        if stamp != nil && stamp == statusStamp && status != nil { return }
        statusStamp = stamp
        let real = Monitor.decode(Paths.status)
        if let real, (real.config?.repos ?? 0) > 0 {
            status = real
            demo = false
            problem = nil
        } else {
            status = Monitor.decode(Paths.sample)
            demo = true
            problem = status == nil ? "The sample status file is missing from this build." : nil
        }
        Clock.anchor = demo ? Clock.date(status?.generatedAt) : nil
        loadedAt = Date()
        if !demo, let stages = status?.views["all"]?.stages { Alerts.check(stages) }
    }

    private func tick() {
        reload()
        if Date().timeIntervalSince(lastCollect) >= Monitor.collectEvery { Task { await collect() } }
    }

    func collect() async {
        guard case .live = mode, !collecting, FileManager.default.fileExists(atPath: Paths.config.path) else { return }
        collecting = true
        lastCollect = Date()
        let result = await Collector.run(["collect"], timeout: 150)
        collecting = false
        if result.code == -1 { problem = "Python 3 could not start. Install Apple's command line tools: xcode-select --install" }
        statusStamp = nil
        reload()
    }

    func loadAvailable() async {
        loadingAvailable = true
        availableError = nil
        let result = await Collector.run(["repos"], timeout: 60)
        loadingAvailable = false
        guard let listing = try? JSONDecoder().decode(RepoListing.self, from: result.output) else {
            availableError = result.code == -1 ? "Python 3 could not start. Install Apple's command line tools: xcode-select --install" : "Couldn't list your repos. Is the GitHub CLI installed and signed in? Run: gh auth login"
            return
        }
        if let error = listing.error {
            availableError = error.contains("signed in") ? "The GitHub CLI isn't signed in. Run: gh auth login" : error
        }
        available = (listing.repos ?? []).filter { !$0.archived }
    }

    func use(_ repos: [String]) async {
        guard !repos.isEmpty else { return }
        _ = await Collector.run(["use"] + repos, timeout: 20)
        statusStamp = nil
        lastCollect = .distantPast
        await collect()
    }

    var currentRepos: [String] { demo ? [] : (status?.repos.map(\.id) ?? []) }
}

enum Alerts {
    private static let key = "stuckStages"

    static func check(_ stages: [Stage]) {
        let before = Set(UserDefaults.standard.stringArray(forKey: key) ?? [])
        var now = before
        for stage in stages {
            guard let state = stage.verdict?.state else { continue }
            if state == "stuck" && !before.contains(stage.id) {
                now.insert(stage.id)
                post("\(stage.label) is stuck", stage.verdict?.reason ?? "")
            } else if (state == "clear" || state == "busy") && before.contains(stage.id) {
                now.remove(stage.id)
                post("\(stage.label) has cleared", "Back to normal.")
            }
        }
        if now != before { UserDefaults.standard.set(Array(now), forKey: key) }
    }

    private static func post(_ title: String, _ body: String) {
        guard Bundle.main.bundleIdentifier != nil else { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
    }
}
