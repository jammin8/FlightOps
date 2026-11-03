import Foundation

struct Collected {
    var storyProblem: String?
    var storyRefused = 0
    var host: HostSnap?
    var guests: [GuestSnap] = []
    var guestsKnown = false
    var rigCI: RigCIHealth?
    var vm: VMSnap?
    var rigStatus: RigStatus?
    var agents: [AgentSnap]?
    var agentsAt: Date?
    var agentsError: String?
    var mac: MacSnap?
    var repos: RepoSnap?
    var linear: LinearSnap?
    var status: [SourceID: SourceStatus] = [:]
    var history: [Sample] = []
    var problems: [String] = []
    var feed: Feed?
    var liveModels: LiveModels?
    var modelsProblem: String?
    var liveBest: LiveBest?
    var bestProblem: String?
    var repeatedFailures: RepeatedFailures?

    func bestDecision(now: Date) -> DecisionSource? {
        guard feed != nil, liveBest != nil || bestProblem != nil else { return nil }
        let held = liveBest.map { now.timeIntervalSince($0.at) > LiveBest.limit } ?? false
        let problem = bestProblem ?? (feed?.live(now: now) != true ? "live Best feed stale: publisher stopped" : held ? "live Best feed stale" : nil)
        var source = DecisionSource()
        source.lastGood = liveBest?.decision
        source.attemptedAt = now
        source.acceptedAt = liveBest?.at
        source.error = problem
        source.failureKind = problem == nil ? nil : "Failed refresh"
        return source
    }

    func shownLiveModels(now: Date) -> LiveModels? {
        guard feed?.live(now: now) == true, let liveModels, liveModels.current(now: now) else { return nil }
        return liveModels
    }

    func world(now: Date, timelines: [String: PRTimeline]) -> World {
        World(now: now, status: status, host: host, guests: guests, rigCI: rigCI, vm: vm, agents: agents,
              agentsAt: agentsAt, mac: mac, repos: repos, linear: linear, timelines: timelines, guestsKnown: guestsKnown, rigStatus: rigStatus, feed: feed, storyProblem: storyProblem, storyRefused: storyRefused, repeatedFailures: repeatedFailures)
    }

    mutating func succeed(_ s: SourceID, at t: Date, note: String? = nil, staleAfter: TimeInterval? = nil, origin: String? = nil) {
        var st = status[s] ?? SourceStatus()
        st.sampleAt = t
        st.observedAt = t
        st.failures = 0
        st.unreachable = false
        st.error = nil
        st.note = note
        st.refreshFailed = false
        st.staleAfter = staleAfter
        st.origin = origin
        st.expiresAt = nil
        status[s] = st
    }

    mutating func fail(_ s: SourceID, _ why: String, unreachable: Bool = false, observedAt: Date? = nil) {
        var st = status[s] ?? SourceStatus()
        st.failures += 1
        st.unreachable = unreachable
        st.error = why
        st.refreshFailed = true
        if let observedAt { st.observedAt = observedAt }
        status[s] = st
        problems.append("\(s.rawValue) refresh failed (\(st.failures) in a row): \(why)")
    }

    mutating func stale(_ s: SourceID, at t: Date, _ why: String) {
        var st = status[s] ?? SourceStatus()
        st.sampleAt = t
        st.observedAt = t
        st.failures += 1
        st.error = why
        st.refreshFailed = true
        status[s] = st
    }

    func regressed(_ s: SourceID, _ t: Date, now: Date, origin: String? = nil) -> Bool {
        guard origin == nil || status[s]?.origin == origin, let held = status[s]?.sampleAt, held.timeIntervalSince(now) <= clockSkew else { return false }
        return t < held
    }

    mutating func timed(_ raw: Double?, now: Date, _ sources: SourceID...) -> Date? {
        if let t = Ingest.validTime(raw, now: now) { return t }
        for s in sources { fail(s, Ingest.untimed) }
        return nil
    }

