import AppKit
import Foundation
import os



enum Collector: String, CaseIterable {
    case rig, mac, repos, linear, prfacts, pull

    var every: TimeInterval {
        switch self {
        case .rig: return SourceID.rig.every
        case .mac: return SourceID.mac.every
        case .repos: return SourceID.repos.every
        case .linear: return SourceID.linear.every

        case .prfacts: return 300
        case .pull: return 60
        }
    }


    var timeout: TimeInterval { self == .prfacts ? 120 : self == .pull ? 25 : 45 }
}



@MainActor final class Monitor: ObservableObject {
    enum Mode { case live, snapshot, inert }

    @Published private(set) var collected = Collected()
    var host: HostSnap? { collected.host }
    var guests: [GuestSnap] { collected.guests }
    var guestsKnown: Bool { collected.guestsKnown }
    var rigCI: RigCIHealth? { collected.rigCI }
    var vm: VMSnap? { collected.vm }
    var agents: [AgentSnap]? { collected.agents }
    var agentsAt: Date? { collected.agentsAt }
    var agentsError: String? { collected.agentsError }
    var mac: MacSnap? { collected.mac }
    var repos: RepoSnap? { collected.repos }
    var linear: LinearSnap? { collected.linear }
    var status: [SourceID: SourceStatus] { collected.status }
    var history: [Sample] { collected.history }


    @Published private(set) var dismissed: [String: String] =
        (UserDefaults.standard.dictionary(forKey: "dismissed") as? [String: String]) ?? [:]

    func isDismissed(_ id: String, _ says: String) -> Bool { dismissed[id] == says }
    func dismiss(_ items: [(id: String, says: String)]) {
        for i in items { dismissed[i.id] = i.says }
        UserDefaults.standard.set(dismissed, forKey: "dismissed")
    }
    func restoreDismissed() {
        dismissed = [:]
        UserDefaults.standard.removeObject(forKey: "dismissed")
    }

    var verdict: SystemVerdict { Rules.verdict(checks: checks, needs: needs, acknowledged: { self.isDismissed($0, $1) }) }

    func dismissShown() { dismiss(verdict.open.map { ($0.key, $0.says) }) }


    @Published private(set) var flow: [FlowSample] = Monitor.loadFlow()
    @Published private(set) var summaryText: String?
    private lazy var summarizer = SummaryStore(writer: SummaryWriters.system()) { [weak self] text in
        if self?.summaryText != text { self?.summaryText = text }
    }

    @Published private(set) var timelines: [String: PRTimeline] = Monitor.load("pr-timelines.json") ?? [:]

    @Published private(set) var modelSource: ModelSource = {
        var source = ModelSource()
        source.read(Monitor.modelFile, now: Date().timeIntervalSince1970)
        return source
    }()
    var prModels: [String: PRModels] { modelSource.models }
    var liveModels: LiveModels? { collected.shownLiveModels(now: Date()) }
    var shownModels: [String: PRModels] { liveModels?.records ?? modelSource.models }
    var modelsStatus: String { modelSource.statusLine(now: Date().timeIntervalSince1970, live: liveModels, liveProblem: collected.modelsProblem) }
    @Published private(set) var decisionSource = DecisionSource()
    var shownDecision: DecisionSource { collected.bestDecision(now: Date()) ?? decisionSource }
    var bestPresentation: BestPresentation? { collected.liveBest?.presentation }
    private var decisionReadInFlight = false
    static var decisionFile: URL { flowFile.deletingLastPathComponent().appendingPathComponent("model-decision.json") }
    static var modelFile: URL { flowFile.deletingLastPathComponent().appendingPathComponent("pr-models.json") }
    private func refreshModels() {
        let now = Date()
        modelSource.read(Self.modelFile, now: now.timeIntervalSince1970)
        refreshDecision()
    }

    private func refreshDecision() {
        guard !decisionReadInFlight else { return }
        decisionReadInFlight = true
        let previous = decisionSource
        Task { @MainActor in
            self.decisionSource = await Self.readDecision(previous, from: Self.decisionFile)
            self.decisionReadInFlight = false
        }
    }

