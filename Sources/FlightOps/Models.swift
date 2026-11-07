import Foundation






private struct Skip: Decodable {
    init(from decoder: Decoder) throws {}
}


struct Lossy<T: Decodable>: Decodable {
    var items: [T]
    var dropped: Int

    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        var out: [T] = []
        var bad = 0
        while !c.isAtEnd {
            if let v = try? c.decode(T.self) {
                out.append(v)
            } else if (try? c.decode(Skip.self)) != nil {
                bad += 1
            } else {
                break
            }
        }
        items = out
        dropped = bad
    }
}

extension Array where Element: Identifiable {
    func uniqued(_ name: String, _ bad: inout [String]) -> [Element] {
        var seen: Set<Element.ID> = []
        let out = filter { seen.insert($0.id).inserted }
        if out.count < count { bad.append("\(name): \(count - out.count) duplicate") }
        return out
    }
}

extension KeyedDecodingContainer {


    func lenient<T: Decodable>(_ type: T.Type, _ key: Key, _ bad: inout [String]) -> T? {
        do {
            return try decodeIfPresent(type, forKey: key)
        } catch {
            bad.append(key.stringValue)
            return nil
        }
    }


    func lenientArray<T: Decodable>(_ type: T.Type, _ key: Key, _ bad: inout [String]) -> [T]? {
        guard let list = lenient(Lossy<T>.self, key, &bad) else { return nil }
        if list.dropped > 0 { bad.append("\(key.stringValue): \(list.dropped) unreadable") }
        return list.items
    }
}



struct HostSnap: Decodable {
    let cpu: Int?
    let mem_used_gb: Double?
    let mem_total_gb: Int?
    let swap_used_gb: Double?
    let load: [String]?
    let temp_c: Int?
    let fan_rpm: Int?
    let uptime_h: Int?
    let disk_pct: Int?
    let net_fail_min: Int?
    var merge_publisher: MergePublisher? = nil

    var memFraction: Double? {
        guard let used = mem_used_gb, let total = mem_total_gb, total > 0 else { return nil }
        return used / Double(total)
    }
}

struct MergePublisher: Decodable, Equatable {
    let timer: String?
    let load: String?
    let dry_run: Bool?
}

struct RigStatus: Decodable, Equatable {
    let storage_state: String
    let guest_free_gib: Double?
    let srv_vm_free_gib: Double?
    let reboot_needed: Bool?
    let updated_at: String
    var checked_at: String? = nil
    var last_cleanup: String? = nil
    var cleanup_debt_gib: Double? = nil

    var updated: Date? { clockDate(updated_at) }
}

struct VMSnap: Decodable {
    let mem_used_gb: Double?
    let mem_total_gb: Int?
    let load: [String]?
    let cpus: Int?
    let disk_pct: Int?
    let orch: String?
    let orch_token_expires: Int?
    let claude_sessions: Int?
    let claude_working: Int?
    let codex_sessions: Int?
    let codex_working: Int?
}

struct GuestSnap: Decodable, Identifiable {
    let name: String
    let kind: String
    let pr: Int?
    let cpu_pct: Int?
    let cpus: Int?
    let mem_gb: Int?
    let rss_gb: Double?

    let up_s: Int?

    var run: Int? = nil
    var id: String { name }
    var isCI: Bool { kind != "agents" }
    var repo: String? { kind == "backend CI" ? "backend" : kind == "web CI" ? "web" : nil }
}


struct AgentSnap: Decodable, Identifiable {
    let title: String?
    let project: String?
    let status: String?
    let provider: String?
    let model: String?
    let threadId: String?
    let updated: String?
    let pr: String?
    let pr_key: String?
    let issue_id: String?
    let agent_kind: String?

    enum CodingKeys: String, CodingKey { case title, project, status, provider, model, updated, pr, pr_key, issue_id, agent_kind, threadId = "id" }

    var id: String { threadId ?? ((title ?? "") + "|" + (project ?? "")) }
    var name: String { title ?? "Untitled thread" }
    var waiting: Bool { status == "waiting" }
    var inTurn: Bool { status == "running" || status == "starting" }
    var isCodex: Bool { provider == "codex" }