    mutating func applyRig(_ data: Data?, now: Date, hostOnly: Bool = false) {
        guard let data else {
            fail(.rig, "collector failed")
            if !hostOnly { fail(.agents, "collector failed") }
            return
        }
        guard let snap = Ingest.decoded(RigSnap.self, data) else {
            fail(.rig, "unreadable collector output")
            if !hostOnly { fail(.agents, "unreadable collector output") }
            return
        }
        guard let t = hostOnly ? timed(snap.collected, now: now, .rig) : timed(snap.collected, now: now, .rig, .agents) else { return }
        if regressed(.rig, t, now: now) {
            problems.append("rig: ignored a payload older than the accepted one")
            return
        }
        if !snap.unreadable.isEmpty { problems.append("rig: unreadable " + snap.unreadable.joined(separator: ", ")) }
        let vmParts = snap.unreadable.filter { $0.hasPrefix("agents") || $0.hasPrefix("vm") }
        let hostParts = snap.unreadable.filter { !($0.hasPrefix("agents") || $0.hasPrefix("vm")) }
        let hostNote: String? = hostParts.isEmpty ? nil : "unreadable " + hostParts.joined(separator: ", ")
        let vmNote: String? = vmParts.isEmpty ? nil : "unreadable " + vmParts.joined(separator: ", ")
        if let h = snap.host {
            host = h
            guests = snap.vms
            guestsKnown = snap.vmsKnown
            rigCI = snap.ci
            succeed(.rig, at: t, note: hostNote)
            if let cpu = h.cpu {
                history.append(Sample(t: t, cpu: cpu, mem: h.memFraction.map { $0 * 100 }, temp: h.temp_c))
                if history.count > 180 { history.removeFirst(history.count - 180) }
            }
        } else {
            let why = snap.hostError ?? snap.error ?? "no host data"
            let down = why.hasPrefix("unreachable")
            fail(.rig, why, unreachable: down, observedAt: down ? t : nil)
        }
        if snap.vmSkipped { return }
        if let v = snap.vm {
            vm = v
            rigStatus = snap.rigStatus
            succeed(.agents, at: t, note: vmNote)
        } else {
            let why = snap.vmError ?? snap.error ?? "no VM data"
            let down = why.hasPrefix("unreachable")
            fail(.agents, why, unreachable: down, observedAt: down ? t : nil)
        }
        applyAgents(snap.agents, unreadable: snap.unreadable, error: snap.agentsError, at: t)
    }

    mutating func applyAgents(_ list: [AgentSnap]?, unreadable: [String], error: String?, at t: Date) {
        if let list, !unreadable.contains(where: { $0 == "agents" || $0.hasPrefix("agents:") }) {
            agents = list
            agentsAt = t
            agentsError = nil
        } else {
            agents = nil
            agentsError = error ?? "no agent data"
        }
    }

    mutating func applyVM(_ data: Data?, now: Date, origin: String? = nil) {
        guard let data else { fail(.agents, "collector failed"); return }
        guard let snap = Ingest.decoded(VMLocalSnap.self, data) else { fail(.agents, "unreadable collector output"); return }
        guard let t = timed(snap.collected, now: now, .agents) else { return }
        if regressed(.agents, t, now: now, origin: origin) {
            problems.append("agents: ignored a payload older than the accepted one")
            return
        }
        guard let v = snap.vm else { fail(.agents, snap.vmError ?? "no VM data"); return }
        vm = v
        rigStatus = snap.rigStatus
        succeed(.agents, at: t, note: snap.unreadable.isEmpty ? nil : "unreadable " + snap.unreadable.joined(separator: ", "),
                staleAfter: SourceID.vmLocalStaleAfter, origin: origin)
        applyAgents(snap.agents, unreadable: snap.unreadable, error: snap.agentsError, at: t)
    }

    mutating func applyMac(_ data: Data?, now: Date) {
        guard let data else { fail(.mac, "collector failed"); return }
        guard let snap = Ingest.decoded(MacSnap.self, data) else { fail(.mac, "unreadable collector output"); return }
        guard let t = timed(snap.collected, now: now, .mac) else { return }
        if regressed(.mac, t, now: now) {
            problems.append("mac: ignored a payload older than the accepted one")
            return
        }
        mac = snap
        let notes = snap.errors.map { "\($0.key): \($0.value)" }.sorted() + snap.unreadable.map { "unreadable \($0)" }
        succeed(.mac, at: t, note: notes.isEmpty ? nil : notes.joined(separator: "; "))
    }

