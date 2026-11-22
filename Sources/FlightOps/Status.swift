import Foundation
import SwiftUI

struct StatusFile: Decodable {
    var schema: String
    var generatedAt: String?
    var viewer: String?
    var config: ConfigState?
    var repos: [RepoInfo]
    var sources: [String: SourceState]?
    var views: [String: StatusView]
    var prs: [PullRequest]

    enum CodingKeys: String, CodingKey {
        case schema, viewer, config, repos, sources, views, prs
        case generatedAt = "generated_at"
    }

    func view(_ key: String) -> StatusView? { views[key] ?? views["all"] }

    func pr(_ id: String) -> PullRequest? { prs.first { $0.id == id } }

    func repo(_ id: String) -> RepoInfo? { repos.first { $0.id == id } }
}

struct ConfigState: Decodable {
    var state: String
    var repos: Int
}

struct SourceState: Decodable {
    var state: String
    var readAt: String?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case state, error
        case readAt = "read_at"
    }
}

struct RepoInfo: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var color: String
    var state: String
    var readAt: String?
    var error: String?
    var defaultBranch: String?

    enum CodingKeys: String, CodingKey {
        case id, name, color, state, error
        case readAt = "read_at"
        case defaultBranch = "default_branch"
    }

    var tint: Color { Color(hex: color) ?? .secondary }
}

struct StatusView: Decodable {
    var state: String
    var partial: Bool
    var statusLine: String
    var counts: Counts
    var stages: [Stage]
    var prs: [String]
    var needsYou: [String]
    var trends: Trends
    var history: History

    enum CodingKeys: String, CodingKey {
        case state, partial, counts, stages, prs, trends, history
        case statusLine = "status_line"
        case needsYou = "needs_you"
    }
}

struct Counts: Decodable {
    var open: Int?
    var mergedToday: Int?
    var needsYou: Int?

    enum CodingKeys: String, CodingKey {
        case open
        case mergedToday = "merged_today"
        case needsYou = "needs_you"
    }
}

struct Stage: Decodable, Identifiable {
    var id: String
    var label: String
    var count: Int?
    var oldestSince: String?
    var oldestPr: String?
    var verdict: String?

    enum CodingKeys: String, CodingKey {
        case id, label, count, verdict
        case oldestSince = "oldest_since"
        case oldestPr = "oldest_pr"
    }
}

struct Trends: Decodable {
    var days: [String]
    var opened: [Int]
    var merged: [Int]
    var open: [Int]
    var ciWaitP90Min: [Double?]
    var ciJobs: [Int]

    enum CodingKeys: String, CodingKey {
        case days, opened, merged, open
        case ciWaitP90Min = "ci_wait_p90_min"
        case ciJobs = "ci_jobs"
    }
}

struct History: Decodable {
    var complete: Bool
    var oldest: String?
}

struct PullRequest: Decodable, Identifiable {
    var id: String
    var repo: String
    var number: Int
    var title: String
    var url: String?
    var author: String?
    var draft: Bool
    var stage: String
    var stageSince: String?
    var waitingOn: String
    var ci: String
    var review: String
    var needsYou: String?

    enum CodingKeys: String, CodingKey {
        case id, repo, number, title, url, author, draft, stage, ci, review
        case stageSince = "stage_since"
        case waitingOn = "waiting_on"
        case needsYou = "needs_you"
    }
}

enum Clock {
    private static let parser: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func date(_ text: String?) -> Date? { text.flatMap { parser.date(from: $0) } }

    static var anchor: Date?

    static func age(_ text: String?, now: Date? = nil) -> String {
        guard let d = date(text) else { return "–" }
        return span(max(0, (now ?? anchor ?? Date()).timeIntervalSince(d)))
    }

    static func span(_ seconds: TimeInterval) -> String {
        let s = Int(seconds)
        if s < 60 { return "now" }
        if s < 3600 { return "\(s / 60)m" }
        if s < 86400 { return s % 3600 >= 600 ? "\(s / 3600)h \((s % 3600) / 60)m" : "\(s / 3600)h" }
        return "\(s / 86400)d"
    }
}

extension Color {
    init?(hex: String) {
        guard hex.count == 7, hex.hasPrefix("#"), let v = UInt32(hex.dropFirst(), radix: 16) else { return nil }
        self.init(red: Double((v >> 16) & 0xFF) / 255, green: Double((v >> 8) & 0xFF) / 255, blue: Double(v & 0xFF) / 255)
    }
}