    var prKey: String? {

        if let pr, let u = URLComponents(string: pr), u.host?.lowercased() == "github.com" {
            let parts = u.path.split(separator: "/").map(String.init)
            if parts.count == 4, parts[0].lowercased() == SiteConfig.shared.owner.lowercased(), parts[2] == "pull", Int(parts[3]) != nil,
               let key = SiteConfig.shared.repoKey(parts[1]) {
                return key + "#" + parts[3]
            }
        }

        if let pr_key { return pr_key }
        guard let t = title else { return nil }
        for (key, name) in SiteConfig.shared.repos {
            if let r = t.range(of: NSRegularExpression.escapedPattern(for: name) + #" #\d+"#, options: .regularExpression) {
                return key + "#" + String(t[r].split(separator: " ")[1].dropFirst())
            }
        }
        return nil
    }
    var issue: String? { issue_id ?? issueId(in: title) }
    var kind: AgentKind { agent_kind.flatMap(AgentKind.init(rawValue:)) ?? AgentKind.of(name) }
}

extension AgentSnap {
    static let states: Set<String> = ["running", "starting", "waiting"]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        project = try c.decodeIfPresent(String.self, forKey: .project)
        status = try c.decodeIfPresent(String.self, forKey: .status)
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        threadId = try c.decodeIfPresent(String.self, forKey: .threadId)
        updated = try c.decodeIfPresent(String.self, forKey: .updated)
        pr = try c.decodeIfPresent(String.self, forKey: .pr)
        pr_key = try c.decodeIfPresent(String.self, forKey: .pr_key)
        issue_id = try c.decodeIfPresent(String.self, forKey: .issue_id)
        agent_kind = try c.decodeIfPresent(String.self, forKey: .agent_kind)
        guard threadId?.isEmpty == false || title?.isEmpty == false, status.map(Self.states.contains) == true else {
            throw DecodingError.dataCorruptedError(forKey: .status, in: c, debugDescription: "no identity or known state")
        }
    }
}

struct RigCIHealth: Decodable {
    let watch_age_s: Int?
    let web_age_s: Int?
    let heavy_job: Bool?
    let health_age_s: Int?

    let heavy_owner: String?

    var lanes: CILanes? = nil
    var watch: CIHeartbeat? = nil
    var web: CIHeartbeat? = nil
}

struct CIHeartbeat: Decodable {
    struct Row: Decodable {
        let repo: String
        let pr: Int?
        let job: String
        let queued_at: String?
        let state: String
        let why: String?
    }
    let age_s: Int?
    let phase: String?
    let detail: String?
    var queue: [Row]? = nil
}

struct QueuedJob: Decodable, Equatable {
    let job: String
    let queued_at: String?
    let id: Int?
}

struct CILanes: Decodable {
    struct Backend: Decodable {
        let fast_s: Int?
        let integration_s: Int?
        var fast_queue_s: Int? = nil
        var integration_queue_s: Int? = nil
        let last_done: Int?

        let guests: [String: String]?
    }
    struct Lane: Decodable {
        let run_s: Int?
        let last_done: Int?
        var queue_s: Int? = nil
    }
    let backend: Backend?
    let web: Lane?
}

struct VMLocalSnap: Decodable {
    var vm: VMSnap?
    var agents: [AgentSnap]?
    var rigStatus: RigStatus?
    var collected: Double?
    var vmError: String?
    var agentsError: String?
    var unreadable: [String]

    enum CodingKeys: String, CodingKey { case vm, agents, rig_status, collected, vm_error, agents_error }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []
        vm = c.lenient(VMSnap.self, .vm, &bad)
        agents = c.lenientArray(AgentSnap.self, .agents, &bad)?.uniqued("agents", &bad)
        rigStatus = c.lenient(RigStatus.self, .rig_status, &bad)
        collected = c.lenient(Double.self, .collected, &bad)
        vmError = c.lenient(String.self, .vm_error, &bad)
        agentsError = c.lenient(String.self, .agents_error, &bad)
        unreadable = bad
    }
}

struct RigSnap: Decodable {
    var host: HostSnap?
    var vm: VMSnap?
    var vms: [GuestSnap]

    var vmsKnown: Bool
    var agents: [AgentSnap]?
    var rigStatus: RigStatus?
    var rigStatusError: String?
    var ci: RigCIHealth?
    var collected: Double?
    var error: String?
    var hostError: String?
    var vmError: String?
    var agentsError: String?
    var vmSkipped: Bool
    var unreadable: [String]