    nonisolated static func readDecision(_ previous: DecisionSource, from url: URL) async -> DecisionSource {
        await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                var next = previous
                next.read(url, now: Date())
                continuation.resume(returning: next)
            }
        }
    }


    @Published private(set) var factsFile: PRFactsFile? = Monitor.load("pr-facts.json")

    @Published private(set) var factsFailedAt: Double?
    var prFacts: [String: PRFact] { factsFile?.prs ?? [:] }
    @Published private(set) var checks: [Check] = []
    @Published private(set) var needs = Needs()

    let mode: Mode
    let log = Logger(subsystem: "io.github.jammin8.flightops", category: "monitor")
    private(set) var alerts: Alerts?
    private var timers: [Timer] = []
    private var inFlight: Set<Collector> = []


    init(mode: Mode = .live) {
        self.mode = mode
        if mode == .inert { return }
        if mode == .live { alerts = Alerts() }
        collected.storyRefused = PRStory.refusedCount(in: PRStory.dir)
        refreshAll()
        guard mode == .live else { return }

        for c in Collector.allCases {
            timers.append(Timer.scheduledTimer(withTimeInterval: c.every, repeats: true) { _ in
                Task { @MainActor in self.refresh(c) }
            })
        }

        timers.append(Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
            Task { @MainActor in self.refreshModels() }
        })

        timers.append(Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in
            Task { @MainActor in self.recompute() }
        })
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in self.didWake() }
        }
    }

    func refreshAll() {
        refreshModels()
        refresh(.pull) { Collector.allCases.filter { $0 != .pull }.forEach { self.refresh($0) } }
    }

    private func didWake() {
        alerts?.rebaseline()
        refreshAll()
    }


    nonisolated static func collector(_ name: String) -> String {
        if let res = Bundle.main.resourceURL?.appendingPathComponent("collectors/\(name).py").path,
           FileManager.default.fileExists(atPath: res) { return res }
        return NSHomeDirectory() + "/Developer/FlightOps/collectors/\(name).py"
    }

    nonisolated static func run(_ name: String, arguments: [String] = [], timeout: TimeInterval = 45) -> Data? {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
        return Supervised.run("/usr/bin/python3", [collector(name)] + arguments, environment: env, timeout: timeout).data
    }



    nonisolated static func runAsync(_ name: String, arguments: [String] = [], timeout: TimeInterval = 45) async -> Data? {
        await withCheckedContinuation { (cont: CheckedContinuation<Data?, Never>) in
            DispatchQueue.global(qos: .utility).async {
                cont.resume(returning: Monitor.run(name, arguments: arguments, timeout: timeout))
            }
        }
    }

    func refresh(_ c: Collector, then next: (@MainActor () -> Void)? = nil) {
        guard !inFlight.contains(c), !(mode == .snapshot && c == .prfacts) else { next?(); return }
        let now = Date()
        if c == .repos, collected.feed?.owns(.repos, now: now) == true { next?(); return }
        let hostOnly = c == .rig && collected.feed?.owns(.agents, now: now) == true
        let arguments = hostOnly ? ["--host-only"] : []
        inFlight.insert(c)
        Task { @MainActor in
            let data: Data? = await Monitor.runAsync(c.rawValue, arguments: arguments, timeout: c.timeout)
            self.inFlight.remove(c)
            self.apply(c, data, at: Date(), hostOnly: hostOnly)
            self.recompute()
            next?()
        }
    }



    private func apply(_ c: Collector, _ data: Data?, at now: Date, hostOnly: Bool) {

        if mode == .live, c != .pull, let data { EventLog.record("\(c)", data, at: now) }
        switch c {
        case .rig: applyRig(data, now, hostOnly: hostOnly)
        case .mac: applyMac(data, now)
        case .repos: applyRepos(data, now)
        case .linear: applyLinear(data, now)
        case .prfacts: applyFacts(data)
        case .pull: applySnapshot(data, now)
        }
    }


    private func applyFacts(_ data: Data?) {
        guard data != nil else {
            factsFailedAt = Date().timeIntervalSince1970
            log.error("prfacts: collector failed")
            return
        }
        if let f: PRFactsFile = Monitor.load("pr-facts.json"), f.prs != nil { factsFile = f } else { factsFailedAt = Date().timeIntervalSince1970 }
    }

    private func logProblems() {
        for problem in collected.drainProblems() { log.error("\(problem, privacy: .public)") }
    }

    private func applyRig(_ data: Data?, _ now: Date, hostOnly: Bool) {
        collected.applyRig(data, now: now, hostOnly: hostOnly)
        logProblems()
    }

    private func applyMac(_ data: Data?, _ now: Date) {
        collected.applyMac(data, now: now)
        logProblems()
    }

    private func applyRepos(_ data: Data?, _ now: Date) {
        let fresh = collected.applyRepos(data, now: now)
        logProblems()
        recordRepos(fresh)
    }

    private func applySnapshot(_ data: Data?, _ now: Date) {
        let before = collected.feed?.owned ?? []
        let fresh = collected.importSnapshot(data, now: now)
        logProblems()
        if mode == .live, let fresh, let payload = PublishedStatus.reposPayload(PublishedStatus.read(data, now: now)) {
            EventLog.record("repos", payload.data, at: fresh.at)
        }
        recordRepos(fresh)
        let lost = before.subtracting(collected.feed?.owned ?? [])
        if lost.contains(.repos) { refresh(.repos) }
        if lost.contains(.agents) { refresh(.rig) }
    }

    private func recordRepos(_ fresh: (snap: RepoSnap, at: Date)?) {
        guard mode == .live, let fresh else { return }
        refreshModels()
        if let raw = try? Data(contentsOf: Monitor.modelFile) {
            EventLog.record("pr-models", raw, at: fresh.at)
        }
        let next = Ingest.advance(flow: flow, timelines: timelines, snap: fresh.snap, at: fresh.at, mac: mac, models: prModels)
        flow = next.flow
        Monitor.saveFlow(flow)
        timelines = next.timelines
        Monitor.save(timelines, "pr-timelines.json")
        PRStory.record(prs: fresh.snap.prs ?? [], merged: fresh.snap.recent, timelines: timelines, models: prModels, now: fresh.at.timeIntervalSince1970) { problem, refused in
            Task { @MainActor in
                self.collected.storyProblem = problem
                self.collected.storyRefused = refused
            }
        }
    }

    private func applyLinear(_ data: Data?, _ now: Date) {
        collected.applyLinear(data, now: now)
        logProblems()
    }



    var world: World {
        collected.world(now: Date(), timelines: timelines)
    }


    func recompute() {
        let w = world
        let c = Rules.checks(w)
        checks = c
        needs = Rules.needs(w, checks: c, threadURL: threadURL)
        alerts?.evaluate(w, checks: c)
        if mode == .live { summarizer.update(w, flow: flow) }
    }

    static var flowFile: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("FlightOps/flow-history.json")
    }
    static func loadFlow() -> [FlowSample] {
        (try? JSONDecoder().decode([FlowSample].self, from: Data(contentsOf: flowFile))) ?? []
    }
    static func load<T: Decodable>(_ name: String) -> T? {
        (try? Data(contentsOf: flowFile.deletingLastPathComponent().appendingPathComponent(name))).flatMap { Ingest.decoded(T.self, $0) }
    }
    static func save<T: Encodable>(_ v: T, _ name: String) {
        let url = flowFile.deletingLastPathComponent().appendingPathComponent(name)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? JSONEncoder().encode(v).write(to: url, options: .atomic)
    }
    static func saveFlow(_ f: [FlowSample]) {
        try? FileManager.default.createDirectory(at: flowFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? JSONEncoder().encode(f).write(to: flowFile, options: .atomic)
    }



    func age(_ s: SourceID, at now: Date) -> Int {
        max(0, Int(now.timeIntervalSince(status[s]?.sampleAt ?? now)))
    }

    func freshness(_ s: SourceID) -> Freshness { (status[s] ?? SourceStatus()).freshness(s, now: Date()) }
    func fresh(_ s: SourceID) -> Bool { freshness(s) == .fresh }

    var rigOffline: Bool { world.rigUnreachable }
    var agentsKnown: Bool { world.agentsKnown }
    var rigCIGuests: [GuestSnap] { guests.filter { $0.isCI } }

    var iosJobs: [IOSJob]? { fresh(.mac) ? mac?.iosCI : nil }
    var openPRs: [PRRow] { repos?.prs ?? [] }
    var launch: LaunchInfo? { linear?.launch }


    var agentsWorking: Int? { world.agentsWorking }

    var ciGuestsKnown: Bool { world.ciGuestsKnown }
    var ciRunning: Int? { world.ciRunning }
    var awaitingReview: Int? { world.awaitingReview }
    var merged24h: Int? {
        guard fresh(.repos) else { return nil }

        return Analytics.mergedCount(repos)
    }


    func workTitle(_ issue: String?, _ title: String) -> String { Rules.workTitle(issue, title, linear) }


    var statusText: String {
        var lines = ["FlightOps · \(Date().formatted(date: .abbreviated, time: .shortened)) · \(verdict.text)"]
        let agentsText = agentsWorking.map { String($0) } ?? "unknown"
        let ciText = ciRunning.map { String($0) } ?? "unknown"
        lines.append("Agents working: \(agentsText), CI jobs: \(ciText)")

        for item in needs.items {
            let tier = item.tier == .now ? "now" : item.tier == .today ? "today" : "later"
            lines.append("- [\(tier)] \(item.title): \(item.reason)")
        }
        if repos != nil { lines += Analytics.repoStatusLines(repos) }
        lines.append(modelsStatus)
        lines += checks.map { "\($0.label): \($0.detail)" }
        if !needs.gaps.isEmpty { lines.append("Unknown: " + needs.gaps.joined(separator: ", ")) }
        return lines.joined(separator: "\n")
    }
}
