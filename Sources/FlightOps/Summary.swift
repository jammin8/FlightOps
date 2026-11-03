import Foundation

struct ForecastMark: Codable, Equatable {
    let at: Double
    let p50: String
    let cohort: String
}

struct ForecastMemory: Codable, Equatable {
    var marks: [ForecastMark] = []

    static let keep: Double = 7 * 86400
    static let minAge: Double = 86400
    static let spacing: Double = 6 * 3600
    static let threshold = 7

    typealias Current = (p50: String, cohort: String, at: Double)

    static func current(_ w: World) -> Current? {
        guard w.fresh(.linear), w.retainedPlanNote == nil, let at = w.status[.linear]?.sampleAt?.timeIntervalSince1970,
              let launch = w.linear?.launch, launch.error == nil,
              let cohort = cohort(launch), let f = launch.forecast, f.scope > 0, f.remaining > 0, !f.insufficient,
              let p50 = f.p50, civilDay(p50) != nil else { return nil }
        return (String(p50.prefix(10)), cohort, at)
    }

    static func cohort(_ launch: LaunchInfo) -> String? {
        if let version = launch.version, !version.isEmpty { return "version:" + version }
        let ids = launch.projectIds.filter { !$0.isEmpty }.sorted()
        return ids.isEmpty ? nil : "projects:" + ids.joined(separator: ",")
    }

    func recording(_ current: Current?, at now: Double) -> ForecastMemory {
        var out = ForecastMemory(marks: marks.filter { now - $0.at < Self.keep && $0.at <= now })
        guard let current, current.at <= now else { return out }
        if let last = out.marks.last, last.cohort == current.cohort, current.at - last.at < Self.spacing { return out }
        out.marks.append(ForecastMark(at: current.at, p50: current.p50, cohort: current.cohort))
        return out
    }

    func shift(_ current: Current?) -> SummaryFacts.Shift? {
        guard let current, let to = Self.civilDay(current.p50) else { return nil }
        let now = current.at
        let eligible = marks.filter { $0.cohort == current.cohort && now - $0.at >= Self.minAge && now - $0.at < Self.keep }
        guard let base = eligible.min(by: { $0.at < $1.at }), let from = Self.civilDay(base.p50) else { return nil }
        let days = to - from
        guard abs(days) >= Self.threshold else { return nil }
        return SummaryFacts.Shift(days: days, from: base.p50, to: current.p50)
    }

    static func civilDay(_ ymd: String) -> Int? {
        let parts = ymd.prefix(10).split(separator: "-")
        guard ymd.count >= 10, parts.count == 3, let y = Int(parts[0]), let m = Int(parts[1]), let d = Int(parts[2]),
              (1...12).contains(m), (1...31).contains(d), y > 1970 else { return nil }
        let yy = m <= 2 ? y - 1 : y
        let era = yy / 400
        let yoe = yy - era * 400
        let doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
        let day = era * 146097 + doe - 719468
        let back = civilText(day)
        return back == String(ymd.prefix(10)) ? day : nil
    }

    static func civilText(_ day: Int) -> String {
        let z = day + 719468
        let era = z / 146097
        let doe = z - era * 146097
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
        let mp = (5 * doy + 2) / 153
        let d = doy - (153 * mp + 2) / 5 + 1
        let m = mp < 10 ? mp + 3 : mp - 9
        let y = yoe + era * 400 + (m <= 2 ? 1 : 0)
        return String(format: "%04d-%02d-%02d", y, m, d)
    }

    static let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    static func dateText(_ ymd: String) -> String? {
        let parts = ymd.prefix(10).split(separator: "-")
        guard civilDay(ymd) != nil, let m = Int(parts[1]), let d = Int(parts[2]) else { return nil }
        return "\(months[m - 1]) \(d)"
    }
}

struct SummaryFacts: Codable, Equatable {
    struct Shift: Codable, Equatable {
        let days: Int
        let from: String
        let to: String
    }
    struct Move: Codable, Equatable {
        let from: Int
        let to: Int
    }
    struct Repeat: Codable, Equatable {
        let repo: String
        let number: Int
        let failures: Int
    }
    struct Candidate: Equatable {
        let id: String
        let sentence: String
    }