    enum CodingKeys: String, CodingKey { case host, vm, vms, vms_complete, vms_unreadable, agents, rig_status, rig_status_error, ci, collected, error, host_error, vm_error, agents_error, vm_skipped }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []
        host = c.lenient(HostSnap.self, .host, &bad)
        vm = c.lenient(VMSnap.self, .vm, &bad)
        let guestList = c.lenientArray(GuestSnap.self, .vms, &bad)?.uniqued("vms", &bad)
        vms = guestList ?? []
        let collectorDropped = c.lenient(Int.self, .vms_unreadable, &bad) ?? 0
        if collectorDropped > 0 { bad.append("vms: \(collectorDropped) unreadable") }
        let scanned = c.lenient(Bool.self, .vms_complete, &bad) ?? false
        vmsKnown = guestList != nil && scanned && !bad.contains { $0 == "vms_unreadable" || $0.hasPrefix("vms") }
        if guestList == nil && !bad.contains(where: { $0.hasPrefix("vms") }) { bad.append("CI guests: not reported") }
        agents = c.lenientArray(AgentSnap.self, .agents, &bad)?.uniqued("agents", &bad)
        rigStatus = c.lenient(RigStatus.self, .rig_status, &bad)
        rigStatusError = c.lenient(String.self, .rig_status_error, &bad) ?? (bad.contains("rig_status") ? "rig-status.json unreadable" : nil)
        ci = c.lenient(RigCIHealth.self, .ci, &bad)
        collected = c.lenient(Double.self, .collected, &bad)
        error = c.lenient(String.self, .error, &bad)
        hostError = c.lenient(String.self, .host_error, &bad)
        vmError = c.lenient(String.self, .vm_error, &bad)
        agentsError = c.lenient(String.self, .agents_error, &bad)
        vmSkipped = c.lenient(Bool.self, .vm_skipped, &bad) ?? false
        unreadable = bad
    }
}



struct RepoRow: Decodable, Identifiable {
    let name: String
    let open: Int
    let drafts: Int?
    let open_total: Int?
    let truncated: Bool?
    let merged_24h: Int?
    let main: String?
    let main_failed_checks: Int?
    let main_running: Bool?
    var main_queue: [QueuedJob]? = nil
    var id: String { name }
    var label: String { repoLabel(name) }
}

struct Merge: Decodable, Identifiable {
    let repo: String
    let number: Int
    let title: String
    let merged_at: String
    let issue: String?

    let created: String?
    var branch: String? = nil
    var id: String { repo + "#" + String(number) }
    var mergedDate: Date? { isoDate(merged_at) }
    var createdDate: Date? { isoDate(created) }
}


struct PRRow: Decodable, Identifiable {
    let repo: String
    let number: Int
    let title: String
    let issue: String?
    let ci: String
    let failed_checks: Int?
    let review: String
    let updated: String
    let created: String
    let mergeable: String?
    let merge_state: String?
    let blocker: String?
    let ready_since: String?
    let failing_since: String?
    let head: String?
    let checks_running: Bool?

    let ci_waiting: Bool?

    var flaky: [String]? = nil

    var branch: String? = nil

    var runs: [Int]? = nil


    var queue_tier: Int? = nil
    var queued_at: String? = nil
    var queued_jobs: [QueuedJob]? = nil


    let merge_ready: Bool?

    var id: String { repo + "#" + String(number) }
    var url: URL { prURL(repo, number) }
    var repoLabelText: String { repoLabel(repo) }
    var approved: Bool { ci == "passed" && review == "approved" }
    var mergeReady: Bool { approved && merge_ready == true }
    var waitingForReview: Bool { ci == "passed" && review != "approved" }
    var readyDate: Date? { isoDate(ready_since) }
    var failingDate: Date? { isoDate(failing_since) }
}

struct RepoSnap: Decodable {
    var repos: [RepoRow]?
    var recent: [Merge]
    var prs: [PRRow]?
    var fetched: Double?
    var error: String?
    var staleReason: String?

    var mergeStateKnown: Bool?
    var githubBudget: Cached<GitHubBudget>?
    var unreadable: [String]



    static let configured: Set<String> = ["backend", "web", "ios"]
    var repositoriesComplete: Bool { Self.configured.isSubset(of: (repos ?? []).map(\.name)) }
    var repoRowsComplete: Bool { repos != nil && !unreadable.contains { $0 == "repos" || $0.hasPrefix("repos:") } }
    var prRowsComplete: Bool { prs != nil && !unreadable.contains { $0 == "prs" || $0.hasPrefix("prs:") } }
    var openInventoryComplete: Bool {
        guard repoRowsComplete, prRowsComplete, repositoriesComplete, let rows = repos, rows.allSatisfy({ $0.truncated == false }) else { return false }
        let listed = prs ?? []
        return rows.allSatisfy { r in r.open == listed.filter { $0.repo == r.name }.count }
            && listed.allSatisfy { p in rows.contains { $0.name == p.repo } }
    }
    var inventoryNote: String {
        var notes: [String] = []
        if !repoRowsComplete { notes.append("repository inventory missing or unreadable") }
        if !prRowsComplete { notes.append("open PR inventory missing or unreadable") }
        if repoRowsComplete && !repositoriesComplete { notes.append("repository missing from the inventory") }
        if (repos ?? []).contains(where: { $0.truncated != false }) { notes.append("open list incomplete or completeness unknown") }
        else if repoRowsComplete && prRowsComplete && repositoriesComplete && !openInventoryComplete { notes.append("repository counts disagree with the open list") }
        return notes.isEmpty ? "" : "partial: " + notes.joined(separator: "; ") + "; observed counts only"
    }

