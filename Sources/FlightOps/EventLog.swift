import CryptoKit
import Foundation





enum EventLog {
    static let keepDays = 90
    static var dir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("FlightOps/events")
    }
    private static var lastHash: [String: String] = [:]
    private static let queue = DispatchQueue(label: "flightops.eventlog", qos: .utility)
    private static let day: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()


    static func record(_ kind: String, _ data: Data, at t: Date = Date()) {
        queue.async {
            guard let line = eventLine(kind, data, at: t) else { return }
            let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
            if lastHash[kind] == hash { return }
            lastHash[kind] = hash
            let fm = FileManager.default
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            let file = dir.appendingPathComponent(day.string(from: t) + ".jsonl")
            if let h = try? FileHandle(forWritingTo: file) {
                h.seekToEndOfFile()
                h.write(line)
                try? h.close()
            } else {
                fm.createFile(atPath: file.path, contents: line, attributes: [.posixPermissions: 0o600])
                housekeep(today: file.lastPathComponent)
            }
        }
    }


    private static func housekeep(today: String) {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { return }
        let cutoff = day.string(from: Date().addingTimeInterval(-Double(keepDays) * 86400))
        for n in names {
            let date = String(n.prefix(10))
            let path = dir.appendingPathComponent(n).path
            if date < cutoff {
                try? fm.removeItem(atPath: path)
            } else if n.hasSuffix(".jsonl"), n != today {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: "/usr/bin/gzip")
                p.arguments = ["-9", "-f", path]
                try? p.run()
            }
        }
    }
}