    mutating func applyRepos(_ data: Data?, now: Date, origin: String? = nil) -> (snap: RepoSnap, at: Date)? {
        guard let data else { fail(.repos, "collector failed"); return nil }
        guard let snap = Ingest.decoded(RepoSnap.self, data) else { fail(.repos, "unreadable collector output"); return nil }
        if let why = snap.error, !why.isEmpty { fail(.repos, why); return nil }
        guard snap.repos != nil else { fail(.repos, "no data"); return nil }
        guard let t = timed(snap.fetched, now: now, .repos) else { return nil }
        if regressed(.repos, t, now: now, origin: origin) {
            problems.append("repos: ignored a payload older than the accepted one")
            return nil
        }
        repos = snap
        if let why = snap.staleReason {
            stale(.repos, at: t, why)
            return nil
        }
        succeed(.repos, at: t, note: snap.unreadable.isEmpty ? nil : "unreadable " + snap.unreadable.joined(separator: ", "), origin: origin)
        return snap.prs == nil ? nil : (snap, t)
    }

    mutating func applyLinear(_ data: Data?, now: Date) {
        guard let data else { fail(.linear, "collector failed"); return }
        guard let snap = Ingest.decoded(LinearSnap.self, data) else { fail(.linear, "unreadable collector output"); return }
        guard snap.ok else {
            if snap.error != nil, linear == nil || snap.error == "no_key" { linear = snap }
            fail(.linear, snap.error ?? (snap.scope == nil ? "no data" : "Linear counts incomplete"))
            return
        }
        guard let t = timed(snap.fetched, now: now, .linear) else { return }
        if regressed(.linear, t, now: now) {
            problems.append("linear: ignored a payload older than the accepted one")
            return
        }
        linear = snap
        if let why = snap.staleReason {
            stale(.linear, at: t, why)
        } else {
            succeed(.linear, at: t, note: snap.unreadable.isEmpty ? nil : "unreadable " + snap.unreadable.joined(separator: ", "))
        }
    }

    mutating func drainProblems() -> [String] {
        defer { problems = [] }
        return problems
    }
}

enum Attempt {
    enum Gate: Equatable { case due, waiting, blocked(String) }

    static func gate(_ text: String?, now: TimeInterval, every: TimeInterval, future: String, unreadable: String) -> Gate {
        guard let text else { return .due }
        guard let last = Double(text.trimmingCharacters(in: .whitespacesAndNewlines)), last.isFinite, last > 0 else { return .blocked(unreadable) }
        if last > now { return .blocked(future) }
        return now >= last + every ? .due : .waiting
    }
}

enum GitHubAttempt {
    typealias Gate = Attempt.Gate
    static let every: TimeInterval = 900
    static let future = "GitHub attempt time is in the future"
    static let unreadable = "GitHub attempt file is unreadable"
    static let unwritable = "GitHub attempt could not be recorded"

    static func gate(_ text: String?, now: TimeInterval) -> Gate {
        Attempt.gate(text, now: now, every: every, future: future, unreadable: unreadable)
    }
}

enum ModelsPass {
    static let every: TimeInterval = 900
    static let future = "Models attempt time is in the future"
    static let unreadable = "Models attempt file is unreadable"
    static let unwritable = "Models attempt could not be recorded"

    static func gate(_ text: String?, now: TimeInterval) -> Attempt.Gate {
        Attempt.gate(text, now: now, every: every, future: future, unreadable: unreadable)
    }
}

enum LedgerPass {
    static let every: TimeInterval = 900
    static let future = "Ledger attempt time is in the future"
    static let unreadable = "Ledger attempt file is unreadable"
    static let unwritable = "Ledger attempt could not be recorded"
    static let squeezed = "The pass left no time for the ledger"
    static let limit: TimeInterval = 90
    static let least: TimeInterval = 20
    static let unitLimit: TimeInterval = 240
    static let margin: TimeInterval = 15

    static func gate(_ text: String?, now: TimeInterval) -> Attempt.Gate {
        Attempt.gate(text, now: now, every: every, future: future, unreadable: unreadable)
    }