    enum CodingKeys: String, CodingKey { case repos, recent, prs, fetched, error, stale_reason, merge_state_known, github_budget }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []
        repos = c.lenientArray(RepoRow.self, .repos, &bad)?.uniqued("repos", &bad)
        recent = c.lenientArray(Merge.self, .recent, &bad)?.uniqued("recent", &bad) ?? []
        prs = c.lenientArray(PRRow.self, .prs, &bad)?.uniqued("prs", &bad)
        fetched = c.lenient(Double.self, .fetched, &bad)
        error = c.lenient(String.self, .error, &bad)
        staleReason = c.lenient(String.self, .stale_reason, &bad)
        mergeStateKnown = c.lenient(Bool.self, .merge_state_known, &bad)
        githubBudget = c.lenient(Cached<GitHubBudget>.self, .github_budget, &bad)
        unreadable = bad
    }
}



struct MacServices: Decodable {
    let ios_watch_age_s: Int?
    let light_age_s: Int?
    let watcher_loaded: Bool?
    let light_loaded: Bool?
    var ios_beat_age_s: Int? = nil
    var ios_watch_phase: String? = nil
    var ios_watch_detail: String? = nil
}

struct IOSJob: Decodable {
    let repo: String?
    let pr: Int?

    var main: Bool? = nil
    let running_s: Int?

    var repoKey: String {
        let r = repo ?? SiteConfig.shared.repoName("ios")
        return SiteConfig.shared.repoKey(r) ?? r
    }
}

extension IOSJob {
    enum CodingKeys: String, CodingKey { case repo, pr, main, running_s }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        repo = try c.decodeIfPresent(String.self, forKey: .repo)
        pr = try c.decodeIfPresent(Int.self, forKey: .pr)
        main = try c.decodeIfPresent(Bool.self, forKey: .main)
        running_s = try c.decodeIfPresent(Int.self, forKey: .running_s)
        guard repo?.isEmpty == false else {
            throw DecodingError.dataCorruptedError(forKey: .repo, in: c, debugDescription: "no repository")
        }
    }
}

struct GitHubBudget: Decodable {
    let core: Int
    let core_limit: Int
    let core_reset: Int
    let graphql: Int?
}

struct CodexUsage: Decodable {
    let used_pct: Double
    let resets_at: Int?
    let window_min: Int?
    var isWeekly: Bool { window_min == 7 * 24 * 60 }
}


struct Cached<T: Decodable>: Decodable {
    let value: T?
    let fetched: Double?
    let error: String?
    var fetchedDate: Date? { fetched.map { Date(timeIntervalSince1970: $0) } }
}

struct AgentCounts: Decodable {
    let claude_sessions: Int?
    let claude_working: Int?
    let codex_sessions: Int?
    let codex_working: Int?

    static let limit = 100_000

    var working: Int? {
        func plausible(_ working: Int, _ sessions: Int?) -> Bool {
            (0...Self.limit).contains(working) && working <= sessions ?? Self.limit
        }
        guard let c = claude_working, let x = codex_working, plausible(c, claude_sessions), plausible(x, codex_sessions) else { return nil }
        let sum = c.addingReportingOverflow(x)
        return sum.overflow ? nil : sum.partialValue
    }
}

struct MacSnap: Decodable {
    var iosCI: [IOSJob]?
    var iosLane: CILanes.Lane?
    var services: MacServices?
    var github: Cached<GitHubBudget>?
    var codex: Cached<CodexUsage>?
    var agents: AgentCounts?
    var errors: [String: String]
    var collected: Double?
    var unreadable: [String]

    enum CodingKeys: String, CodingKey { case ios_ci, ios_lane, services, github, codex, agents, errors, collected }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []

        let jobs = c.lenient(Lossy<IOSJob>.self, .ios_ci, &bad)
        if let jobs, jobs.dropped > 0 { bad.append("ios_ci: \(jobs.dropped) unreadable") }
        iosCI = jobs.flatMap { $0.dropped == 0 ? $0.items : nil }
        iosLane = c.lenient(CILanes.Lane.self, .ios_lane, &bad)
        services = c.lenient(MacServices.self, .services, &bad)
        github = c.lenient(Cached<GitHubBudget>.self, .github, &bad)
        codex = c.lenient(Cached<CodexUsage>.self, .codex, &bad)
        agents = c.lenient(AgentCounts.self, .agents, &bad)
        errors = c.lenient([String: String].self, .errors, &bad) ?? [:]
        collected = c.lenient(Double.self, .collected, &bad)
        unreadable = bad
    }
}



struct StageCounts: Decodable {
    let backlog: Int
    let todo: Int
    let in_progress: Int
    let in_review: Int
    let done: Int
    let scope: Int
    let pct_done: Int
    var remaining: Int { scope - done }
    var inFlight: Int { in_progress + in_review }
    var dict: [String: Int] { ["done": done, "in_review": in_review, "in_progress": in_progress, "todo": todo, "backlog": backlog] }
}

struct FlightIssue: Decodable, Identifiable {
    let id: String
    let title: String
    let url: String
    let stage: String
    let project: String?
    let updated: String?
    let completed: String?
    var link: URL? { URL(string: url) }
}

struct PlanProject: Decodable, Identifiable {
    let id: String
    let name: String
    let url: String?
    let target_date: String?
    let status: String?
    let backlog: Int
    let todo: Int
    let in_progress: Int
    let in_review: Int
    let done: Int
    let scope: Int
    let pct_done: Int
    var counts: [String: Int] { ["done": done, "in_review": in_review, "in_progress": in_progress, "todo": todo, "backlog": backlog] }
    var link: URL? { url.flatMap(URL.init(string:)) }
}

struct PlanInitiative: Decodable, Identifiable {
    let id: String
    let name: String
    let url: String?
    let target_date: String?
    let status: String?
    let project_ids: [String]
    let backlog: Int
    let todo: Int
    let in_progress: Int
    let in_review: Int
    let done: Int
    let scope: Int
    let pct_done: Int
    var counts: [String: Int] { ["done": done, "in_review": in_review, "in_progress": in_progress, "todo": todo, "backlog": backlog] }

    var number: Int? { splitInitiativeName(name).number }
    var title: String { splitInitiativeName(name).title }
    var link: URL? { url.flatMap(URL.init(string:)) }
}

func splitInitiativeName(_ name: String) -> (number: Int?, title: String) {
    let pieces = name.split(separator: "·", maxSplits: 1)
    if pieces.count == 2, let n = Int(pieces[0].trimmingCharacters(in: .whitespaces)) {
        let title = pieces[1].trimmingCharacters(in: .whitespaces)
        if !title.isEmpty { return (n, title) }
    }
    return (nil, name)
}


struct OwnerAction: Decodable, Identifiable {
    let id: String
    let title: String
    let url: String
    let updated: String?
    let created: String?
    let due: String?
    let stage: String?
    let priority: Int?
    let project_id: String?
    var link: URL? { URL(string: url) }

    var pressing: Bool { (priority ?? 0) == 1 || (priority ?? 0) == 2 }
}


struct Forecast: Decodable {
    struct Point: Decodable, Identifiable {
        let t: String
        let done: Int
        let scope: Int
        var id: String { t }
        var date: Date { ymdDate(t) ?? Date() }
    }
    let scope: Int
    let done: Int
    let remaining: Int
    let per_week: Double
    let sample_days: Int?
    let active_days: Int?
    let undated_done: Int?
    let target: String?
    let burnup: [Point]
    let burnup_reconstructed: Bool?
    let horizon_days: Int?
    let insufficient: Bool
    let p50: String?
    let p85: String?
    let prob_by_target: Double?

    let prob_note: String?
    let runs: Int?
}


struct Drift: Decodable {
    let since: String
    let scope_change: Int?
    let done_change: Int?
    let p50_days: Int?
}

struct LaunchInfo: Decodable {
    var name: String?
    var source: String?
    var projectIds: [String]
    var target: String?
    var targetSource: String?
    var version: String?
    var counts: StageCounts?
    var forecast: Forecast?
    var drift: Drift?
    var error: String?
    var overrideError: String?

    enum CodingKeys: String, CodingKey {
        case name, source, project_ids, target, target_source, version, counts, forecast, drift, error, override_error
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []
        name = c.lenient(String.self, .name, &bad)
        source = c.lenient(String.self, .source, &bad)
        projectIds = c.lenient([String].self, .project_ids, &bad) ?? []
        target = c.lenient(String.self, .target, &bad)
        targetSource = c.lenient(String.self, .target_source, &bad)
        version = c.lenient(String.self, .version, &bad)
        counts = c.lenient(StageCounts.self, .counts, &bad)
        forecast = c.lenient(Forecast.self, .forecast, &bad)
        drift = c.lenient(Drift.self, .drift, &bad)
        error = c.lenient(String.self, .error, &bad)
        overrideError = c.lenient(String.self, .override_error, &bad)
        if error == nil && !bad.isEmpty { error = "unreadable: " + bad.joined(separator: ", ") }
    }
}