    var merged: Int?
    var bottleneck: Move?
    var launch: Shift?
    var repeated: [Repeat] = []
    var unreadHeads = 0
    var pullsStale = false
    var planStale = false
    var failuresStale = false

    static let mergedWindowHours = 24
    static let recent: Double = 6 * 3600
    static let earlier: Double = 24 * 3600

    var hasSubstance: Bool { merged != nil || bottleneck != nil || launch != nil || !repeated.isEmpty }

    static let phrases: [PRStatus: String] = [.draft: "drafts", .fixing: "PRs being fixed", .waitingCI: "PRs waiting for CI",
                                              .inCI: "PRs running CI", .review: "PRs in review", .approved: "approved PRs", .merged: "merged PRs"]

    var candidates: [Candidate] {
        var out: [Candidate] = []
        if let merged {
            let text = merged == 0 ? "Nothing has merged in the last \(Self.mergedWindowHours) hours."
                : merged == 1 ? "1 PR merged in the last \(Self.mergedWindowHours) hours."
                : "\(merged) PRs merged over the last \(Self.mergedWindowHours) hours."
            out.append(Candidate(id: "f1", sentence: text))
        }
        if let b = bottleneck, let from = PRStatus(rawValue: b.from).flatMap({ Self.phrases[$0] }), let to = PRStatus(rawValue: b.to).flatMap({ Self.phrases[$0] }) {
            out.append(Candidate(id: "f2", sentence: "The pile-up has moved from \(from) to \(to)."))
        }
        if let l = launch, let from = ForecastMemory.dateText(l.from), let to = ForecastMemory.dateText(l.to) {
            let text = l.days > 0 ? "The projected launch date slipped \(l.days) days, from \(from) to \(to)."
                : "The projected launch date pulled in \(-l.days) days, from \(from) to \(to)."
            out.append(Candidate(id: "f3", sentence: text))
        }
        if let top = repeated.first {
            let others = repeated.count - 1
            let tail = others == 0 ? "" : others == 1 ? " 1 other PR is repeating failures too." : " \(others) other PRs are repeating failures too."
            out.append(Candidate(id: "f6", sentence: "\(top.repo) #\(top.number) has failed \(top.failures) CI attempts on its current head." + tail))
        }
        if unreadHeads > 0 {
            let text = unreadHeads == 1 ? "CI history for 1 open PR hasn't been read yet, so its failures are unknown."
                : "CI history for \(unreadHeads) open PRs hasn't been read yet, so their failures are unknown."
            out.append(Candidate(id: "f7", sentence: text))
        }
        if failuresStale { out.append(Candidate(id: "f8", sentence: "CI failure counts are out of date.")) }
        if pullsStale { out.append(Candidate(id: "f4", sentence: "Pull request numbers are out of date.")) }
        if planStale { out.append(Candidate(id: "f5", sentence: "The launch plan is out of date, so there is no date news.")) }
        return out
    }

    var canonical: String { candidates.map { "\($0.id): \($0.sentence)" }.joined(separator: "\n") }

    static func mergedCount(_ snap: RepoSnap?) -> Int? {
        guard let snap, snap.repoRowsComplete, let rows = snap.repos else { return nil }
        let own = rows.filter { RepoSnap.configured.contains($0.name) }
        guard Set(own.map(\.name)) == RepoSnap.configured, own.count == RepoSnap.configured.count else { return nil }
        let counts = own.compactMap(\.merged_24h)
        guard counts.count == own.count else { return nil }
        return PRModels.Tokens.sum(counts)
    }

    static func build(_ w: World, flow: [FlowSample], memory: ForecastMemory) -> SummaryFacts? {
        let now = w.now.timeIntervalSince1970
        var f = SummaryFacts()
        if w.fresh(.repos) {
            f.merged = mergedCount(w.repos)
            f.bottleneck = bottleneckMove(flow, now: now)
            if w.prsKnown { applyFailures(&f, w) }
        } else {
            f.pullsStale = true
        }
        if w.fresh(.linear), w.retainedPlanNote == nil {
            f.launch = memory.shift(ForecastMemory.current(w))
        } else {
            f.planStale = true
        }
        return f.hasSubstance ? f : nil
    }