    static func allowance(elapsed: TimeInterval) -> TimeInterval? {
        let left = min(limit, unitLimit - margin - elapsed)
        return left >= least ? left : nil
    }
}

enum Ingest {
    static let untimed = "payload time unknown"
    static let numberLimit = 9_007_199_254_740_992.0

    static func decoded<T: Decodable>(_ type: T.Type, _ data: Data) -> T? {
        guard let value = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]), numbersBounded(value) else { return nil }
        return try? JSONDecoder().decode(type, from: data)
    }

    static func numbersBounded(_ value: Any) -> Bool {
        if let list = value as? [Any] { return list.allSatisfy(numbersBounded) }
        if let object = value as? [String: Any] { return object.values.allSatisfy(numbersBounded) }
        guard let number = value as? NSNumber else { return true }
        return number.doubleValue.isFinite && abs(number.doubleValue) <= numberLimit
    }

    static func validTime(_ raw: Double?, now: Date) -> Date? {
        guard let raw, raw.isFinite, raw > 0, raw <= now.timeIntervalSince1970 + clockSkew else { return nil }
        return Date(timeIntervalSince1970: raw)
    }

    static let codexBudgetLimit: TimeInterval = 3600
    static func githubBudgetLimit(_ source: SourceID) -> TimeInterval { max(900, source.staleAfter) }

    static func current<T>(_ reading: Cached<T>?, at t: Date, limit: TimeInterval) -> T? {
        guard let reading, reading.error == nil, let value = reading.value, let fetched = reading.fetchedDate else { return nil }
        let age = t.timeIntervalSince(fetched)
        return age <= limit && age >= -clockSkew ? value : nil
    }

    static func advance(flow: [FlowSample], timelines: [String: PRTimeline], snap: RepoSnap, at t: Date, mac: MacSnap?,
                        models: [String: PRModels]) -> (flow: [FlowSample], timelines: [String: PRTimeline]) {
        if let last = flow.last?.t, t.timeIntervalSince1970 < last { return (flow, timelines) }
        let prs = snap.prs ?? []
        var sample = FlowSample.of(prs, at: t.timeIntervalSince1970, complete: snap.openInventoryComplete)
        sample.main = Dictionary((snap.repos ?? []).compactMap { r in r.main.map { (r.name, $0) } }, uniquingKeysWith: { a, _ in a })
        var budgets: [String: Double] = [:]
        let github = current(snap.githubBudget, at: t, limit: githubBudgetLimit(.repos)) ?? current(mac?.github, at: t, limit: githubBudgetLimit(.mac))
        if let github, github.core_limit > 0 { budgets["github"] = Double(github.core) / Double(github.core_limit) }
        if let codex = current(mac?.codex, at: t, limit: codexBudgetLimit) { budgets["codex"] = codex.used_pct }
        sample.budgets = budgets.isEmpty ? nil : budgets
        var builders: [String: String] = [:]
        for p in prs { if let b = ModelName.builder(branch: p.branch, models: models[p.id]) { builders[p.id] = b } }
        for mg in snap.recent { if let b = ModelName.builder(branch: mg.branch, models: models[mg.id]) { builders[mg.id] = b } }
        let next = PRTimeline.update(timelines, prs: prs, merged: snap.recent, now: t.timeIntervalSince1970, builders: builders,
                                     complete: snap.openInventoryComplete)
        return (FlowSample.append(sample, to: flow), next)
    }
}

enum Store {
    static var directory: URL {
        if let custom = ProcessInfo.processInfo.environment["FLIGHTOPS_DATA_DIR"], !custom.isEmpty {
            return URL(fileURLWithPath: custom, isDirectory: true)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("FlightOps", isDirectory: true)
    }

    static func url(_ name: String) -> URL { directory.appendingPathComponent(name) }

    static func load<T: Decodable>(_ name: String) -> T? {
        (try? Data(contentsOf: url(name))).flatMap { Ingest.decoded(T.self, $0) }
    }

    static func save<T: Encodable>(_ v: T, _ name: String) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? JSONEncoder().encode(v).write(to: url(name), options: .atomic)
    }

    static func write(_ data: Data, _ name: String) -> Bool {
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try data.write(to: url(name), options: .atomic)
            return true
        } catch {
            return false
        }
    }
}