struct LinearSnap: Decodable {
    var scope: Int?
    var backlog: Int?
    var todo: Int?
    var inProgress: Int?
    var inReview: Int?
    var done: Int?
    var pctDone: Int?
    var done7d: Int?
    var unassigned: StageCounts?
    var initiatives: [PlanInitiative]
    var projects: [PlanProject]
    var otherProjectIds: [String]
    var inFlight: [FlightIssue]
    var inFlightTotal: Int?
    var recentDone: [FlightIssue]

    var index: [String: [String?]]
    var ownerActions: [OwnerAction]
    var ownerActionsTotal: Int?
    var launch: LaunchInfo?

    var warnings: [String]
    var fetched: Double?
    var error: String?
    var staleReason: String?
    var unreadable: [String]

    enum CodingKeys: String, CodingKey {
        case scope, backlog, todo, in_progress, in_review, done, pct_done, done_7d, unassigned, initiatives, projects
        case other_project_ids, in_flight, in_flight_total, recent_done, index, owner_actions, owner_actions_total
        case launch, warnings, fetched, error, stale_reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []
        scope = c.lenient(Int.self, .scope, &bad)
        backlog = c.lenient(Int.self, .backlog, &bad)
        todo = c.lenient(Int.self, .todo, &bad)
        inProgress = c.lenient(Int.self, .in_progress, &bad)
        inReview = c.lenient(Int.self, .in_review, &bad)
        done = c.lenient(Int.self, .done, &bad)
        pctDone = c.lenient(Int.self, .pct_done, &bad)
        done7d = c.lenient(Int.self, .done_7d, &bad)
        unassigned = c.lenient(StageCounts.self, .unassigned, &bad)
        initiatives = c.lenientArray(PlanInitiative.self, .initiatives, &bad) ?? []
        projects = c.lenientArray(PlanProject.self, .projects, &bad) ?? []
        otherProjectIds = c.lenient([String].self, .other_project_ids, &bad) ?? []
        inFlight = c.lenientArray(FlightIssue.self, .in_flight, &bad)?.uniqued("in_flight", &bad) ?? []
        inFlightTotal = c.lenient(Int.self, .in_flight_total, &bad)
        recentDone = c.lenientArray(FlightIssue.self, .recent_done, &bad)?.uniqued("recent_done", &bad) ?? []
        index = c.lenient([String: [String?]].self, .index, &bad) ?? [:]
        ownerActions = c.lenientArray(OwnerAction.self, .owner_actions, &bad)?.uniqued("owner_actions", &bad) ?? []
        ownerActionsTotal = c.lenient(Int.self, .owner_actions_total, &bad)
        launch = c.lenient(LaunchInfo.self, .launch, &bad)
        warnings = c.lenient([String].self, .warnings, &bad) ?? []
        fetched = c.lenient(Double.self, .fetched, &bad)
        error = c.lenient(String.self, .error, &bad)
        staleReason = c.lenient(String.self, .stale_reason, &bad)
        unreadable = bad
    }

    var countsComplete: Bool {
        guard let scope, let done, let inReview, let inProgress, let todo, let backlog else { return false }
        return scope == done + inReview + inProgress + todo + backlog
    }
    var ok: Bool { error == nil && countsComplete }
    var counts: [String: Int] {
        ["done": done ?? 0, "in_review": inReview ?? 0, "in_progress": inProgress ?? 0, "todo": todo ?? 0, "backlog": backlog ?? 0]
    }
    func project(_ id: String) -> PlanProject? { projects.first { $0.id == id } }
    func issueTitle(_ id: String) -> String? { (index[id]?.first ?? nil) }
    func issueProjectId(_ id: String) -> String? {
        guard let row = index[id], row.count > 3 else { return nil }
        return row[3]
    }

    func isLaunch(_ issue: String?) -> Bool {
        guard let issue, let pid = issueProjectId(issue), let launch else { return false }
        return launch.projectIds.contains(pid)
    }
}



enum Tab: Int, CaseIterable {



    case mission = 4, charts = 5, models = 6
    var title: String { [.mission: "Mission", .charts: "Charts", .models: "Models"][self]! }
}