    static let repeatedShown = 1

    static func applyFailures(_ f: inout SummaryFacts, _ w: World) {
        guard let report = w.repeatedFailures, w.now.timeIntervalSince1970 - report.generated <= RepeatedFailures.limit,
              report.generated <= w.now.timeIntervalSince1970 + clockSkew else {
            f.failuresStale = w.feed != nil
            return
        }
        var found: [Repeat] = []
        for p in w.openPRs where RepoSnap.configured.contains(p.repo) {
            guard let head = p.head, let entry = report.prs.first(where: { $0.repo == p.repo && $0.number == p.number }),
                  entry.read, head == entry.head, let failed = entry.failed, let readAt = entry.readAt,
                  readAt <= w.now.timeIntervalSince1970 + clockSkew, w.now.timeIntervalSince1970 - readAt <= RepeatedFailures.readLimit else {
                f.unreadHeads += 1
                continue
            }
            if failed >= RepeatedFailures.threshold { found.append(Repeat(repo: p.repo, number: p.number, failures: failed)) }
        }
        f.repeated = found.sorted { ($1.failures, $0.repo, $0.number) < ($0.failures, $1.repo, $1.number) }
    }

    static func bottleneckMove(_ flow: [FlowSample], now: Double) -> Move? {
        let split = now - recent
        guard FlowSample.covered(flow, since: split, now: now),
              let latest = FlowSample.bottleneck(flow, since: split, now: now),
              let before = FlowSample.bottleneck(flow.filter { $0.t <= split }, since: now - earlier, now: split),
              latest.status != before.status else { return nil }
        return Move(from: before.status.rawValue, to: latest.status.rawValue)
    }
}

enum SummaryPrompt {
    static let version = "summary/2"
    static let limit = 3

    static let instructions = """
    You pick which facts matter most right now for a short engineering status report. Each fact has an id. \
    Return at most 3 ids, most important first, written exactly as listed. Do not write any other text.
    """

    static func prompt(_ facts: SummaryFacts) -> String {
        "Facts:\n" + facts.canonical + "\n\nPick the ids."
    }

    static func key(_ facts: SummaryFacts) -> String {
        DecisionSHA256.hex(Array((version + "\n" + instructions + "\n" + facts.canonical).utf8))
    }

    static func report(_ ids: [String]?, facts: SummaryFacts) -> String? {
        guard let ids, !ids.isEmpty, ids.count <= limit, Set(ids).count == ids.count else { return nil }
        let sentences = Dictionary(uniqueKeysWithValues: facts.candidates.map { ($0.id, $0.sentence) })
        let picked = ids.compactMap { sentences[$0] }
        return picked.count == ids.count ? picked.joined(separator: " ") : nil
    }
}

struct SummaryCache: Codable, Equatable {
    struct Entry: Codable, Equatable {
        let key: String
        let text: String?
    }
    var entries: [Entry] = []

    static let capacity = 8

    func lookup(_ key: String) -> Entry? { entries.first { $0.key == key } }

    func storing(_ key: String, _ text: String?) -> SummaryCache {
        var out = entries.filter { $0.key != key }
        out.append(Entry(key: key, text: text))
        return SummaryCache(entries: Array(out.suffix(Self.capacity)))
    }
}

enum SummaryPlan: Equatable {
    case none
    case cached(String?)
    case generate(key: String, instructions: String, prompt: String)

    static func decide(_ facts: SummaryFacts?, cache: SummaryCache) -> SummaryPlan {
        guard let facts else { return .none }
        let key = SummaryPrompt.key(facts)
        if let hit = cache.lookup(key) { return .cached(hit.text) }
        return .generate(key: key, instructions: SummaryPrompt.instructions, prompt: SummaryPrompt.prompt(facts))
    }

    static func settle(_ ids: [String]?, facts: SummaryFacts, key: String, cache: SummaryCache) -> (text: String?, cache: SummaryCache) {
        let text = SummaryPrompt.report(ids, facts: facts)
        return (text, cache.storing(key, text))
    }
}

protocol SummaryWriting {
    var available: Bool { get }
    func choose(instructions: String, prompt: String) async -> [String]?
}