enum Health: Int, Comparable {
    case ok = 0, unknown, warn, bad
    static func < (a: Health, b: Health) -> Bool { a.rawValue < b.rawValue }
}


struct Check: Identifiable {
    let id: String
    let label: String
    let detail: String
    let health: Health
    let source: SourceID

    var alert = false
    var tab: Tab = .mission
    var url: URL? = nil


    var observedAt: Date? = nil



    var recovers = true
}


enum SourceID: String, CaseIterable, Codable {
    case rig, agents, mac, repos, linear

    var label: String {
        switch self {
        case .rig: return "Rig"
        case .agents: return "Agents VM"
        case .mac: return "Mac"
        case .repos: return "GitHub"
        case .linear: return "Linear"
        }
    }

    var every: TimeInterval {
        switch self {
        case .rig, .agents: return 20
        case .mac: return 30

        case .repos: return 900
        case .linear: return 900
        }
    }


    static let vmLocalStaleAfter: TimeInterval = 300

    var staleAfter: TimeInterval {
        switch self {
        case .rig, .agents: return 90
        case .mac: return 120
        case .repos: return 1800
        case .linear: return 3600
        }
    }
}



func repoLabel(_ key: String) -> String { ["backend": "Backend", "web": "Web", "ios": "iOS"][key] ?? key }
func prURL(_ repo: String, _ n: Int) -> URL { URL(string: "\(SiteConfig.shared.repoURL(repo))/pull/\(n)")! }
func pullsURL(_ repo: String) -> URL { URL(string: "\(SiteConfig.shared.repoURL(repo))/pulls")! }
func mainChecksURL(_ repo: String) -> URL {
    URL(string: "\(SiteConfig.shared.repoURL(repo))/actions?query=branch%3Amain")!
}

/// Site-specific names, read from the same local JSON as the collectors (see README, Configuration).
struct SiteConfig {
    var owner = "example-org"
    var repos = ["backend": "example-backend", "web": "example-web", "ios": "example-ios"]
    var linearWorkspace = "example"
    var teamKey = "ENG"

    static let shared = SiteConfig.load()

    static func load(env: [String: String] = ProcessInfo.processInfo.environment) -> SiteConfig {
        var c = SiteConfig()
        let home = URL(fileURLWithPath: NSHomeDirectory())
        let paths = env["FLIGHTOPS_CONFIG"].map { [URL(fileURLWithPath: $0)] }
            ?? [home.appendingPathComponent(".config/flightops/config.json")]
        for url in paths {
            guard let data = try? Data(contentsOf: url),
                  let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            if let v = o["github_owner"] as? String { c.owner = v }
            if let v = o["repos"] as? [String: String] { c.repos = v }
            if let v = o["linear_workspace"] as? String { c.linearWorkspace = v }
            if let v = o["linear_team_key"] as? String { c.teamKey = v }
            break
        }
        return c
    }

    var teamKeyPattern: String { NSRegularExpression.escapedPattern(for: teamKey) }
    func repoName(_ key: String) -> String { repos[key] ?? key }
    func repoKey(_ name: String) -> String? {
        if repos[name] != nil { return name }
        return repos.first { $0.value.lowercased() == name.lowercased() }?.key
    }
    func repoURL(_ key: String) -> String { "https://github.com/\(owner)/\(repoName(key))" }
    var githubURL: URL { URL(string: "https://github.com/\(owner)")! }
    var linearURL: URL { URL(string: "https://linear.app/\(linearWorkspace)")! }
}


func issueId(in text: String?) -> String? {
    guard let text, let r = text.range(of: #"\b"# + SiteConfig.shared.teamKeyPattern + #"-\d+"#, options: [.regularExpression, .caseInsensitive]) else { return nil }
    return text[r].uppercased()
}


func duration(_ seconds: Int) -> String {
    let s = max(0, seconds)
    if s < 60 { return "\(s)s" }
    if s < 3600 { return "\(s / 60)m" }
    if s < 86400 { return "\(s / 3600)h" }
    return "\(s / 86400)d"
}


func clock(_ seconds: Int) -> String {
    let s = max(0, seconds)
    return s < 600 ? String(format: "%d:%02d", s / 60, s % 60) : duration(s)
}


func ago(_ date: Date, now: Date = Date()) -> String { duration(Int(now.timeIntervalSince(date))) }


func age(_ iso: String?) -> String? {
    guard let d = isoDate(iso) else { return nil }
    return ago(d)
}

private let isoFractional: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()
private let isoPlain = ISO8601DateFormatter()

func isoDate(_ s: String?) -> Date? {
    guard let s else { return nil }
    return isoFractional.date(from: s) ?? isoPlain.date(from: s)
}

func clockDate(_ s: String?) -> Date? {
    guard let s, let fraction = s.range(of: #"\.\d+"#, options: .regularExpression) else { return isoDate(s) }
    return isoDate(s.replacingCharacters(in: fraction, with: ""))?.addingTimeInterval(Double("0" + s[fraction]) ?? 0)
}


func ymdDate(_ s: String?) -> Date? {
    guard let s, s.count >= 10 else { return nil }
    let parts = s.prefix(10).split(separator: "-")
    guard parts.count == 3, let y = Int(parts[0]), let m = Int(parts[1]), let d = Int(parts[2]) else { return nil }
    var comps = DateComponents()
    comps.year = y
    comps.month = m
    comps.day = d
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = Calendar.current.timeZone
    guard let date = calendar.date(from: comps) else { return nil }
    let back = calendar.dateComponents([.year, .month, .day], from: date)
    return back.year == y && back.month == m && back.day == d ? date : nil
}


func daysUntil(_ date: Date, now: Date = Date()) -> Int {
    let cal = Calendar.current
    return cal.dateComponents([.day], from: cal.startOfDay(for: now), to: cal.startOfDay(for: date)).day ?? 0
}

private let monthDay: DateFormatter = {
    let f = DateFormatter()
    f.setLocalizedDateFormatFromTemplate("MMMd")
    return f
}()
private let monthDayYear: DateFormatter = {
    let f = DateFormatter()
    f.setLocalizedDateFormatFromTemplate("MMMdyy")
    return f
}()
private let monthYear: DateFormatter = {
    let f = DateFormatter()
    f.setLocalizedDateFormatFromTemplate("MMMyyyy")
    return f
}()


private func zoned(_ formatter: DateFormatter) -> DateFormatter {
    formatter.timeZone = Calendar.current.timeZone
    return formatter
}

func shortDate(_ date: Date, now: Date = Date()) -> String {
    let cal = Calendar.current
    return zoned(cal.component(.year, from: date) == cal.component(.year, from: now) ? monthDay : monthDayYear).string(from: date)
}

func shortDate(_ ymd: String?) -> String? { ymdDate(ymd).map { shortDate($0) } }


func monthAndYear(_ date: Date) -> String { zoned(monthYear).string(from: date) }


func targetText(_ ymd: String?) -> (String, Int)? {
    guard let d = ymdDate(ymd) else { return nil }
    return (shortDate(d), daysUntil(d))
}


func chanceText(_ pct: Double) -> String {
    if pct < 1 { return "<1%" }
    if pct > 99 { return ">99%" }
    return "\(Int(pct.rounded()))%"
}


struct RepeatedFailures: Equatable {
    struct Entry: Equatable {
        let repo: String
        let number: Int
        let head: String
        let failed: Int?
        let read: Bool
        let readAt: Double?
    }

    let generated: Double
    let prs: [Entry]

    static let schema = "flightops-repeated-failures/1"
    static let threshold = 3
    static let headChars = 40
    static let limit: TimeInterval = 900
    static let readLimit: TimeInterval = 7200
    static let malformed = "repeated-failure report unreadable"

    static func parse(_ value: [String: Any]) -> RepeatedFailures? {
        guard value["schema"] as? String == schema, !(value["generated"] is Bool), let generated = (value["generated"] as? NSNumber)?.doubleValue,
              generated.isFinite, generated > 0, let rows = value["prs"] as? [[String: Any]] else { return nil }
        var seen = Set<String>()
        var out: [Entry] = []
        for row in rows {
            guard let repo = row["repo"] as? String, RepoSnap.configured.contains(repo), let n = row["number"] as? Int, n > 0, !(row["number"] is Bool),
                  let head = row["head"] as? String, head.count == headChars, head.allSatisfy({ $0.isHexDigit && !$0.isUppercase }),
                  let read = row["read"] as? Bool, seen.insert("\(repo)#\(n)").inserted else { return nil }
            let failed = row["failed"] as? Int
            guard read ? (failed ?? -1) >= 0 : row["failed"] is NSNull || row["failed"] == nil else { return nil }
            let readAt = (row["read_at"] as? NSNumber).flatMap { row["read_at"] is Bool ? nil : $0.doubleValue }
            guard read ? (readAt.map { $0.isFinite && $0 > 0 } ?? false) : (row["read_at"] == nil || row["read_at"] is NSNull) else { return nil }
            out.append(Entry(repo: repo, number: n, head: head, failed: read ? failed : nil, read: read, readAt: read ? readAt : nil))
        }
        return RepeatedFailures(generated: generated, prs: out)
    }
}
