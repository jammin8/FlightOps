import Foundation






enum Freshness { case fresh, stale, missing }




func eventLine(_ kind: String, _ data: Data, at t: Date) -> Data? {
    guard let obj = try? JSONSerialization.jsonObject(with: data, options: .fragmentsAllowed) else { return nil }
    let record: [String: Any] = ["t": Int(t.timeIntervalSince1970), "kind": kind, "data": obj]
    guard JSONSerialization.isValidJSONObject(record),
          var line = try? JSONSerialization.data(withJSONObject: record, options: [.withoutEscapingSlashes]) else { return nil }
    line.append(0x0A)
    return line
}


let clockSkew: TimeInterval = 5


struct SourceStatus {

    var sampleAt: Date?


    var observedAt: Date?

    var failures = 0

    var unreachable = false
    var error: String?

    var note: String?

    var refreshFailed = false

    var staleAfter: TimeInterval? = nil

    var origin: String? = nil

    var expiresAt: Date? = nil

    func limit(_ id: SourceID) -> TimeInterval { staleAfter ?? id.staleAfter }

    func freshness(_ id: SourceID, now: Date) -> Freshness {
        guard let at = sampleAt else { return .missing }
        if refreshFailed { return .stale }
        if let expiresAt, now > expiresAt { return .stale }
        let age = now.timeIntervalSince(at)

        return age > limit(id) || age < -clockSkew ? .stale : .fresh
    }
}


struct Sample: Identifiable {
    let id = UUID()
    let t: Date
    let cpu: Int
    let mem: Double?
    let temp: Int?
}


struct World {
    var now = Date()
    var status: [SourceID: SourceStatus] = [:]
    var host: HostSnap?
    var guests: [GuestSnap] = []
    var rigCI: RigCIHealth?
    var vm: VMSnap?
    var agents: [AgentSnap]?
    var agentsAt: Date?
    var mac: MacSnap?
    var repos: RepoSnap?
    var linear: LinearSnap?
    var timelines: [String: PRTimeline] = [:]
    var guestsKnown = true
    var rigStatus: RigStatus? = nil
    var feed: Feed? = nil
    var storyProblem: String? = nil
    var storyRefused = 0
    var repeatedFailures: RepeatedFailures? = nil

    func freshness(_ s: SourceID) -> Freshness { (status[s] ?? SourceStatus()).freshness(s, now: now) }
    func fresh(_ s: SourceID) -> Bool { freshness(s) == .fresh }

    var agentsKnown: Bool {
        guard fresh(.agents), agents != nil, let at = agentsAt else { return false }
        let age = now.timeIntervalSince(at)
        return age <= (status[.agents] ?? SourceStatus()).limit(.agents) && age >= -clockSkew
    }

    var rigUnreachable: Bool {
        let s = status[.rig] ?? SourceStatus()
        return !(fresh(.rig) && host != nil) && s.unreachable && s.failures >= 2
    }

    func elapsed(_ s: SourceID) -> Int {
        guard let at = status[s]?.sampleAt else { return 0 }
        return max(0, Int(now.timeIntervalSince(at)))
    }
}



enum Rules {
    static let heartbeatWarn = 300
    static let heartbeatBad = 1800
    static let busyMax = 5400


    static func checks(_ w: World) -> [Check] {
        var c: [Check] = [rig(w), storage(w), rigState(w), backendCI(w), webCI(w), iosCI(w), lightCI(w), orch(w), orchToken(w), vmDisk(w),
                          github(w), codex(w)]
        c += mainBranches(w)
        if let f = feed(w) { c.append(f) }
        if let s = w.storyProblem { c.append(Check(id: "pr-story", label: "PR history", detail: s, health: .warn, source: .repos)) }
        return c.map { stamped($0, w) }
    }

    static func feed(_ w: World) -> Check? {
        guard let f = w.feed else { return nil }
        let id = "vm-feed", label = "VM feed", fallback = "collecting on this Mac"
        let age = f.publishedAt.map { ago($0, now: w.now) } ?? "?"
        switch f.phase(now: w.now) {
        case "fresh":
            let local = [SourceID.repos, .agents].filter { !f.owns($0, now: w.now) }.map(\.label)
            if local.isEmpty { return Check(id: id, label: label, detail: "snapshot \(age) old", health: .ok, source: .agents) }
            return Check(id: id, label: label, detail: local.joined(separator: ", ") + " " + fallback, health: .warn, source: .agents)
        case "stale": return Check(id: id, label: label, detail: "snapshot \(age) old · " + fallback, health: .warn, source: .agents)
        case "invalid": return Check(id: id, label: label, detail: "snapshot invalid · " + fallback, health: .warn, source: .agents)
        default: return Check(id: id, label: label, detail: "no snapshot · " + fallback, health: .unknown, source: .agents)
        }
    }



    static func stamped(_ c: Check, _ w: World) -> Check {
        var out = c
        if c.health == .unknown {
            out.observedAt = nil
        } else if out.observedAt == nil {
            out.observedAt = w.status[c.source]?.sampleAt
        }
        return out
    }

    static func rig(_ w: World) -> Check {
        let s = w.status[.rig] ?? SourceStatus()
        if w.fresh(.rig), let h = w.host {
            if let n = h.net_fail_min, n > 0 {
                return Check(id: "rig", label: "Rig", detail: "network down \(n)m", health: .bad, source: .rig, alert: true)
            }
            return Check(id: "rig", label: "Rig", detail: "online", health: .ok, source: .rig)
        }
        if w.rigUnreachable {
            return Check(id: "rig", label: "Rig", detail: "unreachable", health: .bad, source: .rig, alert: true,
                         observedAt: s.observedAt)
        }
        if let at = s.sampleAt {
            return Check(id: "rig", label: "Rig", detail: "last seen \(ago(at, now: w.now)) ago", health: .unknown, source: .rig)
        }
        let detail = s.failures > 0 ? (s.unreachable ? "not answering" : "no data (\(s.error ?? "collector failed"))") : "checking"
        return Check(id: "rig", label: "Rig", detail: detail, health: .unknown, source: .rig)
    }

    static func rigState(_ w: World) -> Check {
        let id = "rig-state", label = "Rig storage state", fact = w.storageFact
        guard fact.known, let state = fact.state else { return Check(id: id, label: label, detail: fact.why ?? "unknown", health: .unknown, source: .agents) }
        let bad = state.lowercased().contains("critical")
        let healthy = state.uppercased() == "OK"
        let health: Health = bad ? .bad : !healthy || fact.reboot == true ? .warn : fact.hostKnown ? .ok : .unknown
        return Check(id: id, label: label, detail: fact.line, health: health, source: .agents, alert: !healthy, observedAt: fact.updatedDate)
    }

    static func percentUsed(_ pct: Int?, id: String, label: String, source: SourceID, fresh: Bool) -> Check {
        guard fresh else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: source) }
        guard let p = pct else { return Check(id: id, label: label, detail: "no reading", health: .unknown, source: source) }
        if p >= 85 { return Check(id: id, label: label, detail: "\(p)% full", health: .bad, source: source, alert: true) }
        if p >= 75 { return Check(id: id, label: label, detail: "\(p)% full", health: .warn, source: source) }
        return Check(id: id, label: label, detail: "\(p)% used", health: .ok, source: source)
    }

    static func storage(_ w: World) -> Check {
        percentUsed(w.host?.disk_pct, id: "disk", label: "Rig storage", source: .rig, fresh: w.fresh(.rig))
    }

    static func vmDisk(_ w: World) -> Check {
        percentUsed(w.vm?.disk_pct, id: "vmdisk", label: "Agents VM disk", source: .agents, fresh: w.fresh(.agents))
    }


    static func heartbeat(_ age: Int?, id: String, label: String, source: SourceID, busy: Bool = false, missing: Health = .unknown) -> Check {
        guard let age else { return Check(id: id, label: label, detail: "no heartbeat file", health: missing, source: source) }
        if age < heartbeatWarn {
            return Check(id: id, label: label, detail: busy ? "running a job" : "idle", health: .ok, source: source)
        }

        if busy && age < busyMax {
            return Check(id: id, label: label, detail: "running a job (\(duration(age)))", health: .ok, source: source)
        }
        let late = age >= heartbeatBad
        return Check(id: id, label: label, detail: "no heartbeat for \(duration(age))", health: late ? .bad : .warn, source: source, alert: late)
    }


    static func rigLane(_ w: World, id: String, label: String, age: (RigCIHealth) -> Int?, busy: Bool) -> Check {
        guard w.fresh(.rig) else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .rig) }
        guard let ci = w.rigCI else { return Check(id: id, label: label, detail: "no health file", health: .unknown, source: .rig) }
        guard let reported = ci.health_age_s else { return Check(id: id, label: label, detail: "health file age unknown", health: .unknown, source: .rig) }
        let gone = w.elapsed(.rig)
        let lag = reported + gone
        if lag > 300 { return Check(id: id, label: label, detail: "health file \(duration(lag)) old", health: .unknown, source: .rig) }
        return heartbeat(age(ci).map { $0 + gone }, id: id, label: label, source: .rig, busy: busy)
    }

    static func backendCI(_ w: World) -> Check {
        rigLane(w, id: "ci-backend", label: "Backend CI", age: { $0.watch_age_s },
                 busy: (w.rigCI?.heavy_job ?? false) && (w.rigCI?.heavy_owner?.hasPrefix("ci-watch") ?? false))
    }

    static func webCI(_ w: World) -> Check {

        rigLane(w, id: "ci-web", label: "Web CI", age: { $0.web_age_s }, busy: w.rigCI?.heavy_owner == "ci-web")
    }

    static func iosWork(_ w: World) -> String? {
        guard w.fresh(.mac), let s = w.mac?.services, s.ios_watch_phase == "running", let detail = s.ios_watch_detail, !detail.isEmpty,
              let beat = s.ios_beat_age_s, beat + w.elapsed(.mac) < heartbeatWarn else { return nil }
        return detail
    }

    static func iosCI(_ w: World) -> Check {
        let id = "ci-ios", label = "iOS CI"
        guard w.fresh(.mac), let s = w.mac?.services else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .mac) }
        guard let loaded = s.watcher_loaded else { return Check(id: id, label: label, detail: "unknown", health: .unknown, source: .mac) }
        if !loaded { return Check(id: id, label: label, detail: "not loaded", health: .bad, source: .mac, alert: true) }
        guard let jobs = w.mac?.iosCI else { return Check(id: id, label: label, detail: "jobs unknown", health: .unknown, source: .mac) }


        let builds = jobs.filter { $0.repoKey == "ios" }
        let pr = builds.compactMap { $0.pr }.first
        let building = pr.map { "building PR #\($0)" } ?? "building"
        let gone = w.elapsed(.mac)
        if let age = s.ios_watch_age_s.map({ $0 + gone }), age < heartbeatWarn {
            return Check(id: id, label: label, detail: builds.isEmpty ? iosWork(w) ?? "idle" : building, health: .ok, source: .mac)
        }
        if !builds.isEmpty {
            let durations = builds.map { $0.running_s }
            if durations.contains(where: { $0 == nil }) {
                return Check(id: id, label: label, detail: building + ", duration unknown", health: .unknown, source: .mac)
            }
            let longest = durations.compactMap { $0 }.max() ?? 0
            if longest > 5400 {
                return Check(id: id, label: label, detail: "build running \(duration(longest))", health: .warn, source: .mac, alert: true)
            }
            return Check(id: id, label: label, detail: building, health: .ok, source: .mac)
        }
        return heartbeat(s.ios_watch_age_s.map { $0 + gone }, id: id, label: label, source: .mac, missing: .warn)
    }

    static func lightCI(_ w: World) -> Check {
        let id = "ci-light", label = "Light CI"
        guard w.fresh(.mac), let s = w.mac?.services else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .mac) }
        guard let loaded = s.light_loaded else { return Check(id: id, label: label, detail: "unknown", health: .unknown, source: .mac) }
        if !loaded { return Check(id: id, label: label, detail: "not loaded", health: .bad, source: .mac, alert: true) }
        return heartbeat(s.light_age_s.map { $0 + w.elapsed(.mac) }, id: id, label: label, source: .mac)
    }

    static func orch(_ w: World) -> Check {
        let id = "orch", label = "Orch"
        guard w.fresh(.agents), let state = w.vm?.orch else {
            return Check(id: id, label: label, detail: "no data", health: .unknown, source: .agents)
        }
        if state != "active" { return Check(id: id, label: label, detail: state, health: .bad, source: .agents, alert: true) }
        return Check(id: id, label: label, detail: "running", health: .ok, source: .agents)
    }

    static func orchToken(_ w: World) -> Check {
        let id = "orch-token", label = "Orch collector token"
        guard w.fresh(.agents), let v = w.vm else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .agents) }
        guard let exp = v.orch_token_expires else { return Check(id: id, label: label, detail: "expiry unknown", health: .unknown, source: .agents) }
        let left = exp - Int(w.now.timeIntervalSince1970)
        if left <= 0 { return Check(id: id, label: label, detail: "expired", health: .bad, source: .agents, alert: true) }
        if left < 7 * 86400 {
            return Check(id: id, label: label, detail: "expires in \(duration(left))", health: .warn, source: .agents, alert: true)
        }
        return Check(id: id, label: label, detail: "valid", health: .ok, source: .agents)
    }

    static func stale<T>(_ reading: Cached<T>, at: Date, limit: TimeInterval, name: String, now: Date) -> String? {
        if let error = reading.error { return error }
        let age = now.timeIntervalSince(at)
        if age < -clockSkew { return "\(name) time in the future" }
        return age > limit ? "\(name) \(ago(at, now: now)) old" : nil
    }

    static func github(_ w: World) -> Check {
        let id = "github", label = "GitHub API"
        guard let reading = w.githubBudget else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .repos) }
        let source = reading.source, g = reading.budget
        guard let b = g.value, let at = g.fetchedDate else {
            return Check(id: id, label: label, detail: g.error ?? "no data", health: .unknown, source: source)
        }
        if let why = stale(g, at: at, limit: Ingest.githubBudgetLimit(source), name: "budget", now: w.now) {
            return Check(id: id, label: label, detail: why, health: .unknown, source: source)
        }
        let detail = "\(b.core) of \(b.core_limit) left"
        if b.core < 500 { return Check(id: id, label: label, detail: detail, health: .bad, source: source, alert: true, observedAt: at) }
        return Check(id: id, label: label, detail: detail, health: b.core < 1500 ? .warn : .ok, source: source, observedAt: at)
    }

    static func codex(_ w: World) -> Check {
        let id = "codex", label = "Codex usage"
        guard w.fresh(.mac), let x = w.mac?.codex else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .mac) }
        guard let u = x.value, let at = x.fetchedDate else {
            return Check(id: id, label: label, detail: x.error ?? "no data", health: .unknown, source: .mac)
        }
        if let why = stale(x, at: at, limit: Ingest.codexBudgetLimit, name: "usage", now: w.now) {
            return Check(id: id, label: label, detail: why, health: .unknown, source: .mac)
        }
        let pct = Int(min(max(u.used_pct, 0), 1000).rounded())
        let detail = u.isWeekly ? "\(pct)% of week" : "\(pct)% of window"

        if pct >= 95 { return Check(id: id, label: label, detail: detail + " · reviews on credits", health: .warn, source: .mac, alert: true, observedAt: at) }
        if pct >= 90 { return Check(id: id, label: label, detail: detail, health: .warn, source: .mac, alert: true, observedAt: at) }
        return Check(id: id, label: label, detail: detail, health: .ok, source: .mac, observedAt: at)
    }




    static func mainBranches(_ w: World) -> [Check] {
        let fresh = w.fresh(.repos)
        return (w.repos?.repos ?? []).map { r in mainBranch(r, fresh: fresh) }
    }

    static func mainBranch(_ r: RepoRow, fresh: Bool) -> Check {
        let id = "main-" + r.name, label = r.label + " main", url = mainChecksURL(r.name)
        guard fresh else { return Check(id: id, label: label, detail: "no data", health: .unknown, source: .repos, tab: .charts, url: url) }
        switch r.main ?? "unknown" {
        case "failure":
            let still = (r.main_running ?? false) ? " · others still running" : ""
            return Check(id: id, label: label, detail: "checks failing" + still, health: .bad, source: .repos, alert: true, tab: .charts, url: url)
        case "success":
            return Check(id: id, label: label, detail: "checks passing", health: .ok, source: .repos, tab: .charts, url: url)
        case "pending":
            return Check(id: id, label: label, detail: "checks running", health: .ok, source: .repos, tab: .charts, url: url, recovers: false)
        case "cancelled":
            return Check(id: id, label: label, detail: "checks cancelled, need a re-run", health: .warn, source: .repos, tab: .charts, url: url,
                         recovers: false)
        case "not_applicable":
            return Check(id: id, label: label, detail: "no checks on main (by design)", health: .ok, source: .repos, tab: .charts, url: url,
                         recovers: false)
        default:
            return Check(id: id, label: label, detail: "no check state reported", health: .unknown, source: .repos, tab: .charts, url: url)
        }
    }




    static func workTitle(_ issue: String?, _ title: String, _ linear: LinearSnap?) -> String {
        if let issue, let t = linear?.issueTitle(issue) { return t }
        return title.replacingOccurrences(of: #"^\s*"# + SiteConfig.shared.teamKeyPattern + #"-\d+\s*[:·-]?\s*|\s*\("# + SiteConfig.shared.teamKeyPattern + #"-\d+\)\s*$"#, with: "", options: [.regularExpression, .caseInsensitive])
    }
}




struct NeedItem: Identifiable {
    enum Kind: Int { case problem = 0, agent, merge, warning, owner, failing }

    enum Tier: Int, Comparable {
        case now = 0, today, later
        static func < (a: Tier, b: Tier) -> Bool { a.rawValue < b.rawValue }
    }
    let id: String
    let kind: Kind
    let tier: Tier
    let title: String
    let reason: String
    var since: Date? = nil
    var url: URL? = nil
    var copyText: String? = nil
    var tab: Tab? = nil
    var health: Health = .warn
    var launch = false
    var children: [NeedItem] = []
}



struct Needs {
    var items: [NeedItem] = []


    var gaps: [String] = []
    var complete: Bool { gaps.isEmpty }
    func count(_ tier: NeedItem.Tier) -> Int { items.filter { $0.tier == tier }.count }
    func items(_ tier: NeedItem.Tier) -> [NeedItem] { items.filter { $0.tier == tier } }
}


struct SystemVerdict: Equatable {
    struct Item: Identifiable, Equatable {
        let key: String
        let says: String
        let title: String
        let detail: String
        let health: Health
        var id: String { key }
    }
    let health: Health
    let state: String
    let headline: String
    let text: String
    let open: [Item]
    var waiting: [Item] = []
    var menuSymbol: String? {
        switch health {
        case .ok: return nil
        case .unknown: return "questionmark.circle"
        case .warn: return "exclamationmark.circle"
        case .bad: return "exclamationmark.circle.fill"
        }
    }
    var menuNotice: Bool { !open.isEmpty }
}

let agentScope = "Orch backend, web and iOS projects"

extension Rules {

    static func coverageGaps(_ w: World, checks: [Check]) -> [String] {
        var gaps: [String] = []
        for s in SourceID.allCases where !w.fresh(s) { gaps.append(s.label) }
        for c in checks where c.health == .unknown && w.fresh(c.source) { gaps.append(c.label) }
        if w.fresh(.agents) && !w.agentsKnown { gaps.append("Orch agent list") }
        for s in SourceID.allCases where w.fresh(s) && w.status[s]?.note != nil { gaps.append("\(s.label) (partly unreadable)") }
        if w.fresh(.mac), let m = w.mac {
            if m.iosCI == nil { gaps.append("Mac CI jobs") }
            if m.agents == nil { gaps.append("Mac agents") }
        }
        if w.fresh(.repos), let r = w.repos {
            for repo in r.repos ?? [] where repo.truncated == true { gaps.append("\(repo.label) PRs beyond the first 100") }
            if r.mergeStateKnown == false { gaps.append("PR merge states") }
            if r.prs != nil, !r.openInventoryComplete, (r.repos ?? []).allSatisfy({ $0.truncated != true }) { gaps.append("GitHub PR inventory") }
            let unproven = (r.prs ?? []).filter { $0.approved && $0.merge_ready == nil }.count
            if unproven > 0 { gaps.append(unproven == 1 ? "readiness of 1 approved PR" : "readiness of \(unproven) approved PRs") }
        }
        if w.fresh(.linear), let l = w.linear { gaps += l.warnings }
        if w.storyRefused > 0 { gaps.append(w.storyRefused == 1 ? "1 PR history entry not recorded" : "\(w.storyRefused) PR history entries not recorded") }
        var seen = Set<String>()
        return gaps.filter { seen.insert($0).inserted }
    }

    static func needs(_ w: World, checks: [Check], threadURL: (String?) -> URL?) -> Needs {
        var out: [NeedItem] = []
        let linear = w.linear
        let launchProjects = Set(linear?.launch?.projectIds ?? [])


        for c in checks where c.health == .bad || c.health == .warn {
            let urgent = c.health == .bad || c.alert
            let item = NeedItem(id: "check-" + c.id, kind: urgent ? .problem : .warning, tier: urgent ? .now : .today,
                                title: c.label, reason: c.detail, url: c.url, tab: c.tab, health: c.health)
            out.append(item)
        }




        let agentsKnown = w.agentsKnown
        let agents = agentsKnown ? (w.agents ?? []) : []
        let agentPRs = Set(agents.compactMap { $0.prKey })
        let agentIssues = Set(agents.compactMap { $0.issue })
        let ownersFresh = w.fresh(.linear)
        let owners = ownersFresh ? linear?.ownerActions ?? [] : []
        var ownerByIssue: [String: OwnerAction] = [:]
        for o in owners where ownerByIssue[o.id] == nil { ownerByIssue[o.id] = o }

        let prs = w.fresh(.repos) ? (w.repos?.prs ?? []) : []
        let readyPRs = prs.filter { $0.mergeReady }
        var claimedIssues = Set<String>()
        var claimedPRs = Set<String>()

        for a in agents where a.waiting {
            var reason = "waiting for you" + ((a.project ?? "").isEmpty ? "" : " · " + (a.project ?? ""))
            var url = threadURL(a.threadId)
            var launch = linear?.isLaunch(a.issue) ?? false
            if let issue = a.issue {
                claimedIssues.insert(issue)
                if let o = ownerByIssue[issue] {
                    reason += " · owner action" + (dueText(o).map { ", " + $0 } ?? "")
                    url = url ?? o.link
                    launch = launch || o.project_id.map { launchProjects.contains($0) } ?? false
                }
            }
            if let k = a.prKey { claimedPRs.insert(k) }
            let item = NeedItem(id: "agent-" + a.id, kind: .agent, tier: .now, title: a.name, reason: reason,
                                since: isoDate(a.updated), url: url, copyText: a.threadId, launch: launch)
            out.append(item)
        }


        for o in owners where !claimedIssues.contains(o.id) {
            claimedIssues.insert(o.id)
            let due = targetText(o.due)
            let overdue = due.map { $0.1 < 0 } ?? false
            let soon = due.map { $0.1 <= 7 } ?? false
            var reason = dueText(o) ?? (o.pressing ? "urgent" : "no due date")
            var url = o.link
            if let pr = readyPRs.first(where: { $0.issue == o.id }) {
                reason += " · PR ready"
                claimedPRs.insert(pr.repo + "#" + String(pr.number))
                url = url ?? pr.url
            }
            let item = NeedItem(id: "owner-" + o.id, kind: .owner, tier: overdue || soon || o.pressing ? .today : .later,
                                title: o.title, reason: reason, since: isoDate(o.created) ?? isoDate(o.updated), url: url,
                                health: overdue ? .bad : .warn, launch: o.project_id.map { launchProjects.contains($0) } ?? false)
            out.append(item)
        }
        if !ownersFresh, let note = w.retainedPlanNote {
            var kept = Set<String>()
            for o in linear?.ownerActions ?? [] where kept.insert(o.id).inserted {
                out.append(NeedItem(id: "owner-" + o.id, kind: .owner, tier: .later, title: o.title, reason: note + " · " + (dueText(o) ?? "no due date"),
                                    since: isoDate(o.created) ?? isoDate(o.updated), url: o.link, health: .unknown,
                                    launch: o.project_id.map { launchProjects.contains($0) } ?? false))
            }
        }



        var stuck: [NeedItem] = []
        for pr in readyPRs {
            let key = pr.repo + "#" + String(pr.number)
            if claimedPRs.contains(key) || (pr.issue.map { claimedIssues.contains($0) } ?? false) { continue }
            guard let since = pr.readyDate, w.now.timeIntervalSince(since) > 2 * 3600 else { continue }
            let item = NeedItem(id: "pr-" + pr.id, kind: .merge, tier: .today, title: workTitle(pr.issue, pr.title, linear),
                                reason: "\(pr.repoLabelText) #\(pr.number) · ready, not merged", since: since, url: pr.url,
                                tab: .charts, launch: linear?.isLaunch(pr.issue) ?? false)
            stuck.append(item)
        }
        if let g = group(stuck, id: "group-merge", kind: .merge, tier: .today, title: { "\($0) ready PRs not merging" },
                         reason: "mergeable for over 2 hours") {
            out.append(g)
        }



        var failing: [NeedItem] = []
        for pr in prs where pr.ci == "failing" {
            let key = pr.repo + "#" + String(pr.number)
            if claimedPRs.contains(key) || (pr.issue.map { claimedIssues.contains($0) } ?? false) { continue }
            let owned = agentPRs.contains(key) || (pr.issue.map { agentIssues.contains($0) } ?? false)
            if agentsKnown && owned { continue }
            guard let since = pr.failingDate ?? isoDate(pr.updated), w.now.timeIntervalSince(since) > 86400 else { continue }
            let item = NeedItem(id: "fail-" + pr.id, kind: .failing, tier: .later, title: workTitle(pr.issue, pr.title, linear),
                                reason: "\(pr.repoLabelText) #\(pr.number) · failing \(ago(since, now: w.now))" + (agentsKnown ? "" : " · ownership unknown"),
                                since: since, url: pr.url, tab: .charts, launch: linear?.isLaunch(pr.issue) ?? false)
            failing.append(item)
        }
        if let g = group(failing, id: "group-failing", kind: .failing, tier: .later, title: { "\($0) PRs failing for over a day" },
                         reason: agentsKnown ? "no agent working on them" : "agent coverage unknown") {
            out.append(g)
        }

        let sorted = out.sorted { a, b in
            if a.tier != b.tier { return a.tier < b.tier }
            if a.kind != b.kind { return a.kind.rawValue < b.kind.rawValue }
            if a.launch != b.launch { return a.launch }
            return (a.since ?? .distantFuture) < (b.since ?? .distantFuture)
        }
        return Needs(items: sorted, gaps: coverageGaps(w, checks: checks))
    }


    static func verdict(checks: [Check], needs: Needs, acknowledged: (String, String) -> Bool = { _, _ in false }) -> SystemVerdict {
        let trouble = checks.filter { $0.health >= .warn }.sorted { $0.health > $1.health }
            .map { SystemVerdict.Item(key: "check:" + $0.id, says: $0.detail, title: $0.label, detail: $0.detail, health: $0.health) }
        let outstanding = needs.items.filter { !$0.id.hasPrefix("check-") }
        func item(_ n: NeedItem) -> SystemVerdict.Item {
            SystemVerdict.Item(key: "need:" + n.id, says: n.title + n.reason, title: n.title, detail: n.reason, health: n.health)
        }
        let isSystem: (NeedItem) -> Bool = { $0.kind != .owner && $0.tier != .later }
        let items = trouble + outstanding.filter(isSystem).map(item)
        let gaps = needs.gaps.isEmpty ? checks.filter { $0.health == .unknown }.map(\.label) : needs.gaps
        let health = (items.map(\.health) + [gaps.isEmpty ? Health.ok : .unknown]).max() ?? .ok
        let open = items.filter { !acknowledged($0.key, $0.says) }
        let gapKey = needs.gaps.joined(separator: ",")
        let gapOpen = !gaps.isEmpty && !acknowledged("partial", gapKey)
        let gapItem = SystemVerdict.Item(key: "partial", says: gapKey, title: "Unknown", detail: gaps.joined(separator: ", "), health: .unknown)
        let focus = open.isEmpty ? items : open
        var headline: String, text: String
        if focus.count == 1 {
            headline = "\(focus[0].title): \(focus[0].detail)"
            text = "\(focus[0].title) · \(focus[0].detail)"
        } else if focus.count > 1 {
            headline = "\(focus.count) things need you"
            text = "\(focus.count) problems"
        } else if gaps.isEmpty {
            headline = "All systems go"
            text = "All clear"
        } else {
            headline = gaps.count == 1 ? "\(gaps[0]) unknown" : "\(gaps.count) sources unknown"
            text = "partial data"
        }
        if !items.isEmpty && gapOpen { text += " · partial data" }
        if health != .ok && open.isEmpty && !gapOpen {
            headline += " · acknowledged"
            text += " · acknowledged"
        }
        return SystemVerdict(health: health, state: health == .unknown ? "partial" : StatusExport.healthKeys[health] ?? "unknown",
                             headline: headline, text: text, open: open + (gapOpen ? [gapItem] : []),
                             waiting: outstanding.filter { !isSystem($0) }.map(item))
    }

    static func dueText(_ o: OwnerAction) -> String? {
        guard let due = targetText(o.due) else { return nil }
        return due.1 < 0 ? "overdue since \(due.0)" : due.1 == 0 ? "due today" : "due \(due.0)"
    }


    static func group(_ items: [NeedItem], id: String, kind: NeedItem.Kind, tier: NeedItem.Tier, title: (Int) -> String, reason: String) -> NeedItem? {
        if items.isEmpty { return nil }
        if items.count == 1 { return items[0] }
        let kids = items.sorted { ($0.since ?? .distantFuture) < ($1.since ?? .distantFuture) }
        return NeedItem(id: id, kind: kind, tier: tier, title: title(items.count), reason: reason, since: kids.first?.since,
                        tab: .charts, launch: items.contains { $0.launch }, children: kids)
    }
}


func threadURL(_ id: String?) -> URL? {
    guard let id, let template = UserDefaults.standard.string(forKey: "orchThreadURL"), template.contains("{id}") else { return nil }
    return URL(string: template.replacingOccurrences(of: "{id}", with: id))
}




struct AlertObservation {
    enum State { case problem, clear, unknown }
    let id: String
    let state: State
    let text: String

    let at: Date?
    var url: URL? = nil
    var tab: Tab = .mission
}

struct WaitingAgent {
    let id: String
    let title: String
    var url: URL? = nil
}

struct Notice {
    enum Kind: String { case incident, reminder, allClear, agent, missed }
    let kind: Kind
    let title: String
    let body: String
    var url: URL? = nil
    var tab: Tab? = nil
    var ids: [String] = []

    var prevLastPost: Date? = nil
    var startedEpisode = false
    var recovery: AlertState.Recovery? = nil
    var told: [String: String] = [:]
    var timeSensitive: Bool { kind == .agent }
}

struct AlertInput {
    var now: Date

    var observations: [AlertObservation]

    var waiting: [WaitingAgent]?

    var canDeliver: Bool
}


struct AlertState: Codable {
    struct Seen: Codable {
        var first: Date
        var last: Date
        var count: Int
    }
    struct Recovery: Codable {
        var start: Date
        var end: Date
    }
    var episodeStart: Date?
    var lastPost: Date?

    var announced: [String: String] = [:]

    var pending: [String: Seen] = [:]



    var holds: [String: Date] = [:]

    var clean: [String: Seen] = [:]
    var agentsAnnounced: [String] = []

    var owedAllClear: Recovery?


    var recoveredAt: Date?

    var missed: [String: String] = [:]

    var missedRecovered: [String: String] = [:]

    var baseline: Date = .distantPast

    enum CodingKeys: String, CodingKey {
        case episodeStart, lastPost, announced, pending, holds, clean, agentsAnnounced, owedAllClear, recoveredAt
        case missed, missedRecovered, baseline
    }
}

extension AlertState {


    init(from decoder: Decoder) throws {
        self.init()
        let c = try decoder.container(keyedBy: CodingKeys.self)
        episodeStart = try c.decodeIfPresent(Date.self, forKey: .episodeStart)
        lastPost = try c.decodeIfPresent(Date.self, forKey: .lastPost)
        announced = try c.decodeIfPresent([String: String].self, forKey: .announced) ?? [:]
        pending = try c.decodeIfPresent([String: Seen].self, forKey: .pending) ?? [:]
        holds = try c.decodeIfPresent([String: Date].self, forKey: .holds) ?? [:]
        clean = try c.decodeIfPresent([String: Seen].self, forKey: .clean) ?? [:]
        agentsAnnounced = try c.decodeIfPresent([String].self, forKey: .agentsAnnounced) ?? []
        owedAllClear = try c.decodeIfPresent(Recovery.self, forKey: .owedAllClear)
        recoveredAt = try c.decodeIfPresent(Date.self, forKey: .recoveredAt)
        missed = try c.decodeIfPresent([String: String].self, forKey: .missed) ?? [:]
        missedRecovered = try c.decodeIfPresent([String: String].self, forKey: .missedRecovered) ?? [:]
        baseline = try c.decodeIfPresent(Date.self, forKey: .baseline) ?? .distantPast
        dropRetired()
    }



    static let retiredChecks: Set<String> = ["pipeline"]

    mutating func dropRetired() {
        for id in AlertState.retiredChecks {
            announced[id] = nil
            pending[id] = nil
            holds[id] = nil
            clean[id] = nil
            missed[id] = nil
            missedRecovered[id] = nil
        }
    }

    mutating func resetEvidence(_ now: Date) {
        for (id, seen) in pending where holds[id] == nil { holds[id] = seen.last }
        baseline = now
        pending = [:]
        clean = [:]
    }
}














struct AlertEngine {
    var state: AlertState
    static let confirmSpan: TimeInterval = 60
    static let recoverSpan: TimeInterval = 120
    static let minGap: TimeInterval = 600
    static let reminderEvery: TimeInterval = 7200

    init(state: AlertState = AlertState()) { self.state = state }



    static func restored(_ data: Data?, now: Date) -> AlertEngine {
        var s = data.flatMap { try? JSONDecoder().decode(AlertState.self, from: $0) } ?? AlertState()
        s.resetEvidence(now)
        return AlertEngine(state: s)
    }

    func encoded() -> Data? { try? JSONEncoder().encode(state) }


    mutating func rebaseline(_ now: Date) {
        state.resetEvidence(now)
    }

    private static func bump(_ seen: AlertState.Seen?, at t: Date) -> AlertState.Seen {
        guard var s = seen else { return AlertState.Seen(first: t, last: t, count: 1) }
        if t > s.last {
            s.count += 1
            s.last = t
        }
        return s
    }

    private func confirmed(_ id: String) -> Bool {
        guard let s = state.pending[id] else { return false }
        return s.count >= 2 && s.last.timeIntervalSince(s.first) >= AlertEngine.confirmSpan
    }

    private static func allClear(_ r: AlertState.Recovery) -> Notice {
        Notice(kind: .allClear, title: "All clear", body: "Recovered after \(duration(Int(r.end.timeIntervalSince(r.start)))).",
               tab: .mission, recovery: r)
    }

    mutating func step(_ input: AlertInput) -> [Notice] {
        let now = input.now
        var notices: [Notice] = []
        var current: [String: AlertObservation] = [:]
        for o in input.observations where current[o.id] == nil { current[o.id] = o }


        for o in current.values {
            guard let t = o.at, t > state.baseline else { continue }
            switch o.state {
            case .problem:
                state.pending[o.id] = AlertEngine.bump(state.pending[o.id], at: t)
                state.clean[o.id] = nil
                state.holds[o.id] = max(state.holds[o.id] ?? t, t)
            case .clear:
                if let p = state.pending[o.id], t > p.last { state.pending[o.id] = nil }
                if let h = state.holds[o.id], t > h { state.holds[o.id] = nil }
                if state.announced[o.id] != nil || state.missed[o.id] != nil {
                    state.clean[o.id] = AlertEngine.bump(state.clean[o.id], at: t)
                }
            case .unknown:
                break
            }
        }


        for (id, c) in state.clean where c.count >= 2 && c.last.timeIntervalSince(c.first) >= AlertEngine.recoverSpan {
            state.clean[id] = nil
            state.announced[id] = nil
            if let text = state.missed[id] {
                state.missed[id] = nil
                state.missedRecovered[id] = text
            }
        }

        let holding = current.values.filter { $0.state == .problem && confirmed($0.id) }.sorted { $0.id < $1.id }
        let fresh = holding.filter { state.announced[$0.id] == nil }


        let unsettled = !state.holds.isEmpty || current.values.contains { $0.state == .problem }
        if state.episodeStart != nil && state.announced.isEmpty && state.recoveredAt == nil { state.recoveredAt = now }

        if input.canDeliver {

            if !state.missedRecovered.isEmpty {
                let told = state.missedRecovered
                state.missedRecovered = [:]
                let lines = told.values.sorted().map { $0 + " (recovered)" }
                notices.append(Notice(kind: .missed, title: "While notifications were off", body: lines.joined(separator: "\n"),
                                      ids: Array(told.keys), told: told))
            }
            for p in holding { state.missed[p.id] = nil }

            let sinceLast = state.lastPost.map { now.timeIntervalSince($0) } ?? .infinity
            if !fresh.isEmpty && sinceLast >= AlertEngine.minGap {
                let others = holding.count - fresh.count
                let title = fresh.count == 1 ? fresh[0].text : "\(fresh.count) problems"
                var body = fresh.count == 1 ? "" : fresh.map(\.text).joined(separator: "\n")
                if others > 0 { body += (body.isEmpty ? "" : "\n") + "\(others) more still unresolved" }
                var n = Notice(kind: .incident, title: title, body: body, url: fresh.compactMap(\.url).first,
                               tab: fresh.first?.tab, ids: fresh.map(\.id))
                n.prevLastPost = state.lastPost
                n.startedEpisode = state.episodeStart == nil
                if state.episodeStart == nil { state.episodeStart = now }
                for p in fresh { state.announced[p.id] = p.text }
                state.lastPost = now
                state.owedAllClear = nil
                state.recoveredAt = nil
                notices.append(n)
            } else if let start = state.episodeStart, sinceLast >= AlertEngine.reminderEvery {
                let still = holding.filter { state.announced[$0.id] != nil }
                if !still.isEmpty {
                    var n = Notice(kind: .reminder, title: "Still unresolved after \(duration(Int(now.timeIntervalSince(start))))",
                                   body: still.map(\.text).joined(separator: "\n"), url: still.compactMap(\.url).first,
                                   tab: still.first?.tab, ids: still.map(\.id))
                    n.prevLastPost = state.lastPost
                    state.lastPost = now
                    notices.append(n)
                }
            }
        } else {
            for p in fresh where state.missed[p.id] == nil { state.missed[p.id] = p.text }
        }



        if let start = state.episodeStart, state.announced.isEmpty, !unsettled {
            state.episodeStart = nil
            let r = AlertState.Recovery(start: start, end: state.recoveredAt ?? now)
            state.recoveredAt = nil
            if input.canDeliver {
                notices.append(AlertEngine.allClear(r))
            } else {
                state.owedAllClear = r
            }
        } else if input.canDeliver, let r = state.owedAllClear, state.episodeStart == nil, !unsettled {
            state.owedAllClear = nil
            notices.append(AlertEngine.allClear(r))
        }


        if let waiting = input.waiting {
            let ids = Set(waiting.map(\.id))
            state.agentsAnnounced = state.agentsAnnounced.filter { ids.contains($0) }
            let new = waiting.filter { !state.agentsAnnounced.contains($0.id) }
            if input.canDeliver && !new.isEmpty {
                if new.count <= 3 {
                    for a in new {
                        notices.append(Notice(kind: .agent, title: "Agent waiting for you", body: a.title, url: a.url, tab: .mission, ids: [a.id]))
                    }
                } else {
                    notices.append(Notice(kind: .agent, title: "\(new.count) agents waiting for you", body: new.map(\.title).joined(separator: "\n"),
                                          tab: .mission, ids: new.map(\.id)))
                }
                state.agentsAnnounced += new.map(\.id)
            }
        }
        return notices
    }



    mutating func deliveryFailed(_ n: Notice) {
        switch n.kind {
        case .incident:
            for id in n.ids { state.announced[id] = nil }
            state.lastPost = n.prevLastPost
            if n.startedEpisode && state.announced.isEmpty { state.episodeStart = nil }
        case .reminder:
            state.lastPost = n.prevLastPost
        case .allClear:
            if state.episodeStart == nil, let r = n.recovery { state.owedAllClear = r }
        case .missed:
            for (id, text) in n.told { state.missedRecovered[id] = text }
        case .agent:
            state.agentsAnnounced.removeAll { n.ids.contains($0) }
        }
    }
}

extension Rules {



    static func alertObservations(_ checks: [Check]) -> [AlertObservation] {
        checks.map { c in
            let state: AlertObservation.State
            if c.health == .unknown {
                state = .unknown
            } else if c.alert && (c.health == .bad || c.health == .warn) {
                state = .problem
            } else {
                state = c.recovers ? .clear : .unknown
            }
            return AlertObservation(id: c.id, state: state, text: "\(c.label): \(c.detail)", at: c.observedAt, url: c.url, tab: c.tab)
        }
    }
}


enum PRStatus: Int, CaseIterable, Identifiable {
    case draft, fixing, waitingCI, inCI, review, approved, merged
    var id: Int { rawValue }
    var label: String { ["Draft", "Fixing", "Waiting CI", "In CI", "In review", "Approved", "Merged 24h"][rawValue] }




    static func of(_ p: PRRow) -> PRStatus {
        if p.merge_state == "DIRTY" || p.ci == "failing" { return .fixing }
        if p.ci == "running" || p.checks_running == true { return p.ci_waiting == true ? .waitingCI : .inCI }
        if p.ci == "passed" { return p.review == "approved" ? .approved : .review }
        return .waitingCI
    }
}


enum PRStage: Int, CaseIterable, Identifiable {
    case work, ci, review, merged
    var id: Int { rawValue }
    var label: String { ["In work", "In CI", "In review", "Merged 24h"][rawValue] }

    static func of(_ p: PRRow) -> PRStage {
        switch PRStatus.of(p) {
        case .draft, .fixing: return .work
        case .waitingCI, .inCI: return .ci
        case .review, .approved: return .review
        case .merged: return .merged
        }
    }
}



struct FlowSample: Codable, Equatable {
    let t: Double

    let counts: [String: Int]?

    var main: [String: String]? = nil

    var budgets: [String: Double]? = nil
    func count(_ s: PRStatus) -> Int { counts?[String(s.rawValue)] ?? 0 }

    static let keep: Double = 7 * 86400
    static let spacing: Double = 10 * 60

    static let waiting: [PRStatus] = [.fixing, .waitingCI, .inCI, .review, .approved]

    static func countedRuns(_ history: [FlowSample]) -> [[FlowSample]] {
        var runs: [[FlowSample]] = [[]]
        for s in history {
            if s.counts == nil { runs.append([]) } else { runs[runs.count - 1].append(s) }
        }
        return runs.filter { !$0.isEmpty }
    }

    static func of(_ prs: [PRRow], at t: Double, complete: Bool = true) -> FlowSample {
        guard complete else { return FlowSample(t: t, counts: nil) }
        var c: [String: Int] = [:]
        for p in prs { c[String(PRStatus.of(p).rawValue), default: 0] += 1 }
        return FlowSample(t: t, counts: c)
    }


    static func append(_ s: FlowSample, to history: [FlowSample]) -> [FlowSample] {
        var h = history.filter { s.t - $0.t < keep }
        if let last = h.last, s.t - last.t < spacing { return h }
        h.append(s)
        return h
    }



    static let maxHold: Double = 2 * 3600



    static func held(_ history: [FlowSample], since: Double, now: Double) -> [(FlowSample, Double)] {
        let h = history.filter { $0.t <= now }.sorted { $0.t < $1.t }
        return h.enumerated().compactMap { i, s in
            let end = min(i + 1 < h.count ? h[i + 1].t : now, s.t + maxHold, now)
            let from = max(s.t, since)
            return end > from ? (s, end - from) : nil
        }
    }




    static func bottleneck(_ history: [FlowSample], since: Double, now: Double = Date().timeIntervalSince1970) -> (status: PRStatus, avg: Double)? {
        let spans = held(history, since: since, now: now).filter { $0.0.counts != nil }
        let covered = spans.map(\.1).reduce(0, +)
        guard covered > 0 else { return nil }
        let avgs = waiting.map { s in (s, spans.map { Double($0.0.count(s)) * $0.1 }.reduce(0, +) / covered) }
        return avgs.max { $0.1 < $1.1 }.flatMap { $0.1 > 0 ? $0 : nil }
    }

    static func covered(_ history: [FlowSample], since: Double, now: Double) -> Bool {
        held(history, since: since, now: now).contains { $0.0.counts != nil }
    }
}



struct LaneJob: Identifiable, Equatable {
    let id: String
    let title: String
    let elapsed: Int?
    let typical: Int?

    var pr: Int? = nil
    var run: Int? = nil

    var repo: String? = nil

    var name: String? = nil

    var leg: String? = nil

    var completion: String? = nil

    var progress: Double? {
        guard let e = elapsed, let t = typical, t > 0 else { return nil }
        return min(Double(e) / Double(t), 1)
    }

    var overdue: Bool {
        guard let e = elapsed, let t = typical else { return false }
        return Double(e) > Double(t) * 1.5
    }
    var remaining: Int? { typical.flatMap { t in elapsed.map { max(t - $0, 0) } } }

    var timeText: String {
        if overdue, let e = elapsed { return "running \(duration(e))" }
        if let left = remaining { return left > 0 ? "\(clock(left)) left" : "finishing" }
        return elapsed.map { duration($0) } ?? "running"
    }

    var cardText: String { pr == nil ? timeText : completion ?? elapsed.map { duration($0) } ?? "running" }

    var short: String? {
        if title.hasPrefix("PR #") { return "#" + title.dropFirst(4).prefix { $0.isNumber } }
        return title.hasPrefix("main") ? "main" : nil
    }
}

enum LaneTiming {


    static func schedule(running: [Int?], slots: Int, jobs: [(group: String, length: Int?)],
                         busy: [String: Int?] = [:]) -> [(start: Int?, end: Int?)] {
        func order(_ a: Int?, _ b: Int?) -> Bool { (a ?? .max) < (b ?? .max) }
        var free = Array(running.sorted(by: order).suffix(slots)) + Array(repeating: Int?(0), count: max(0, slots - running.count))
        var groupEnd = busy
        var waiting = Array(jobs.indices)
        var out: [(start: Int?, end: Int?)] = Array(repeating: (nil, nil), count: jobs.count)
        while !waiting.isEmpty {
            waiting.removeAll { if let held = groupEnd[jobs[$0].group] { return held == nil } else { return false } }
            guard let slot = free.indices.filter({ free[$0] != nil }).min(by: { free[$0]! < free[$1]! }), let at = free[slot], !waiting.isEmpty else { break }
            guard let i = waiting.first(where: { (groupEnd[jobs[$0].group] ?? nil).map { $0 <= at } ?? true }) else {
                free[slot] = waiting.compactMap { groupEnd[jobs[$0].group] ?? nil }.filter { $0 > at }.min()
                continue
            }
            let end = jobs[i].length.map { at + $0 }
            out[i] = (at, end)
            free[slot] = end
            groupEnd[jobs[i].group] = .some(end)
            waiting.removeAll { $0 == i }
        }
        return out
    }


    static func head(ofGuest name: String) -> String? {
        guard let r = name.range(of: #"-([0-9a-f]{7,40})$"#, options: .regularExpression) else { return nil }
        return String(name[r].dropFirst())
    }



    static func sameHead(guest name: String, prHead: String?) -> Bool {
        guard let g = head(ofGuest: name), let h = prHead, !h.isEmpty else { return false }
        return h.hasPrefix(g) || g.hasPrefix(h)
    }


    static func backend(_ guests: [GuestSnap], _ lanes: CILanes.Backend?, age: Int = 0) -> [LaneJob] {
        guests.filter { $0.kind == "backend CI" }.map { g in

            let lane = lanes?.guests?[g.name]
                ?? g.pr.flatMap { n in lanes?.guests?.first { $0.key.hasPrefix("ciw-pr\(n)-") }?.value }
            let what = g.pr.map { "PR #\($0)" } ?? (g.name.contains("-main-") ? "main" : g.name)
            let typical = lane == "fast" ? lanes?.fast_s : lane == "integration" ? lanes?.integration_s : nil
            return LaneJob(id: g.name, title: [what, lane].compactMap { $0 }.joined(separator: " · "),
                           elapsed: g.up_s.map { $0 + age }, typical: typical, pr: g.pr, repo: "backend", leg: lane)
        }
    }

    static func web(_ guests: [GuestSnap], _ lane: CILanes.Lane?, age: Int = 0) -> [LaneJob] {
        guests.filter { $0.kind == "web CI" }.map { g in
            LaneJob(id: g.name, title: g.pr.map { "PR #\($0)" } ?? "web run", elapsed: g.up_s.map { $0 + age }, typical: lane?.run_s,
                    pr: g.pr, run: g.run, repo: "web")
        }
    }

    static func ios(_ jobs: [IOSJob], _ lane: CILanes.Lane?, age: Int = 0) -> [LaneJob] {
        jobs.enumerated().map { i, j in
            LaneJob(id: "ios\(i)", title: j.pr.map { "PR #\($0)" } ?? (j.main == true ? "main" : "build"), elapsed: j.running_s.map { $0 + age },
                    typical: j.repoKey == "ios" ? lane?.run_s : nil, pr: j.pr, repo: j.repoKey)
        }
    }


    static func idle(lastDone: Int?, now: Double) -> Int? {
        lastDone.map { max(0, Int(now) - $0) }
    }
}



struct PRTimeline: Codable, Equatable {
    struct Span: Codable, Equatable {
        let status: Int
        let from: Double
        var to: Double?
    }
    var spans: [Span]
    var opened: Double?

    var flaky: [String]? = nil

    var builder: String? = nil


    var seen: Double? = nil

    var flakyOnsets: [String: [Double]]? = nil

    var flakyLive: [String]? = nil

    mutating func observeFlaky(_ names: [String]?, at now: Double) {
        guard let names else { return }
        let live = Set(flakyLive ?? [])
        let inherited = flakyLive == nil ? Set(flaky ?? []) : []
        for name in names where !live.contains(name) && !inherited.contains(name) {
            flakyOnsets = (flakyOnsets ?? [:]).merging([name: Array(((flakyOnsets?[name] ?? []) + [now]).suffix(PRTimeline.keepOnsets))]) { _, new in new }
        }
        flakyLive = names.sorted()
    }


    func openEnd(_ now: Double) -> Double { seen.map { min(now, $0 + PRTimeline.maxGap) } ?? now }

    var current: PRStatus? { spans.last.flatMap { $0.to == nil ? PRStatus(rawValue: $0.status) : nil } }
    func inStage(now: Double) -> Double? { spans.last.flatMap { $0.to == nil ? now - $0.from : nil } }


    func totals(now: Double) -> [PRStatus: Double] {
        var out: [PRStatus: Double] = [:]
        for s in spans { if let st = PRStatus(rawValue: s.status) { out[st, default: 0] += max(0, (s.to ?? openEnd(now)) - s.from) } }
        return out
    }

    func bottleneck(now: Double) -> (status: PRStatus, seconds: Double)? {
        totals(now: now).filter { FlowSample.waiting.contains($0.key) }.max { $0.value < $1.value }.map { ($0.key, $0.value) }
    }

    static let keep: Double = 7 * 86400
    static let keepOnsets = 100
    static let maxGap: Double = 3600






    static func update(_ all: [String: PRTimeline], prs: [PRRow], merged: [Merge], now: Double,
                       builders: [String: String] = [:], complete: Bool = false) -> [String: PRTimeline] {
        var out = all
        for (id, b) in builders where out[id] != nil && out[id]?.builder == nil { out[id]?.builder = b }
        for p in prs {
            let st = PRStatus.of(p).rawValue
            if var t = out[p.id], let last = t.spans.last, last.to == nil {
                if now < t.seen ?? last.from { continue }
                if let seen = t.seen, now - seen > maxGap {
                    t.spans[t.spans.count - 1].to = max(seen, last.from)
                    t.spans.append(Span(status: st, from: now))
                } else if last.status != st {
                    t.spans[t.spans.count - 1].to = now
                    t.spans.append(Span(status: st, from: now))
                }
                t.seen = now
                t.observeFlaky(p.flaky, at: now)
                if let f = p.flaky, !f.isEmpty { t.flaky = Array(Set((t.flaky ?? []) + f)).sorted() }
                out[p.id] = t
            } else {
                let guess: Date? = PRStatus.of(p) == .fixing ? p.failingDate
                    : [.review, .approved].contains(PRStatus.of(p)) ? p.readyDate : nil
                let opened = isoDate(p.created)?.timeIntervalSince1970
                let start = max(guess?.timeIntervalSince1970 ?? now, opened ?? 0)
                var fresh = PRTimeline(spans: [Span(status: st, from: min(start, now))], opened: opened,
                                       flaky: (p.flaky ?? []).isEmpty ? nil : p.flaky, builder: builders[p.id], seen: now)
                fresh.flakyLive = []
                fresh.observeFlaky(p.flaky, at: now)
                out[p.id] = fresh
            }
        }
        for mg in merged {
            guard var t = out[mg.id], let last = t.spans.last, last.to == nil, let at = mg.mergedDate?.timeIntervalSince1970 else { continue }
            t.spans[t.spans.count - 1].to = max(at, last.from)
            t.spans.append(Span(status: PRStatus.merged.rawValue, from: max(at, last.from), to: max(at, last.from)))
            out[mg.id] = t
        }
        if complete {
            let gone = Set(out.keys).subtracting(prs.map(\.id)).subtracting(merged.map(\.id))
            for id in gone {
                guard var t = out[id], let last = t.spans.last, last.to == nil else { continue }
                t.spans[t.spans.count - 1].to = max(t.seen ?? last.from, last.from)
                out[id] = t
            }
        }
        return out.filter { _, t in now - (t.spans.last?.to ?? now) < keep }
    }
}


enum Analytics {

    static func rank(_ s: PRStatus) -> Int { [.draft: -1, .fixing: 0, .waitingCI: 1, .inCI: 2, .review: 3, .approved: 4, .merged: 5][s] ?? -1 }


    static func clipped(_ t: PRTimeline, since: Double, now: Double) -> [(PRStatus, Double)] {
        t.spans.compactMap { sp in
            guard let st = PRStatus(rawValue: sp.status) else { return nil }
            let d = min(sp.to ?? t.openEnd(now), now) - max(sp.from, since)
            return d > 0 ? (st, d) : nil
        }
    }

    static func mean(_ xs: [Double]) -> Double? { xs.isEmpty ? nil : xs.reduce(0, +) / Double(xs.count) }

    static func percentile(_ xs: [Double], _ p: Double) -> Double? {
        guard !xs.isEmpty else { return nil }
        let s = xs.sorted()
        return s[min(s.count - 1, Int((Double(s.count - 1) * p).rounded()))]
    }


    static func phases(_ tl: [PRTimeline], since: Double, now: Double) -> [PRStatus: (median: Double, p85: Double)] {
        var per: [PRStatus: [Double]] = [:]
        for t in tl {
            var sums: [PRStatus: Double] = [:]
            for (st, d) in clipped(t, since: since, now: now) where FlowSample.waiting.contains(st) { sums[st, default: 0] += d }
            for (st, d) in sums { per[st, default: []].append(d) }
        }
        return per.compactMapValues { xs in percentile(xs, 0.5).flatMap { m in percentile(xs, 0.85).map { (m, $0) } } }
    }


    static func shares(_ tl: [PRTimeline], since: Double, now: Double) -> [PRStatus: Double] {
        var sums: [PRStatus: Double] = [:]
        for t in tl { for (st, d) in clipped(t, since: since, now: now) where FlowSample.waiting.contains(st) { sums[st, default: 0] += d } }
        let total = sums.values.reduce(0, +)
        return total > 0 ? sums.mapValues { $0 / total } : [:]
    }


    static func sentence(_ tl: [PRTimeline], window: Double, now: Double) -> String? {
        sentenceParts(tl, window: window, now: now).map { "\($0.status.label) \($0.rest)" }
    }


    static func sentenceParts(_ tl: [PRTimeline], window: Double, now: Double) -> (status: PRStatus, rest: String)? {
        let cur = shares(tl, since: now - window, now: now)

        guard let top = cur.max(by: { ($0.value, -rank($0.key)) < ($1.value, -rank($1.key)) }) else { return nil }
        let pct = Int((top.value * 100).rounded())
        let before = shares(tl, since: now - 2 * window, now: now - window)[top.key].map { Int(($0 * 100).rounded()) }
        let trend = before.map { b in b == pct ? ", same as before" : ", \(pct > b ? "up" : "down") from \(b)%" } ?? ""
        return (top.key, "held \(pct)% of PR time\(trend)")
    }



    static func rework(_ t: PRTimeline, now: Double, since: Double = -.infinity) -> (loops: Int, seconds: Double) {
        var furthest = -1, loops = 0, secs = 0.0, below = false
        for sp in t.spans {
            guard let st = PRStatus(rawValue: sp.status), st != .merged, st != .draft else { continue }
            let r = rank(st)
            if furthest >= 3 && r < furthest {
                if !below { loops += 1; below = true }
                secs += max(0, min(sp.to ?? t.openEnd(now), now) - max(sp.from, since))
            } else {
                below = false
            }
            furthest = max(furthest, r)
        }
        return (loops, secs)
    }


    static func reworkSummary(_ tl: [PRTimeline], since: Double, now: Double) -> (prShare: Double, timeShare: Double)? {
        let active = tl.filter { !clipped($0, since: since, now: now).isEmpty }
        guard !active.isEmpty else { return nil }

        let withRework = active.filter { rework($0, now: now, since: since).seconds > 0 }.count
        let total = active.flatMap { clipped($0, since: since, now: now) }.map(\.1).reduce(0, +)
        let rw = active.map { rework($0, now: now, since: since).seconds }.reduce(0, +)
        return (Double(withRework) / Double(active.count), total > 0 ? rw / total : 0)
    }



    static func reviewFlow(_ tl: [PRTimeline], since: Double) -> (inflow: Int, outflow: Int) {
        var i = 0, o = 0
        for t in tl {
            for (a, b) in zip(t.spans, t.spans.dropFirst()) where b.from >= since {
                guard let sa = PRStatus(rawValue: a.status), let sb = PRStatus(rawValue: b.status) else { continue }
                if rank(sa) < 3 && rank(sb) >= 3 && sb != .merged { i += 1 }
                if rank(sa) >= 3 && (sb == .merged || rank(sb) < 3) { o += 1 }
            }
        }
        return (i, o)
    }


    static func flaky(_ tl: [PRTimeline], since: Double = -.infinity) -> [(name: String, prs: Int)] {
        var n: [String: Int] = [:]
        for t in tl { for f in t.flaky ?? [] where since == -.infinity || (t.flakyOnsets?[f] ?? []).contains(where: { $0 >= since }) { n[f, default: 0] += 1 } }
        return n.map { ($0.key, $0.value) }.sorted { ($0.prs, $1.name) > ($1.prs, $0.name) }
    }




    struct Signal: Equatable {
        enum State { case ok, warn, partial, unknown }
        let name: String
        let value: String
        let state: State
    }

    static let fullWindow = 0.95

    static func observedShare(_ flow: [FlowSample], since: Double, now: Double) -> Double {
        guard now > since else { return 0 }
        return min(1, FlowSample.held(flow, since: since, now: now).filter { $0.0.counts != nil }.map(\.1).reduce(0, +) / (now - since))
    }

    static func signals(_ tl: [PRTimeline], flow: [FlowSample], since: Double, now: Double) -> [Signal] {
        let seen = observedShare(flow, since: since, now: now), whole = seen >= fullWindow
        func percent(_ x: Double) -> Int { Int((x * 100).rounded()) }
        let seenText = seen < 0.01 ? "<1% seen" : "\(percent(seen))% seen"
        func judged(_ name: String, _ value: String, warn: Bool) -> Signal {
            Signal(name: name, value: whole ? value : value + " · " + seenText, state: warn ? .warn : whole ? .ok : .partial)
        }
        func quiet(_ name: String, _ value: String) -> Signal {
            seen > 0 ? judged(name, value, warn: false) : Signal(name: name, value: "–", state: .unknown)
        }
        let moved = reviewFlow(tl, since: since)
        let review = moved.inflow + moved.outflow == 0 ? quiet("Review", "none moved")
            : judged("Review", "\(moved.inflow) entered · \(moved.outflow) left", warn: moved.inflow > moved.outflow + 2)
        let reworked = reworkSummary(tl, since: since, now: now).map {
            judged("Rework", "\(percent($0.prShare))% of PRs · \(percent($0.timeShare))% of time", warn: $0.prShare > 0.3)
        } ?? quiet("Rework", "no active PRs")
        let flakes = flaky(tl, since: since)
        let undated = tl.contains { t in
            guard !clipped(t, since: since, now: now).isEmpty || (t.seen ?? -.infinity) >= since else { return false }
            return (t.flaky ?? []).contains { (t.flakyOnsets?[$0] ?? []).isEmpty } || (t.flakyLive ?? []).contains { name in !(t.flakyOnsets?[name] ?? []).contains { $0 >= since } }
        }
        let flakeRow = !flakes.isEmpty ? Signal(name: "Flaky checks", value: flakes.prefix(3).map { "\($0.name) ×\($0.prs)" }.joined(separator: " · "), state: .warn)
            : undated || seen == 0 ? Signal(name: "Flaky checks", value: "–", state: .unknown)
            : whole ? Signal(name: "Flaky checks", value: "none seen", state: .ok)
            : Signal(name: "Flaky checks", value: "none in " + seenText, state: .partial)
        let repos = ["backend", "ios", "web"]
        let red: [(String, Double, Double)] = repos.compactMap { r in mainCoverage(flow, repo: r, since: since, now: now).map { (repoLabel(r), $0.red, $0.seen) } }
        let unseen = repos.filter { mainCoverage(flow, repo: $0, since: since, now: now) == nil }.map(repoLabel)
        let shares = red.map { "\($0.0) \(percent($0.1))%" + ($0.2 < fullWindow ? " of \(percent($0.2))% seen" : "") }
        let mainRow = red.isEmpty ? Signal(name: "Main red", value: "–", state: .unknown)
            : Signal(name: "Main red", value: (shares + (unseen.isEmpty ? [] : [unseen.joined(separator: ", ") + " unknown"])).joined(separator: " · "),
                     state: red.contains { $0.1 > 0.1 } ? .warn : unseen.isEmpty && red.allSatisfy { $0.2 >= fullWindow } ? .ok : .partial)
        return [review, reworked, flakeRow, mainRow]
    }

    static func mainRed(_ flow: [FlowSample], repo: String, since: Double, now: Double = Date().timeIntervalSince1970) -> Double? {
        mainCoverage(flow, repo: repo, since: since, now: now)?.red
    }

    static func mainCoverage(_ flow: [FlowSample], repo: String, since: Double, now: Double) -> (red: Double, seen: Double)? {
        let spans = FlowSample.held(flow, since: since, now: now)
        let known = spans.filter { $0.0.main?[repo].map { $0 != "unknown" } ?? false }
        let total = known.map(\.1).reduce(0, +)
        guard total > 0 else { return nil }
        return (known.filter { $0.0.main?[repo] == "failure" }.map(\.1).reduce(0, +) / total, total / (now - since))
    }



    static func projection(_ flow: [FlowSample], key: String, limit: Double, now: Double, window: Double = 6 * 3600) -> Double? {
        let pts = flow.filter { now - $0.t <= window }.compactMap { s in s.budgets?[key].map { (s.t, $0) } }
        guard pts.count >= 3 else { return nil }
        let mx = pts.map(\.0).reduce(0, +) / Double(pts.count), my = pts.map(\.1).reduce(0, +) / Double(pts.count)
        let num = pts.map { ($0.0 - mx) * ($0.1 - my) }.reduce(0, +), den = pts.map { pow($0.0 - mx, 2) }.reduce(0, +)
        guard den > 0 else { return nil }
        let slope = num / den, last = pts.last!.1
        guard slope != 0, (limit - last) / slope > 0 else { return nil }
        return (limit - last) / slope
    }


    static func agingThreshold(_ tl: [PRTimeline], _ status: PRStatus) -> Double? {
        let xs = tl.flatMap { $0.spans.filter { $0.status == status.rawValue && $0.to != nil }.map { $0.to! - $0.from } }
        return xs.count >= 5 ? percentile(xs, 0.85) : nil
    }



    static func typical(_ all: [String: PRTimeline], _ status: PRStatus, repo: String) -> Double? {
        func spans(_ t: [PRTimeline]) -> [Double] {
            t.flatMap { $0.spans.filter { $0.status == status.rawValue && $0.to != nil }.map { $0.to! - $0.from } }
        }
        let mine = spans(all.filter { $0.key.hasPrefix(repo + "#") }.map(\.value))
        if mine.count >= 5 { return percentile(mine, 0.5) }
        let every = spans(Array(all.values))
        return every.count >= 3 ? percentile(every, 0.5) : nil
    }
}













struct PRModels: Decodable {
    let schema: String?
    let built_by: String?
    let built_confidence: String?
    let built_family: String?
    let reviewed_by: [String]?
    let reworked_by: [String]?
    let rework_runs: Int?
    let tokens_complete: Bool?
    let shared_runs: Int?

    struct Run: Decodable, Equatable {
        let run: String?
        let model: String?
        let effort: String?
        let harness: String?
        let tokens: Tokens?

        let tokens_complete: Bool?

        let usage_basis: String?

        let allocation: String?
        let role: String?



        let switched: [String]?
        var settings_basis: String? = nil

        var label: String? {
            let requested = settings_basis == Self.requestedBasis ? " (requested)" : ""
            return model.map { $0 == "mixed" ? "Mixed" : ModelName.display($0) + (effort.map { " · \($0)" } ?? "") + requested }
        }

        static let requestedBasis = "orch-requested"

        var setupKey: String { [model ?? "?", effort ?? "?", harness ?? "?"].joined(separator: "|") }

        var countedTokens: Tokens? { allocation == "shared" || allocation == LiveModels.unallocated || tokens_complete == false || tokens?.valid != true ? nil : tokens }



        static func conflictingEvidence(_ runs: [Run]) -> Bool {
            func conflict<T: Equatable>(_ values: [T?]) -> Bool {
                let known = values.compactMap { $0 }
                return known.first.map { first in known.dropFirst().contains { $0 != first } } ?? false
            }
            return conflict(runs.map(\.model)) || conflict(runs.map(\.effort)) || conflict(runs.map(\.harness))
                || conflict(runs.map(\.allocation)) || conflict(runs.map(\.role)) || conflict(runs.map(\.tokens_complete))
                || conflict(runs.map { $0.switched.map(Set.init) })
                || conflict(runs.map { $0.tokens?.input }) || conflict(runs.map { $0.tokens?.output })
                || conflict(runs.map { $0.tokens?.cache_read ?? $0.tokens?.cached_input })
                || conflict(runs.map { $0.tokens?.cache_write }) || conflict(runs.map { $0.tokens?.reasoning })
        }


        static func coalesced(_ copies: [Run]) -> Run? {
            guard !conflictingEvidence(copies) else { return nil }
            func known<T>(_ values: [T?]) -> T? { values.compactMap { $0 }.first }
            let usage: Tokens? = copies.allSatisfy({ $0.tokens?.valid == true }) ? Tokens(
                input: known(copies.map { $0.tokens?.input }), cached_input: known(copies.map { $0.tokens?.cached_input }),
                cache_read: known(copies.map { $0.tokens?.cache_read }), cache_write: known(copies.map { $0.tokens?.cache_write }),
                output: known(copies.map { $0.tokens?.output }), reasoning: known(copies.map { $0.tokens?.reasoning })) : nil
            let bases = Set(copies.compactMap(\.usage_basis))
            return Run(run: known(copies.map(\.run)), model: known(copies.map(\.model)), effort: known(copies.map(\.effort)),
                       harness: known(copies.map(\.harness)), tokens: usage, tokens_complete: known(copies.map(\.tokens_complete)),
                       usage_basis: bases.count == 1 ? bases.first : nil, allocation: known(copies.map(\.allocation)),
                       role: known(copies.map(\.role)), switched: known(copies.map(\.switched)),
                       settings_basis: known(copies.map(\.settings_basis)))
        }

    }

    struct Tokens: Decodable, Equatable {
        let input: Int?
        let cached_input: Int?
        let cache_read: Int?
        let cache_write: Int?
        let output: Int?
        let reasoning: Int?
        var cacheRead: Int? { valid ? (cache_read ?? cached_input ?? 0) : nil }


        var valid: Bool {
            let fields = [input, cached_input, cache_read, cache_write, output, reasoning].compactMap { $0 }
            return (input != nil || cache_write != nil || output != nil) && fields.allSatisfy { $0 >= 0 }
                && Self.sum([input ?? 0, cache_write ?? 0, output ?? 0]) != nil
        }
        var fresh: Int? { valid ? Self.sum([input ?? 0, cache_write ?? 0, output ?? 0]) : nil }
        static func sum(_ values: [Int]) -> Int? {
            var result = 0
            for value in values {
                guard value >= 0 else { return nil }
                let (next, overflow) = result.addingReportingOverflow(value)
                guard !overflow else { return nil }
                result = next
            }
            return result
        }
    }
    var built: Run? = nil
    var reviews: [Run]? = nil
    var reworks: [Run]? = nil

    var runs: [Run]? = nil
    var tokens: Tokens? = nil
    var tokens_basis: String? = nil
    var history_complete: Bool? = nil
    var history_start: Double? = nil


    var unreadable: [String] = []
    var retainedAfterError = false
    var identityUncertain = false
    enum CodingKeys: String, CodingKey { case schema, built_by, built_confidence, built_family, reviewed_by, reworked_by, rework_runs, tokens_complete, shared_runs, built, reviews, reworks, runs, tokens, tokens_basis, history_complete, history_start }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        var bad: [String] = []
        schema = c.lenient(String.self, .schema, &bad)
        built_by = c.lenient(String.self, .built_by, &bad)
        built_confidence = c.lenient(String.self, .built_confidence, &bad)
        built_family = c.lenient(String.self, .built_family, &bad)
        reviewed_by = c.lenient([String].self, .reviewed_by, &bad)
        reworked_by = c.lenient([String].self, .reworked_by, &bad)
        rework_runs = c.lenient(Int.self, .rework_runs, &bad)
        tokens_complete = c.lenient(Bool.self, .tokens_complete, &bad)
        shared_runs = c.lenient(Int.self, .shared_runs, &bad)
        built = c.lenient(Run.self, .built, &bad)
        reviews = c.lenientArray(Run.self, .reviews, &bad)
        reworks = c.lenientArray(Run.self, .reworks, &bad)
        runs = c.lenientArray(Run.self, .runs, &bad)
        tokens = c.lenient(Tokens.self, .tokens, &bad)
        tokens_basis = c.lenient(String.self, .tokens_basis, &bad)
        history_complete = c.lenient(Bool.self, .history_complete, &bad)
        history_start = c.lenient(Double.self, .history_start, &bad)
        for (label, usage) in [("tokens", tokens), ("built.tokens", built?.tokens)] {
            if let usage, !usage.valid { bad.append(label + ": invalid usage") }
        }
        for (label, list) in [("runs", runs), ("reviews", reviews), ("reworks", reworks)] {
            if (list ?? []).contains(where: { $0.tokens.map { !$0.valid } ?? false }) { bad.append(label + ": invalid usage") }
        }
        unreadable = bad
    }
    var usageEvidenceValid: Bool {
        unreadable.isEmpty && !retainedAfterError && !identityUncertain
            && Self.checkedIdentities(["self": self])["self"]?.identityUncertain == false
    }



    struct RunEvidence {
        let runs: [Run]?
        let reviews: [Run]?
        let reworks: [Run]?
        let built: Run?
    }
    var runEvidence: RunEvidence {
        let copies = (runs ?? []) + (reviews ?? []) + (reworks ?? []) + (built.map { [$0] } ?? [])
        let identified = Dictionary(grouping: copies.filter { !($0.run ?? "").isEmpty }, by: { $0.run! })
        let canonical = identified.compactMapValues { Run.coalesced($0) }
        func resolve(_ run: Run) -> Run { run.run.flatMap { canonical[$0] } ?? run }
        func resolveList(_ list: [Run]?) -> [Run]? { list.map { Self.uniqueRuns($0.map(resolve)) } }
        return RunEvidence(runs: resolveList(runs), reviews: resolveList(reviews), reworks: resolveList(reworks),
                           built: built.map(resolve))
    }

    static let liveSchema = "flightops-models-pr/1"

    var isLive: Bool { schema == Self.liveSchema }

    var isRunLevel: Bool { schema == "pr-models/2" || isLive }

    var threadBuilder: Run? { isRunLevel && built_confidence == "thread" ? runEvidence.built : nil }

    var switchedSetup: Bool { !(threadBuilder?.switched ?? []).isEmpty }


    func historyKnown(opened: Double?) -> Bool {
        guard isRunLevel, history_complete == true, let start = history_start, let opened else { return false }
        return start.isFinite && opened.isFinite && opened >= start
    }


    func historyCut(opened: Double?) -> Bool {
        history_complete == false || history_start.map({ (opened ?? -.infinity) < $0 }) == true
    }

    func builderTokens(opened: Double?) -> Tokens? {
        guard usageEvidenceValid, let b = threadBuilder, b.tokens_complete == true, b.countedTokens != nil,
              (b.switched ?? []).isEmpty, historyKnown(opened: opened) else { return nil }
        return b.tokens
    }


    func allTokens(opened: Double?) -> Tokens? {
        let evidence = runEvidence
        return usageEvidenceValid && isRunLevel && !isLive && tokens_complete == true && tokens?.valid == true
            && evidence.built?.allocation != "shared" && shared_runs.map({ $0 == 0 }) != false && sharedRunIDs.isEmpty
            && !((runs ?? []) + (reviews ?? []) + (reworks ?? [])).contains(where: { $0.allocation == "shared" })
            && [runs ?? [], reviews ?? [], reworks ?? []].allSatisfy({ Self.uniqueRuns($0).count == $0.count })
            && constituentUsageKnown(evidence)
            && historyKnown(opened: opened) ? tokens : nil
    }



    private func constituentUsageKnown(_ evidence: RunEvidence) -> Bool {
        let represented = (evidence.runs ?? []) + (evidence.reviews ?? []) + (evidence.reworks ?? [])
        guard represented.allSatisfy({ $0.tokens_complete != false && $0.countedTokens != nil }) else { return false }
        var arithmetic = represented


        if let built = evidence.built, !(built.run ?? "").isEmpty, built.countedTokens != nil {
            arithmetic.append(built)
        }
        let canonical = Self.uniqueRuns(arithmetic)
        let amounts = canonical.compactMap { $0.countedTokens?.fresh }
        guard amounts.count == canonical.count, Tokens.sum(amounts) != nil else { return false }
        let unnamed = evidence.built.flatMap { ($0.run ?? "").isEmpty ? $0.countedTokens : nil }
        let parts = canonical.compactMap(\.countedTokens)
        if let total = tokens, !parts.isEmpty || unnamed != nil,
           !(Self.bucketsAgree(total, parts) || unnamed.map { Self.bucketsAgree(total, parts + [$0]) } == true) { return false }
        guard let built = evidence.built else { return true }
        if !(built.switched ?? []).isEmpty && !(evidence.runs ?? []).isEmpty { return true }
        return built.tokens_complete != false && built.countedTokens != nil
    }



    func roleRunCount(reworks isRework: Bool) -> Int? {
        let evidence = runEvidence
        let list = isRework ? evidence.reworks : evidence.reviews
        guard usageEvidenceValid, let list else { return nil }
        return list.count
    }

    func reworkSummary(short: (String) -> String = { $0 }) -> String? {
        let unread = unreadable.contains { $0.hasPrefix("reworks") }
        let work = runEvidence.reworks ?? []
        guard !work.isEmpty || unread else { return nil }
        let who = Set(work.compactMap(\.label).map(short)).sorted().joined(separator: ", ")
        let count = !usageEvidenceValid ? "unknown" : history_complete == false ? "at least \(work.count)×" : "\(work.count)×"
        return "reworked " + count + (who.isEmpty ? "" : " (" + who + ")")
    }


    var sharedRunIDs: Set<String> { Set((runs ?? []).filter { $0.allocation == "shared" }.compactMap(\.run)) }



    func linkedEstimate(opened: Double?) -> Tokens? {
        guard isLive, usageEvidenceValid, tokens_basis == LiveModels.linkEstimate, tokens?.valid == true, historyKnown(opened: opened) else { return nil }
        return tokens
    }

    func tokenLine(opened: Double?) -> String {
        if let n = linkedEstimate(opened: opened)?.fresh { return "~" + compactCount(n) + " · no cache reads · estimate from Orch links, exclusive use unproven" }
        if let n = allTokens(opened: opened)?.fresh { return compactCount(n) + " · no cache reads · historical, freshness unknown" }
        if !usageEvidenceValid { return "– · model evidence unreadable or conflicting" }
        if historyCut(opened: opened) { return "– · history not fully covered" }
        if isRunLevel { return "– · usage not fully known" }
        return tokens == nil ? "–" : "– · old report, counted per folder"
    }
}

extension PRModels {

    static func bucketsAgree(_ total: Tokens, _ parts: [Tokens]) -> Bool {
        let buckets: [(Tokens) -> Int?] = [{ $0.input }, { $0.cache_write }, { $0.output }, { $0.cache_read ?? $0.cached_input }, { $0.reasoning }]
        return buckets.allSatisfy { bucket in
            guard let claimed = bucket(total) else { return true }
            return Tokens.sum(parts.map { bucket($0) ?? 0 }) == claimed
        }
    }

    static func uniqueRuns(_ runs: [Run]) -> [Run] {
        let identified = Dictionary(grouping: runs.filter { !($0.run ?? "").isEmpty }, by: { $0.run! })
        var seen: Set<String> = []
        return runs.flatMap { r -> [Run] in
            guard let id = r.run, !id.isEmpty else { return [r] }
            guard seen.insert(id).inserted else { return [] }
            let copies = identified[id] ?? [r]
            if let canonical = Run.coalesced(copies) { return [canonical] }

            var setups: Set<String> = []
            return copies.filter { setups.insert($0.setupKey).inserted }
        }
    }



    static func checkedIdentities(_ models: [String: PRModels]) -> [String: PRModels] {
        var owners: [String: Set<String>] = [:]
        var occurrences: [String: [Run]] = [:]
        var bad: Set<String> = []
        for (pr, pm) in models {
            let lists = [pm.runs ?? [], pm.reviews ?? [], pm.reworks ?? [], pm.built.map { [$0] } ?? []]
            for list in lists {
                for r in list {
                    guard let id = r.run, !id.isEmpty else { continue }
                    owners[id, default: []].insert(pr)
                    occurrences[id, default: []].append(r)
                }
            }
            let reviewIDs = Set((pm.reviews ?? []).compactMap(\.run).filter { !$0.isEmpty })
            if !(pm.reworks ?? []).compactMap(\.run).filter({ reviewIDs.contains($0) }).isEmpty { bad.insert(pr) }
        }
        for (id, rs) in occurrences {
            let conflict = Run.conflictingEvidence(rs)
            let reusedExclusive = (owners[id]?.count ?? 0) > 1 && rs.contains { $0.allocation != "shared" }
            if conflict || reusedExclusive { bad.formUnion(owners[id] ?? []) }
        }
        var result = models
        for pr in bad { result[pr]?.identityUncertain = true }
        return result
    }
}



struct ModelSource {
    var models: [String: PRModels] = [:]
    var loadedAt: Double?
    var fileModifiedAt: Double?
    var attemptedAt: Double?
    var error: String?
    var issues: [String] = []

    mutating func read(_ url: URL, now: Double) {
        attemptedAt = now
        do {
            let data = try Data(contentsOf: url)
            guard let entries = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw CocoaError(.propertyListReadCorrupt)
            }
            var next: [String: PRModels] = [:]
            var bad: [String] = []
            for key in entries.keys.sorted() {
                let value = entries[key]!
                let record = (value as? [String: Any]).flatMap { try? JSONSerialization.data(withJSONObject: $0) }.flatMap { try? JSONDecoder().decode(PRModels.self, from: $0) }
                if let record {
                    if !record.unreadable.isEmpty {
                        bad.append(key + ": " + record.unreadable.joined(separator: ", "))
                        if var old = models[key] { old.retainedAfterError = true; next[key] = old; continue }
                    }
                    next[key] = record
                } else {
                    bad.append(key + ": unreadable record")
                    if var old = models[key] { old.retainedAfterError = true; next[key] = old }
                }
            }
            models = PRModels.checkedIdentities(next)
            issues = bad
            loadedAt = now
            fileModifiedAt = ((try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date)?.timeIntervalSince1970
            error = nil
        } catch {
            self.error = "model publication unavailable or malformed"
        }
    }

    func statusLine(now: Double, live: LiveModels? = nil, liveProblem: String? = nil) -> String {
        if let live { return live.statusLine(now: now) }
        var parts = [loadedAt == nil ? "Models not read" : "Models: historical report"]
        if let liveProblem { parts.append(liveProblem) }
        if let modified = fileModifiedAt {
            let age = now - modified
            if age < -300 { parts.append("file time is in the future") }
            else { parts.append("file age " + duration(Int(min(max(age, 0), Double(Int.max / 2))))) }
            if age > 1800 { parts.append("file older than 30 min") }
        } else { parts.append("file age unknown") }
        if let error { parts.append(error + (loadedAt == nil ? "" : "; retaining last good data")) }
        if !issues.isEmpty { parts.append("unreadable model evidence for \(issues.count) PRs; affected usage unknown") }
        if models.values.contains(where: \.identityUncertain) { parts.append("conflicting or reused run identity; affected usage/counts unknown") }
        parts.append("legacy freshness unknown: run/head coverage unverified")
        return parts.joined(separator: " · ")
    }
}


func compactCount(_ n: Int) -> String {
    n >= 1_000_000 ? String(format: "%.1fM", Double(n) / 1e6) : n >= 1000 ? "\(n / 1000)k" : "\(n)"
}


func compactCount(_ n: Double) -> String {
    guard n.isFinite, n >= 0 else { return "–" }
    return n >= 1_000_000 ? String(format: "%.1fM", n / 1e6) : n >= 1000 ? String(format: "%.0fk", floor(n / 1000)) : String(format: "%.0f", n)
}


enum ModelName {


    static func display(_ id: String) -> String {
        let s = id.lowercased()
        let v = version(s).map { " " + $0 } ?? ""
        if s.hasPrefix("claude") {
            for tier in ["opus", "sonnet", "haiku", "fable"] where s.contains(tier) { return "Claude " + tier.capitalized + v }
            return "Claude" + v
        }
        if s.hasPrefix("o"), s.count <= 4 { return "Codex " + s }
        if s.hasPrefix("gpt") || s.hasPrefix("codex") {
            for tier in ["sol", "luna", "terra", "astra"] where s.contains(tier) { return "Codex " + tier.capitalized + v }
            return "Codex" + v
        }
        for fam in ["grok", "gemini", "muse", "cursor", "devin", "copilot", "mistral", "llama", "deepseek", "qwen"] where s.hasPrefix(fam) {
            return fam.capitalized + v
        }
        return id
    }


    static func version(_ id: String) -> String? {
        let parts: [String] = id.split(separator: "-").map(String.init).filter { p in
            p.count < 6 && p.first?.isNumber == true && p.allSatisfy { $0.isNumber || $0 == "." }
        }
        return parts.isEmpty ? nil : parts.joined(separator: ".")
    }


    static func fromBranch(_ branch: String?) -> String? {
        guard let b = branch?.lowercased(), let slash = b.firstIndex(of: "/") else { return nil }
        let prefix = String(b[..<slash])
        let known = ["claude", "codex", "grok", "gemini", "muse", "cursor", "devin", "copilot", "jules", "aider"]
        return known.contains(prefix) ? prefix.capitalized : nil
    }


    static func family(_ label: String?) -> String? {
        label.flatMap { $0.split(separator: " ").first.map(String.init) }
    }



    static func builderLine(models: PRModels?, timelineBuilder: String?) -> String? {
        if let b = models?.threadBuilder {
            return (b.label ?? "–") + (b.harness.map { " · \($0)" } ?? "")
                + ((b.switched ?? []).isEmpty ? "" : " · then another " + (b.switched ?? []).joined(separator: ", "))
        }
        return (models?.built_family ?? family(timelineBuilder)).map { $0 + " (family)" }
    }




    static func builder(branch: String?, models: PRModels?) -> String? {
        if let exact = models?.threadBuilder?.label { return exact }
        return fromBranch(branch) ?? models?.built_family
    }
}

extension Analytics {

    enum ModelGroup: String, CaseIterable {
        case setup = "Setup", model = "Model", effort = "Effort", harness = "Harness"
    }



    enum ModelMetric: String, CaseIterable {
        case ci = "No CI fix", rework = "Rework", tokens = "Tokens", merge = "To merge"
    }



    struct ModelStat: Identifiable {

        let key: String
        let name: String
        let prs: Int
        let ciRan: Int
        let merged: Int

        let ciPass: Double?

        let rework: Double?

        let loopsPerPR: Double?

        let mergeAvg: Double?


        let tokensPerPR: Double?

        let tokenPRs: Int

        let inferred: Bool
        var id: String { (inferred ? "~" : "") + key }

        func denominator(_ metric: ModelMetric) -> Int {
            switch metric {
            case .ci: return ciRan
            case .rework: return prs
            case .tokens: return tokenPRs
            case .merge: return merged
            }
        }
    }



    struct AgentStat: Identifiable {
        let key: String
        let name: String
        let prs: Int
        let runsPerPR: Double?
        let tokensPerPR: Double?
        let tokenPRs: Int
        let mergedShare: Double

        let legacy: Bool
        var estimate = false
        var id: String { key }
        var tokensText: String { tokensPerPR.map { (estimate ? "~" : "") + compactCount($0) } ?? "–" }
    }



    struct GroupKey: Equatable {
        let key: String
        let name: String
        let full: String
        let inferred: Bool
    }



    private static func ciRan(_ t: PRTimeline) -> Bool {
        t.spans.contains { $0.status == PRStatus.inCI.rawValue }
            && (ciFailed(t) || t.spans.contains { (PRStatus(rawValue: $0.status).map(rank) ?? -1) >= 3 })
    }

    private static func ciFailed(_ t: PRTimeline) -> Bool {
        zip(t.spans, t.spans.dropFirst()).contains { a, b in
            [PRStatus.inCI.rawValue, PRStatus.waitingCI.rawValue].contains(a.status) && b.status == PRStatus.fixing.rawValue
        }
    }

    private static func mergeTime(_ t: PRTimeline) -> Double? {
        guard let last = t.spans.last, last.status == PRStatus.merged.rawValue, let o = t.opened else { return nil }
        return last.from - o
    }




    static func groupKey(_ t: PRTimeline, _ pm: PRModels?, by: ModelGroup) -> GroupKey {
        if let b = pm?.threadBuilder {
            if b.model == "mixed" { return GroupKey(key: "mixed", name: "Mixed", full: "Mixed", inferred: false) }
            let model = b.model.map(ModelName.display) ?? "Unknown model"
            let raw = " (" + (b.model ?? "?") + ")"
            switch by {
            case .setup:
                let name = model + " · " + (b.effort ?? "unknown effort")
                return GroupKey(key: b.setupKey, name: name, full: name + " · " + (b.harness ?? "unknown harness") + raw, inferred: false)
            case .model: return GroupKey(key: b.model ?? "?", name: model, full: model + raw, inferred: false)
            case .effort:
                let name = b.effort?.capitalized ?? "Unknown"
                return GroupKey(key: b.effort ?? "?", name: name, full: b.effort ?? name, inferred: false)
            case .harness: return GroupKey(key: b.harness ?? "?", name: b.harness ?? "Unknown", full: b.harness ?? "Unknown", inferred: false)
            }
        }
        if let fam = pm?.built_family ?? ModelName.family(t.builder) {
            return GroupKey(key: "family:" + fam, name: fam, full: fam, inferred: true)
        }
        return GroupKey(key: "unknown", name: "Unknown", full: "Unknown", inferred: true)
    }


    private static func shown(_ gs: [(key: String, name: String, full: String)]) -> [String: String] {
        let taken = Dictionary(grouping: gs, by: \.name).filter { $0.value.count > 1 }
        return Dictionary(uniqueKeysWithValues: gs.map { ($0.key, taken[$0.name] == nil ? $0.name : $0.full) })
    }



    static func modelStats(_ all: [String: PRTimeline], models: [String: PRModels], since: Double, now: Double,
                           by: ModelGroup = .setup) -> [ModelStat] {
        let models = PRModels.checkedIdentities(models)
        var groups: [String: (g: GroupKey, pairs: [(String, PRTimeline)])] = [:]
        for (id, t) in all where !clipped(t, since: since, now: now).isEmpty {
            let k = groupKey(t, models[id], by: by)
            groups[(k.inferred ? "~" : "") + k.key, default: (k, [])].pairs.append((id, t))
        }
        let names = shown(groups.map { ($0.key, $0.value.g.name, $0.value.g.full) })
        return groups.map { id, v in
            let g = v.g, ts = v.pairs.map(\.1)
            if g.inferred {
                return ModelStat(key: g.key, name: names[id] ?? g.name, prs: ts.count, ciRan: 0, merged: 0, ciPass: nil, rework: nil,
                                 loopsPerPR: nil, mergeAvg: nil, tokensPerPR: nil, tokenPRs: 0, inferred: true)
            }
            let ran = ts.filter(ciRan)
            let spent = v.pairs.compactMap { models[$0.0]?.builderTokens(opened: $0.1.opened)?.fresh }.map(Double.init)
            let merges = ts.compactMap(mergeTime)
            let loops = ts.map { Double(rework($0, now: now).loops) }
            return ModelStat(key: g.key, name: names[id] ?? g.name, prs: ts.count, ciRan: ran.count, merged: merges.count,
                             ciPass: ran.isEmpty ? nil : Double(ran.filter { !ciFailed($0) }.count) / Double(ran.count),
                             rework: Double(loops.filter { $0 > 0 }.count) / Double(ts.count), loopsPerPR: mean(loops),
                             mergeAvg: mean(merges), tokensPerPR: mean(spent), tokenPRs: spent.count, inferred: false)
        }
        .sorted { ($0.inferred ? 1 : 0, -$0.prs, $0.name) < ($1.inferred ? 1 : 0, -$1.prs, $1.name) }
    }




    static func attributionLines(_ rows: [ModelStat]) -> [String] {
        let total = rows.reduce(0) { $0 + $1.prs }
        return rows.filter(\.inferred).sorted { $0.name < $1.name }
            .map { ($0.key == "unknown" ? $0.name : $0.name + " (family)") + ": \($0.prs) of \(total) PRs" }
    }




    static func agentStats(_ all: [String: PRTimeline], models: [String: PRModels], since: Double, now: Double,
                           reworks: Bool, linked: Bool = false) -> [AgentStat] {
        let models = PRModels.checkedIdentities(models)
        var runs: [String: (name: String, full: String, legacy: Bool, rs: [(pr: String, tokens: Int?, countKnown: Bool)])] = [:]
        for (id, t) in all where !clipped(t, since: since, now: now).isEmpty {
            guard let pm = models[id] else { continue }
            let legacy = !pm.isRunLevel, whole = pm.historyKnown(opened: t.opened)
            let evidence = pm.runEvidence
            let chosen = linked ? evidence.runs?.filter { $0.role == LiveModels.linkedRole } : reworks ? evidence.reworks : evidence.reviews
            for r in chosen ?? [] {
                let name = (r.label ?? "Unknown model") + (legacy ? " (old report)" : "")
                let full = name + " · " + (r.harness ?? "unknown harness") + " (" + (r.model ?? "?") + ")"
                runs[(legacy ? "v1|" : "") + r.setupKey, default: (name, full, legacy, [])].rs
                    .append((id, !legacy && whole && pm.usageEvidenceValid ? r.countedTokens?.fresh : nil, pm.usageEvidenceValid && !pm.historyCut(opened: t.opened)))
            }
        }
        let names = shown(runs.map { ($0.key, $0.value.name, $0.value.full) })
        return runs.map { key, v in
            let byPR = Dictionary(grouping: v.rs, by: \.pr)
            let merged = byPR.keys.filter { all[$0].flatMap(mergeTime) != nil }.count
            let spent = byPR.values.compactMap { runs -> Double? in
                let t = runs.compactMap(\.tokens)
                return t.count == runs.count ? PRModels.Tokens.sum(t).map(Double.init) : nil
            }
            return AgentStat(key: key, name: names[key] ?? v.name, prs: byPR.count, runsPerPR: v.rs.allSatisfy(\.countKnown) ? Double(v.rs.count) / Double(byPR.count) : nil,
                             tokensPerPR: mean(spent), tokenPRs: spent.count, mergedShare: Double(merged) / Double(byPR.count),
                             legacy: v.legacy, estimate: byPR.keys.contains { models[$0]?.isLive == true })
        }
        .sorted { ($0.legacy ? 1 : 0, -$0.prs, $0.name) < ($1.legacy ? 1 : 0, -$1.prs, $1.name) }
    }



    struct Gate: Identifiable {
        enum State { case met, notMet, notYet }
        let id: String
        let label: String
        let state: State

        let detail: String

        var short: String? = nil
    }




    struct Measure: Identifiable {
        let id: String
        let title: String
        let verdict: String
        let gates: [Gate]

        var reason: String {
            (gates.first { $0.state == .notMet } ?? gates.first { $0.state == .notYet }).map { $0.label + ": " + ($0.short ?? $0.detail) } ?? ""
        }
        var unmet: [Gate] { gates.filter { $0.state != .met } }
    }

    static let noCall = "No clear difference yet"




    static func bestArea(_ all: [String: PRTimeline], models: [String: PRModels], facts: [String: PRFact] = [:],
                         factsPartial: [String] = [], since: Double, now: Double) -> (best: [Measure], attention: Measure, notes: [String]) {
        let models = PRModels.checkedIdentities(models)
        let ids = all.filter { !clipped($0.value, since: since, now: now).isEmpty }.map(\.key)
        let n = ids.count
        let sampled = ids.filter { all[$0].map(ciRan) == true }.count
        let thread = ids.filter { models[$0]?.threadBuilder != nil }.count
        let groups = ids.map { groupKey(all[$0]!, models[$0], by: .setup) }
        let unknown = groups.filter { $0.key == "unknown" }.count
        let family = groups.filter { $0.inferred && $0.key != "unknown" }.count

        let single = ids.filter { id in models[id].map { $0.threadBuilder?.model.map { $0 != "mixed" } == true && !$0.switchedSetup } == true }
        let complete = single.filter { models[$0]?.builderTokens(opened: all[$0]?.opened) != nil }.count
        let switched = ids.filter { models[$0]?.switchedSetup == true }.count
        let cut = ids.filter { id in models[id].map { $0.isRunLevel && !$0.historyKnown(opened: all[id]?.opened) } == true }.count
        let oldReports = ids.filter { models[$0].map { !$0.isRunLevel } == true }.count
        let shared = Set(ids.flatMap { models[$0]?.sharedRunIDs ?? [] }).count

        func gate(_ id: String, _ label: String, _ state: Gate.State, _ detail: String, short: String? = nil) -> Gate {
            Gate(id: id, label: label, state: state, detail: detail, short: short)
        }
        let precision = gate("precision", "Precision", .notYet, "intervals aren't computed yet")
        let support = gate("support", "Common support", .notYet, "needs sizes at the first-ready commit")
        let effect = gate("effect", "Adjusted effect", .notYet, "isn't computed yet")
        let calibration = gate("calibration", "Calibrated call rules", .notYet, "not built yet")


        let fs = factsSummary(facts, since: since, now: now)
        let v = fs.r0["verified"] ?? 0
        let decided = (fs.firstTry["verified"]?.pass ?? 0) + (fs.firstTry["verified"]?.fail ?? 0)
        let reasons = fs.unknownReasons.sorted { ($1.value, $0.key) < ($0.value, $1.key) }.map { "\($0.value) \($0.key)" }
        let ci = facts.isEmpty ? Measure(id: "ci", title: "Most passing CI", verdict: noCall, gates: [
            gate("inputs", "Per-attempt CI", .notMet, "unavailable; sampled observations only, \(sampled) of \(n) PRs"),
            gate("r0", "Verified first-ready commit", .notMet, "no GitHub receipts read yet"),
            gate("producer", "Exact producer", .notMet, "builder thread known for \(thread) of \(n) PRs; commits aren't tied to runs yet"),
            gate("coverage", "Coverage or worst-case bounds", .notMet, "no verified CI values"),
            precision, support, effect, calibration])
            : Measure(id: "ci", title: "Most passing CI", verdict: noCall, gates: [
                gate("inputs", "Verified first-ready commit", fs.prs > 0 && v == fs.prs && factsPartial.isEmpty ? .met : .notMet,
                     (fs.prs == 0 ? "no PR first ready in this window"
                        : "\(v) of \(fs.prs) PRs; \(fs.r0["supported"] ?? 0) from push events, \(fs.r0["candidate"] ?? 0) candidates")
                        + (factsPartial.isEmpty ? "" : "; partial: " + factsPartial.joined(separator: ", ")),
                     short: fs.prs == 0 ? nil : "\(v) of \(fs.prs) PRs" + (factsPartial.isEmpty ? "" : ", partial")),
                gate("decided", "First-try result", v > 0 && decided == v ? .met : .notMet,
                     v == 0 ? "no verified first-ready commit"
                        : "\(decided) of \(v) verified first-ready commits" + (reasons.isEmpty ? "" : "; unknown: " + reasons.joined(separator: ", "))),
                gate("producer", "Exact producer", .notMet, "none: commits aren't tied to runs yet (builder thread known for \(thread) of \(n) PRs)"),
                gate("coverage", "Coverage or worst-case bounds", .notMet, "no verified first try with an exact producer"),
                precision, support, effect, calibration])
        let rework = Measure(id: "rework", title: "Least rework", verdict: noCall, gates: [
            gate("inputs", "Verified rework", .notMet, "unavailable; sampled status changes only"),
            gate("assignment", "Assignment receipts", .notMet, "the conductor doesn't record them yet"),
            gate("coverage", "Coverage or worst-case bounds", .notMet, "no verified rework values"),
            precision, support, effect, calibration])
        let usage: Gate
        if oldReports > 0 && thread == 0 {
            usage = gate("inputs", "Complete builder usage", .notMet, "only workspace totals (old report) for \(oldReports) PRs")
        } else {
            usage = gate("inputs", "Complete builder usage", !single.isEmpty && complete == single.count ? .met : .notMet,
                         "\(complete) of \(single.count) single-setup builder PRs")
        }


        let setups = modelStats(all, models: models, since: since, now: now).filter { !$0.inferred && $0.key != "mixed" }
        let below = setups.filter { Double($0.tokenPRs) < 0.9 * Double($0.prs) }.count
        let coverage = setups.isEmpty ? gate("coverage", "Coverage or worst-case bounds", .notMet, "no setup with a builder thread")
            : below == 0 ? gate("coverage", "Coverage or worst-case bounds", .met, "every setup has ≥ 90% of its PRs complete")
            : gate("coverage", "Coverage or worst-case bounds", .notYet, "\(below) of \(setups.count) setups below 90%; bounds aren't computed yet")
        let freshness = gate("freshness", "Publication coverage", .notMet, "legacy model report: run/head freshness unverified")
        let tokens = Measure(id: "tokens", title: "Fewest tokens", verdict: noCall, gates: [usage, freshness, coverage, precision, support, effect, calibration])
        let short = [(ci, "CI"), (rework, "rework"), (tokens, "tokens")].filter { $0.0.gates[0].state != .met }.map(\.1)
        let attention = Measure(id: "attention", title: "Nothing flagged", verdict: "No call yet", gates: [
            gate("inputs", "Verified inputs", short.isEmpty ? .met : .notMet, short.isEmpty ? "every measure" : "missing for " + short.joined(separator: ", ")),
            gate("coverage", "Coverage or worst-case bounds", .notYet, "as for each measure above"),
            precision, support,
            gate("effect", "Adjusted deficit", .notYet, "beyond 10 points or ×1.5 against the reference; isn't computed yet"),
            calibration])
        var notes: [String] = []
        if family > 0 { notes.append("\(family) PRs with only a family") }
        if unknown > 0 { notes.append("\(unknown) with no builder known") }
        if complete < single.count { notes.append("\(single.count - complete) with incomplete builder usage") }
        if switched > 0 { notes.append("\(switched) switched setup after the build") }
        if cut > 0 { notes.append("\(cut) with PR history not fully covered") }
        if shared > 0 { notes.append("\(shared) runs shared between PRs") }
        if fs.heads > fs.headsComplete { notes.append("\(fs.heads - fs.headsComplete) heads without full CI history") }
        return ([ci, rework, tokens], attention, notes)
    }
}














struct PRFactsFile: Decodable {
    struct Gap: Decodable, Equatable {
        let after: Double?
        let before: Double?
        let detected: Double?
    }
    struct EventsRepo: Decodable {
        let polled: Double?
        let covered_since: Double?
        let gap_list: [Gap]?
        let error: String?
    }
    struct Events: Decodable {
        let repos: [String: EventsRepo]?
    }
    struct Facts: Decodable {
        let discovered: Int?
        let discovery_complete: Bool?
        let discovered_at: Double?
        let unfetched: [String]?
        let stale: [String]?


        let last_clean_read: Double?

        let due_heads: Int?
        let errors: [String]?
    }
    struct Run: Decodable {
        let at: Double?
        let errors: [String]?
    }
    struct Sources: Decodable {
        let facts: Facts?
        let events: Events?
        let run: Run?
    }
    let schema: String?
    let generated: Double?
    let sources: Sources?
    let prs: [String: PRFact]?

    let unreadable: [String]

    private struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init?(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }
    enum CodingKeys: String, CodingKey { case schema, generated, sources, prs }


    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = try? c.decodeIfPresent(String.self, forKey: .schema)
        generated = try? c.decodeIfPresent(Double.self, forKey: .generated)
        sources = try? c.decodeIfPresent(Sources.self, forKey: .sources)
        var bad: [String] = []
        if c.contains(.prs), let pc = try? c.nestedContainer(keyedBy: Key.self, forKey: .prs) {
            var out: [String: PRFact] = [:]
            for k in pc.allKeys {
                if let f = try? pc.decode(PRFact.self, forKey: k) { out[k.stringValue] = f } else { bad.append(k.stringValue) }
            }
            prs = out
        } else {
            prs = nil
        }
        unreadable = bad.sorted()
    }



    var partial: [String] {
        guard let f = sources?.facts else { return ["no record of what discovery found"] }
        var out: [String] = []
        if let n = f.unfetched?.count, n > 0 { out.append("\(n) PRs not fetched yet") }
        if let n = f.stale?.count, n > 0 { out.append("\(n) changed since they were fetched") }
        if f.discovery_complete != true { out.append("discovery didn't read every page") }
        if let n = f.due_heads, n > 0 { out.append("\(n) head histories due to be read again") }
        if !unreadable.isEmpty { out.append("\(unreadable.count) PR records unreadable") }
        return out
    }


    func eventGaps(since: Double) -> [String: Int] {
        (sources?.events?.repos ?? [:]).compactMapValues { r in
            let n = (r.gap_list ?? []).filter { ($0.detected ?? 0) >= since }.count
            return n > 0 ? n : nil
        }
    }
}

struct PRFact: Decodable {
    struct Outcome: Decodable, Equatable {
        let outcome: String
        let reason: String?
        let failed: [String]?
    }
    struct Head: Decodable {
        let sha: String
        let observed: Double?
        let observed_via: String?
        let sync: Bool?
        let replaced: Bool?
        let complete: Bool?
        let family: String?
        let first_attempt: Outcome?
        let first_completed: Outcome?
        let latest: Outcome?
    }
    struct R0: Decodable {
        let sha: String

        let status: String
        let evidence: String?
        let delay_s: Double?
        let first_attempt: Outcome
        let family: String?
    }
    struct Decided: Decodable {
        let sha: String
        let outcome: String
        let failed: [String]?
    }
    let pr: String?
    let state: String?
    let anchor: Double?

    let anchor_kind: String?
    let opened: Double?
    let merged: Double?

    let policy: String?

    let policy_source: String?
    let heads: [Head]
    let r0: R0?
    let first_decided: Decided?
    let history_complete: Int?

    let history_read: [Double]?

    let incomplete: [String]?

    static func short(_ sha: String) -> String { String(sha.prefix(7)) }


    static func said(_ o: Outcome) -> String {
        switch o.outcome {
        case "pass": return "passed"
        case "fail": return "failed" + ((o.failed ?? []).isEmpty ? "" : " (" + (o.failed ?? []).joined(separator: ", ") + ")")
        default: return "unknown" + (o.reason.map { " (" + $0 + ")" } ?? "")
        }
    }




    func ciLines(now: Double) -> [String] {
        var lines: [String] = []
        let opening = anchor_kind == "ready" ? "ready" : "opening"
        if let r = r0 {
            let first = "first try " + PRFact.said(r.first_attempt)
            switch r.status {
            case "verified": lines.append("First-ready commit \(PRFact.short(r.sha)), verified (opened event) · \(first)")
            case "supported": lines.append("First-ready commit \(PRFact.short(r.sha)), from push events, not verified · \(first)")
            default:
                let delay = r.delay_s.map { ", \(Int($0)) s after \(opening)" } ?? ""
                lines.append("Candidate first-ready commit \(PRFact.short(r.sha)) (\(r.evidence ?? "first PR run")\(delay)) · \(first)")
            }
        } else {
            let why = anchor_kind == "incomplete" ? "readiness history not fully read"
                : anchor == nil ? "not ready for review yet" : "no receipt or PR run on its branch"
            lines.append("First-ready commit unknown (\(why))")
        }
        if let d = first_decided, d.sha != r0?.sha {
            lines.append("First decided work head \(PRFact.short(d.sha)) \(PRFact.said(Outcome(outcome: d.outcome, reason: nil, failed: d.failed)))")
        }
        if policy_source == "observed" {
            lines.append("Main's required checks weren't read: no pass is claimed")
        } else if policy == "tracked" {
            lines.append("Main requires no checks: every job counts")
        }
        if state == "OPEN", let oldest = history_read?.first, now - oldest > 3600 {
            lines.append("History read \(duration(Int(now - oldest))) ago")
        }
        if let gaps = incomplete, let first = gaps.first {
            lines.append("Incomplete: " + first + (gaps.count > 1 ? " (+\(gaps.count - 1) more)" : ""))
        }
        return lines
    }


    enum Mark: Equatable {
        case head(outcome: String, sync: Bool, replaced: Bool, exactPush: Bool, r0: Bool)

        case gap
        case merged
    }

    func strip(gap: Double = 3600) -> [Mark] {
        var out: [Mark] = []
        var last: Double?
        for h in heads {
            if let t = h.observed, let l = last, t - l > gap { out.append(.gap) }
            last = h.observed ?? last
            out.append(.head(outcome: h.first_attempt?.outcome ?? "unknown", sync: h.sync == true, replaced: h.replaced == true,
                             exactPush: h.observed_via == "push event", r0: h.sha == r0?.sha))
        }
        if merged != nil { out.append(.merged) }
        return out
    }


    var stripSpoken: String {
        let first = heads.map { $0.first_attempt?.outcome ?? "unknown" }
        let r0At = heads.firstIndex { $0.sha == r0?.sha }.map { ", the first-ready commit is head \($0 + 1)" } ?? ""
        return "\(heads.count) heads, first try: \(first.filter { $0 == "pass" }.count) passed, \(first.filter { $0 == "fail" }.count) failed, "
            + "\(first.filter { $0 != "pass" && $0 != "fail" }.count) unknown\(r0At)" + (merged != nil ? ", merged" : "")
    }
}

extension Analytics {


    struct FactsSummary: Equatable {
        struct Tally: Equatable {
            var pass = 0, fail = 0, unknown = 0
            var total: Int { pass + fail + unknown }
            mutating func add(_ outcome: String) {
                switch outcome {
                case "pass": pass += 1
                case "fail": fail += 1
                default: unknown += 1
                }
            }
        }
        var prs = 0


        var readinessUnknown = 0

        var r0: [String: Int] = ["verified": 0, "supported": 0, "candidate": 0, "unknown": 0]

        var firstTry: [String: Tally] = ["verified": Tally(), "supported": Tally(), "candidate": Tally()]

        var unknownReasons: [String: Int] = [:]
        var heads = 0
        var headsComplete = 0

        var trailerLabels: [String: Int] = [:]
    }

    static func factsSummary(_ prs: [String: PRFact], since: Double, now: Double) -> FactsSummary {
        var s = FactsSummary()
        for r in prs.values {
            if r.anchor_kind == "incomplete", let o = r.opened, o >= since, o <= now { s.readinessUnknown += 1 }
            guard let a = r.anchor, a >= since, a <= now else { continue }
            s.prs += 1
            s.heads += r.heads.count
            s.headsComplete += r.heads.filter { $0.complete == true }.count
            guard let r0 = r.r0 else { s.r0["unknown", default: 0] += 1; continue }
            let outcome = r0.first_attempt.outcome
            s.r0[r0.status, default: 0] += 1
            s.firstTry[r0.status, default: FactsSummary.Tally()].add(outcome)
            if r0.status == "verified" && outcome != "pass" && outcome != "fail" {
                s.unknownReasons[r0.first_attempt.reason ?? "unknown", default: 0] += 1
            }
            s.trailerLabels[r0.family ?? "none", default: 0] += 1
        }
        return s
    }


    static func trailerName(_ label: String) -> String {
        switch label {
        case "none": return "No trailer"
        case "mixed": return "Mixed"
        case "other": return "Other"
        default: return label.split(separator: "-").map { $0.capitalized }.joined(separator: " ")
        }
    }


    static func trailerLines(_ f: FactsSummary) -> [String] {
        let labels = f.trailerLabels.map { (name: trailerName($0.key), prs: $0.value) }.sorted { $0.name < $1.name }
        let unattributed = f.r0["unknown"] ?? 0
        let rows = labels + (unattributed > 0 ? [(name: "No first-ready commit", prs: unattributed)] : [])
        return rows.map { "\($0.name): \($0.prs) of \(f.prs) PRs" }
    }


    static func evidenceName(_ status: String) -> String {
        switch status {
        case "verified": return "Verified"
        case "supported": return "Push events"
        default: return "Candidate"
        }
    }


    static func firstTryLines(_ f: FactsSummary) -> [String] {
        ["verified", "supported", "candidate"].compactMap { status in
            f.firstTry[status].flatMap { $0.total > 0 ? "\(evidenceName(status)): \(tallyWords($0)) of \($0.total)" : nil }
        }
    }


    enum Fitness: String, CaseIterable {
        case decisionGrade = "Decision grade", withCorrection = "Usable with correction", doNotUse = "Do not use"
    }


    enum EvidenceField: String, CaseIterable {
        case prSize = "PR size", openToMerge = "Open-to-merge time"
        case requestedSettings = "Requested settings", sameHeadCI = "Same-head CI", verdictClaims = "Verdict claims", tokensPerRun = "Tokens per run"
        case trailerLabels = "Trailer labels", firstPushedHead = "First-pushed head", failureCause = "Failure cause", realCatches = "Real catches"
        case falsePositives = "False positives", perPRAllocation = "Per-PR allocation", escapedBugs = "Escaped bugs"

        var fitness: Fitness {
            switch self {
            case .prSize, .openToMerge: return .decisionGrade
            case .requestedSettings, .sameHeadCI, .verdictClaims, .tokensPerRun: return .withCorrection
            case .trailerLabels, .firstPushedHead, .failureCause, .realCatches, .falsePositives, .perPRAllocation, .escapedBugs: return .doNotUse
            }
        }
        var label: String { rawValue + ": " + fitness.rawValue }
    }


    static var fitnessLegend: String {
        Fitness.allCases.map { grade in
            grade.rawValue + ": " + EvidenceField.allCases.filter { $0.fitness == grade }.map(\.rawValue).joined(separator: ", ") + "."
        }.joined(separator: " ")
    }


    static func tallyWords(_ t: FactsSummary.Tally?) -> String {
        guard let t, t.total > 0 else { return "–" }
        let parts: [String?] = [t.pass > 0 ? "\(t.pass) passed" : nil, t.fail > 0 ? "\(t.fail) failed" : nil,
                                t.unknown > 0 ? "\(t.unknown) unknown" : nil]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }


    static func factsCounts(_ file: PRFactsFile?, _ f: FactsSummary) -> (prs: String, verified: String, heads: String, complete: String) {
        guard file?.prs != nil else { return ("–", "–", "–", "–") }
        return (String(f.prs), String(f.r0["verified"] ?? 0), String(f.heads), String(f.headsComplete))
    }

    static func mergedCount(_ rows: [RepoRow]) -> Int? {
        let counts = rows.compactMap(\.merged_24h)
        return counts.count == rows.count ? PRModels.Tokens.sum(counts) : nil
    }

    static func mergedCount(_ snapshot: RepoSnap?) -> Int? {
        guard let snapshot, snapshot.repoRowsComplete, let rows = snapshot.repos else { return nil }
        return mergedCount(rows)
    }


    static func openShare(_ snapshot: RepoSnap?) -> Double? {
        guard let snapshot, snapshot.openInventoryComplete, let open = snapshot.prs,
              let merged = mergedCount(snapshot), let total = PRModels.Tokens.sum([open.count, merged]), total > 0 else { return nil }
        return Double(open.count) / Double(total)
    }

    struct RepoCounts {
        let counts: [Int?]
        let mergedShare: Double?
        let note: String
    }


    static func repoCounts(_ snapshot: RepoSnap?, repo: String) -> RepoCounts {
        let row = snapshot?.repos?.first { $0.name == repo }
        let open = snapshot?.prs?.filter { $0.repo == repo }
        return repoCounts(row, open: open, inventoryComplete: snapshot?.repoRowsComplete == true && snapshot?.prRowsComplete == true,
                          publicationNote: snapshot?.inventoryNote ?? "repository publication unknown; observed counts only")
    }

    private static func repoCounts(_ row: RepoRow?, open: [PRRow]?, inventoryComplete: Bool, publicationNote: String) -> RepoCounts {
        let counts: [Int?] = PRStatus.allCases.map { status in
            switch status {
            case .draft: return row?.drafts.flatMap { $0 >= 0 ? $0 : nil }
            case .merged: return row?.merged_24h.flatMap { $0 >= 0 ? $0 : nil }
            default: return row == nil ? nil : open?.filter { PRStatus.of($0) == status }.count
            }
        }
        let total = counts.count == counts.compactMap({ $0 }).count ? PRModels.Tokens.sum(counts.compactMap { $0 }) : nil
        let share = inventoryComplete && row?.truncated == false && open != nil ? total.flatMap { t in
            t > 0 ? counts[PRStatus.merged.rawValue].map { Double($0) / Double(t) } : nil
        } : nil
        var notes: [String] = publicationNote.isEmpty ? [] : [publicationNote]
        if row == nil { notes.append("repository data unknown") }
        if open == nil { notes.append("open PR inventory unknown") }
        if counts[PRStatus.merged.rawValue] == nil { notes.append("merged count unknown") }
        if counts[PRStatus.draft.rawValue] == nil { notes.append("draft count unknown") }
        if row != nil && row?.truncated != false { notes.append("open list incomplete or completeness unknown; observed counts only") }
        return RepoCounts(counts: counts, mergedShare: share, note: notes.joined(separator: " · "))
    }

    static func repoStatusLines(_ snapshot: RepoSnap?) -> [String] {
        guard let snapshot else { return ["Repositories: publication unknown"] }
        var lines = (snapshot.repos ?? []).map { row in
            let line = repoStatusLine(row)
            return snapshot.openInventoryComplete ? line : line.replacingOccurrences(of: " open,", with: " observed open,")
        }
        if !snapshot.inventoryNote.isEmpty { lines.append("Repositories: " + snapshot.inventoryNote) }
        return lines
    }

    static func repoStatusLine(_ row: RepoRow) -> String {
        let merged = row.merged_24h.flatMap { $0 >= 0 ? "+\($0)" : nil } ?? "unknown"
        return "\(row.label): \(row.open) open, main \(row.main ?? "unknown"), \(merged) merged in 24h"
            + (row.truncated == false ? "" : " · open list incomplete or completeness unknown")
    }



    static func coverageLine(_ file: PRFactsFile?, _ f: FactsSummary, since: Double, now: Double, failedAt: Double? = nil) -> String {
        guard let file, file.prs != nil else { return "Coverage · GitHub receipts not read yet" }
        var parts: [String] = []
        let partial = file.partial
        parts.append(partial.isEmpty ? "Coverage" : "Coverage (partial: " + partial.joined(separator: ", ") + ")")
        if f.prs == 0 {
            parts.append("no PR first ready in this window")
        } else {
            parts.append("\(f.prs) PRs first ready")
            parts.append("first-ready commit verified \(f.r0["verified"] ?? 0), from push events \(f.r0["supported"] ?? 0), "
                         + "candidate \(f.r0["candidate"] ?? 0), unknown \(f.r0["unknown"] ?? 0)")
            parts.append("full CI history \(f.headsComplete) of \(f.heads) heads")
        }
        if f.readinessUnknown > 0 { parts.append("\(f.readinessUnknown) with readiness not fully read") }
        let gaps = file.eventGaps(since: since)
        if !gaps.isEmpty {
            parts.append("events feed gaps: " + gaps.keys.sorted().map { "\(repoLabel($0)) \(gaps[$0]!)" }.joined(separator: ", "))
        }

        if let clean = file.sources?.facts?.last_clean_read {
            if now - clean > 1800 { parts.append("GitHub last read without errors \(duration(Int(now - clean))) ago") }
        } else {
            parts.append("GitHub not yet read without errors")
        }
        let quiet = (file.sources?.events?.repos ?? [:]).filter { now - ($0.value.polled ?? 0) > 1800 }.keys.sorted()
        if !quiet.isEmpty { parts.append("events feed not read for 30 min: " + quiet.map(repoLabel).joined(separator: ", ")) }
        if let g = file.generated, now - g > 900 { parts.append("published \(duration(Int(now - g))) ago") }
        if let t = failedAt, t > (file.generated ?? 0) {
            parts.append("the last refresh failed")
        } else if !(file.sources?.run?.errors ?? []).isEmpty {
            parts.append("the last refresh had errors")
        }
        return parts.joined(separator: " · ")
    }
}



enum AgentKind: String, CaseIterable {
    case review = "Reviews", fix = "Fixes", build = "Builds", other = "Other"

    static func of(_ title: String) -> AgentKind {
        let t = title.lowercased()
        if t.contains("review") && !t.contains("fix review") && !t.hasPrefix("fix") { return .review }
        if t.hasPrefix("fix") || t.hasPrefix("resolve") || t.contains("rework") || t.contains("conflict") { return .fix }
        if t.hasPrefix("out-") || t.contains("build") || t.contains(": ") && t.hasPrefix("conductor") { return .build }
        return .other
    }


    static func counts(_ titles: [String]) -> [(AgentKind, Int)] { tally(titles.map(of)) }

    static func tally(_ kinds: [AgentKind]) -> [(AgentKind, Int)] {
        let c = Dictionary(grouping: kinds) { $0 }.mapValues(\.count)
        return allCases.compactMap { k in c[k].map { (k, $0) } }
    }
}


struct FlowItem: Identifiable, Equatable {
    let id: String
    let repo: String
    let number: Int
    let title: String
    let mark: Health?
    let approved: Bool
    let timer: String?
    let progress: Double?
    let rank: Int
    var order: Double = 0
    var opened: Date? = nil
    var merged: Date? = nil
    var aging = false
    var conflicts = false
    var progressKnown = false
}


struct StorageFact: Equatable {
    static let maxAge: TimeInterval = 2 * 3600
    var known = false
    var state: String? = nil
    var guestFree: Double? = nil
    var srvFree: Double? = nil
    var reboot: Bool? = nil
    var updatedAt: String? = nil
    var age: Int? = nil
    var why: String? = nil
    var checkedAt: String? = nil
    var hostAge: Int? = nil
    var hostWhy: String? = nil
    var cleanupDebt: Double? = nil
    var lastCleanup: String? = nil
    var cleanupAge: Int? = nil
    var cleanupWhy: String? = nil

    var updatedDate: Date? { clockDate(updatedAt) }
    var hostKnown: Bool { known && hostWhy == nil }

    static func clock(_ raw: String?, now: Date) -> (date: Date?, age: Int?, why: String?) {
        guard let raw else { return (nil, nil, "time missing") }
        guard let at = clockDate(raw) else { return (nil, nil, "time unreadable") }
        let age = now.timeIntervalSince(at)
        if age < -clockSkew { return (nil, nil, "time in the future") }
        if age > maxAge { return (nil, Int(age), "over 2 hours old") }
        return (at, max(0, Int(age)), nil)
    }
    var line: String {
        guard known, let state else { return "Storage unknown" }
        let free = [guestFree, srvFree].compactMap { $0 }.min().map { String(format: " · %.0f GiB free", $0) } ?? ""
        return "Storage \(state)" + free + (reboot == true ? " · reboot needed" : reboot == nil ? " · reboot unknown" : "")
    }
}

struct PublisherFact: Equatable {
    let state: String
    let detail: String
}


struct QueueLeg: Equatable {
    let repo: String
    let pr: Int?
    let job: String
    let queuedAt: String?
    let jobID: Int?
    var hold: String? = nil
    var excluded = false

    var main: Bool { pr == nil }
    var prID: String? { pr.map { "\(repo)#\($0)" } }
    var id: String { "\(repo)#\(pr.map(String.init) ?? "main")/\(job)" }
}


extension World {
    var openPRs: [PRRow] { repos?.prs ?? [] }
    var prsKnown: Bool { repos?.openInventoryComplete == true }
    var ciGuestsKnown: Bool { fresh(.rig) && guestsKnown }
    var agentsWorkingVM: Int? { agentsKnown ? agents?.filter(\.inTurn).count : nil }
    var agentsWorkingMac: Int? { fresh(.mac) ? mac?.agents?.working : nil }
    var agentsWorking: Int? { agentsWorkingVM.map { $0 + (agentsWorkingMac ?? 0) } }
    var githubBudget: (budget: Cached<GitHubBudget>, source: SourceID)? {
        let readings: [(budget: Cached<GitHubBudget>, source: SourceID)] = [
            fresh(.repos) ? repos?.githubBudget.map { ($0, SourceID.repos) } : nil,
            fresh(.mac) ? mac?.github.map { ($0, SourceID.mac) } : nil].compactMap { $0 }
        func newest(_ list: [(budget: Cached<GitHubBudget>, source: SourceID)]) -> (budget: Cached<GitHubBudget>, source: SourceID)? {
            list.max { ($0.budget.fetched ?? 0) < ($1.budget.fetched ?? 0) }
        }
        return newest(readings.filter { Ingest.current($0.budget, at: now, limit: Ingest.githubBudgetLimit($0.source)) != nil })
            ?? newest(readings.filter { $0.budget.value != nil }) ?? readings.first
    }
    var storageFact: StorageFact {
        guard fresh(.agents) else { return StorageFact(why: "no data") }
        guard let s = rigStatus else { return StorageFact(why: "file missing or unreadable") }
        let local = StorageFact.clock(s.updated_at, now: now)
        guard local.why == nil else { return StorageFact(updatedAt: s.updated_at, age: local.age, why: local.why) }
        let host = StorageFact.clock(s.checked_at, now: now)
        let cleanup = StorageFact.clock(s.last_cleanup, now: now)
        return StorageFact(known: true, state: s.storage_state, guestFree: s.guest_free_gib, srvFree: host.why == nil ? s.srv_vm_free_gib : nil,
                           reboot: host.why == nil ? s.reboot_needed : nil, updatedAt: s.updated_at, age: local.age,
                           checkedAt: s.checked_at, hostAge: host.age, hostWhy: host.why ?? (s.reboot_needed == nil ? "reboot status missing" : nil),
                           cleanupDebt: cleanup.why == nil ? s.cleanup_debt_gib : nil, lastCleanup: s.last_cleanup, cleanupAge: cleanup.age, cleanupWhy: cleanup.why)
    }
    var publisherFact: PublisherFact {
        guard fresh(.rig), let p = host?.merge_publisher, let timer = p.timer else { return PublisherFact(state: "unknown", detail: "no data") }
        if p.load == "not-found" { return PublisherFact(state: "not_installed", detail: "not installed") }
        guard timer == "active" else { return PublisherFact(state: "off", detail: "timer \(timer)") }
        guard let dry = p.dry_run else { return PublisherFact(state: "unknown", detail: "mode unknown") }
        return dry ? PublisherFact(state: "dry_run", detail: "dry run, not publishing") : PublisherFact(state: "publishing", detail: "publishing")
    }
    var retainedPlanNote: String? {
        guard !fresh(.linear) else { return nil }
        let s = status[.linear]
        let age = s?.sampleAt.map { "Linear data \(ago($0, now: now)) old" } ?? "Linear data out of date"
        return s?.error.map { age + " (\($0))" } ?? age
    }
    var awaitingReview: Int? { fresh(.repos) && prsKnown ? openPRs.filter(\.waitingForReview).count : nil }
    var ciRunning: Int? { ciGuestsKnown ? iosJobs.map { $0.count + rigCIGuests.count } : nil }
    var rigCIGuests: [GuestSnap] { guests.filter { $0.isCI } }
    var iosJobs: [IOSJob]? { fresh(.mac) ? mac?.iosCI : nil }
}


enum FlowBoard {
    struct CIState: Equatable {
        let timer: String?
        let progress: Double?
        let rank: Int
        var progressKnown = false
        var hold: String? = nil
        var estimate: Estimate? = nil
    }

    struct Capacity: Equatable {
        let basis: String
        let slots: [Int]
        let admitted: Int?
        let note: String?
    }

    struct Queues: Equatable {
        let backend: [QueueLeg]
        let web: [QueueLeg]
        let ios: [QueueLeg]
        let source: String
        let capacity: Capacity
        let interLaneKnown: Bool
    }

    struct Estimate: Equatable {
        var ends: [Int: Int] = [:]
        var starts: [Int: Int] = [:]
        var admitted: Int? = nil
        var hold: String? = nil
        var unknown: Set<Int> = []
        var pending = false

        mutating func legWithoutEnd() {
            pending = true
            ends = [:]
        }

        mutating func absorb(slots: Int, end: Int?, start: Int?) {
            pending = true
            if let end, !unknown.contains(slots) {
                ends[slots] = max(ends[slots] ?? 0, end)
            } else {
                unknown.insert(slots)
                ends[slots] = nil
            }
            if let start { starts[slots] = min(starts[slots] ?? .max, start) }
        }
        var hasTime: Bool { !ends.isEmpty }
        var startsNow: Bool { !starts.isEmpty && starts.values.allSatisfy { $0 == 0 } }
        var timer: String? {
            if let one = ends[1], let two = ends[2] {
                return one == two ? "~\(clock(one)) left" : "1 slot ~\(clock(one)) · 2 ~\(clock(two))"
            }
            if let only = ends[admitted ?? 0] { return "~\(clock(only)) left" }
            if let one = ends[1] { return "1 slot ~\(clock(one))" }
            return ends[2].map { "2 slots ~\(clock($0))" }
        }
    }

    static let repoOrder = ["backend": 0, "ios": 1, "web": 2]
    static let macLanes: Set<String> = ["ios"]
    static let heartbeatFresh = 300
    static let stuckAfter: Double = 600
    static let guestBusy = "a guest for this PR is running"

    static func bottleneck(_ w: World) -> (stage: PRStage, count: Int)? {
        guard w.fresh(.repos), w.prsKnown, !w.openPRs.isEmpty else { return nil }
        let counts = Dictionary(grouping: w.openPRs, by: PRStage.of).mapValues(\.count)
        return counts.max { ($0.value, -$0.key.rawValue) < ($1.value, -$1.key.rawValue) }.map { ($0.key, $0.value) }
    }

    static func stageCount(_ w: World, _ s: PRStage, _ items: [FlowItem]) -> Int? {
        guard w.fresh(.repos) else { return nil }
        if s == .merged { return Analytics.mergedCount(w.repos) }
        return w.prsKnown ? items.count : nil
    }

    static func countText(_ w: World, exact: Int?, observed: Int) -> String {
        if let exact { return String(exact) }
        return w.fresh(.repos) && observed > 0 ? "\(observed)+" : "–"
    }

    static func stageText(_ w: World, _ s: PRStage, _ items: [FlowItem]) -> String {
        countText(w, exact: stageCount(w, s, items), observed: items.count)
    }

    static func stackObserved(_ w: World, _ s: PRStage, _ repo: String, _ group: [FlowItem]) -> Int {
        guard s == .merged else { return group.count }
        return max(w.repos?.repos?.first { $0.name == repo }?.merged_24h ?? group.count, group.count)
    }

    static func stackCount(_ w: World, _ s: PRStage, _ repo: String, _ group: [FlowItem]) -> Int? {
        guard w.fresh(.repos) else { return nil }
        if s == .merged { return w.repos?.repos?.first { $0.name == repo }?.merged_24h.map { max($0, group.count) } }
        return w.prsKnown ? group.count : nil
    }

    static func stackText(_ w: World, _ s: PRStage, _ repo: String, _ group: [FlowItem]) -> String {
        countText(w, exact: stackCount(w, s, repo, group), observed: stackObserved(w, s, repo, group))
    }

    static func columns(_ w: World) -> [PRStage: [FlowItem]] {
        let now = w.now
        var out: [PRStage: [FlowItem]] = [:]
        let ci = ciState(w)
        let all = Array(w.timelines.values)
        let worked = Set((w.agentsKnown ? w.agents ?? [] : []).filter(\.inTurn).compactMap(\.prKey))
        let thresholds = Dictionary(uniqueKeysWithValues: FlowSample.waiting.compactMap { s in Analytics.agingThreshold(all, s).map { (s, $0) } })
        for p in w.openPRs {
            let stage = PRStage.of(p)
            let mark: Health? = p.ci == "failing" ? .bad : p.merge_state == "DIRTY" ? .warn : nil
            let s = ci[p.id]
            var timer = s?.timer
            if timer == nil, stage == .work || stage == .review {
                let st = PRStatus.of(p)
                let since = w.timelines[p.id].flatMap { $0.current == st ? $0.inStage(now: now.timeIntervalSince1970) : nil }
                    ?? (st == .fixing ? p.failingDate : p.readyDate).map { now.timeIntervalSince($0) }
                if let typ = Analytics.typical(w.timelines, st, repo: p.repo), let since {
                    let left = Int(typ - since)
                    timer = left > 0 ? "~\(clock(left)) left" : "\(clock(-left)) over"
                }
            }
            out[stage, default: []].append(FlowItem(id: p.id, repo: p.repo, number: p.number, title: p.title, mark: mark,
                                                    approved: p.approved, timer: timer, progress: s?.progress,
                                                    rank: s?.rank ?? (worked.contains(p.id) ? 0 : 1),
                                                    order: -(w.timelines[p.id]?.inStage(now: now.timeIntervalSince1970)
                                                             ?? isoDate(p.created).map { now.timeIntervalSince($0) } ?? 0),
                                                    opened: isoDate(p.created),
                                                    aging: w.timelines[p.id].map { t in
                                                        t.current.flatMap { thresholds[$0] }.map { (t.inStage(now: now.timeIntervalSince1970) ?? 0) > $0 } ?? false
                                                    } ?? false,
                                                    conflicts: p.merge_state == "DIRTY", progressKnown: s?.progressKnown ?? false))
        }
        let day = now.addingTimeInterval(-86400)
        out[.merged] = (w.repos?.recent ?? []).filter { ($0.mergedDate ?? .distantPast) > day }
            .map { FlowItem(id: $0.id, repo: $0.repo, number: $0.number, title: $0.title, mark: nil, approved: false,
                            timer: $0.mergedDate.map { duration(Int(now.timeIntervalSince($0))) + " ago" }, progress: nil,
                            rank: -Int($0.mergedDate?.timeIntervalSince1970 ?? 0), opened: $0.createdDate, merged: $0.mergedDate) }
        for k in out.keys {
            out[k]?.sort { ($0.rank, repoOrder[$0.repo] ?? 9, $0.order, -$0.number) < ($1.rank, repoOrder[$1.repo] ?? 9, $1.order, -$1.number) }
        }
        return out
    }

    static func runningJobs(_ w: World) -> [String: LaneJob] {
        guard w.fresh(.repos) else { return [:] }
        let lanes = w.fresh(.rig) ? w.rigCI?.lanes : nil
        let rigGuests = w.fresh(.rig) ? w.rigCIGuests : []
        var running: [String: LaneJob] = [:]
        let rig = LaneTiming.backend(rigGuests, lanes?.backend, age: w.elapsed(.rig))
            + LaneTiming.web(rigGuests, lanes?.web, age: w.elapsed(.rig))
        for j in rig {
            guard let g = rigGuests.first(where: { $0.name == j.id }), let repo = g.repo else { continue }
            if let byRun = g.run.flatMap({ r in w.openPRs.first { $0.repo == repo && ($0.runs ?? []).contains(r) } }) {
                running[byRun.id] = j
            } else if let n = g.pr, LaneTiming.sameHead(guest: g.name, prHead: w.openPRs.first { $0.repo == repo && $0.number == n }?.head) {
                running["\(repo)#\(n)"] = j
            }
        }
        let ios = LaneTiming.ios(w.iosJobs ?? [], w.mac?.iosLane, age: w.elapsed(.mac))
        for (i, job) in (w.iosJobs ?? []).enumerated() {
            if let n = job.pr { running["\(job.repoKey)#\(n)"] = ios[i] }
        }
        return running
    }

    static func linked(_ jobs: [LaneJob], _ w: World) -> [LaneJob] {
        let running = runningJobs(w), state = ciState(w)
        return jobs.map { j in
            var j = j
            let link = running.first { $0.value.id == j.id }
            let pr = link.flatMap { l in w.openPRs.first { $0.id == l.key } }
            j.pr = pr?.number
            j.completion = link.flatMap { state[$0.key]?.timer }
            if let pr { j.name = pr.title } else if j.short == "main" { j.name = "main after a merge" }
            return j
        }
    }

    static func heartbeat(_ w: World, _ beat: CIHeartbeat?) -> CIHeartbeat? {
        guard w.fresh(.rig), let beat, let age = beat.age_s, age + w.elapsed(.rig) <= heartbeatFresh else { return nil }
        return beat
    }

    static func waitNote(_ beat: CIHeartbeat?) -> String? {
        guard let beat else { return nil }
        if beat.phase == "running" || beat.phase == "idle" { return beat.detail?.contains("refused") == true ? beat.detail : nil }
        return beat.detail ?? beat.phase
    }

    static func rigCapacity(_ w: World) -> Capacity {
        let beat = heartbeat(w, w.rigCI?.watch)
        let wait = waitNote(beat)
        guard w.ciGuestsKnown else { return Capacity(basis: "unknown", slots: [], admitted: nil, note: wait) }
        let running = w.rigCIGuests.count
        if running >= 2 { return Capacity(basis: "admitted", slots: [2], admitted: 2, note: wait) }
        let refused = beat.map { $0.phase == "waiting for admission" || $0.detail?.contains("admission refused") == true } ?? false
        let stopped = beat?.phase == "refused" || beat?.phase == "paused" || beat?.phase == "error"
        if stopped || (refused && running == 0) { return Capacity(basis: "unknown", slots: [], admitted: nil, note: wait) }
        if refused { return Capacity(basis: "admitted", slots: [1], admitted: 1, note: wait) }
        return Capacity(basis: "scenarios", slots: [1, 2], admitted: nil, note: wait)
    }

    static func legLength(_ job: String, _ w: World) -> Int? {
        let lanes = w.fresh(.rig) ? w.rigCI?.lanes : nil
        let name = job.lowercased()
        return name.contains("integration") ? lanes?.backend?.integration_s : name.contains("fast") || name == "queued" ? lanes?.backend?.fast_s : nil
    }

    static func githubLegs(_ w: World, repo: String) -> [QueueLeg] {
        guard w.fresh(.repos) else { return [] }
        let mains = (w.repos?.repos ?? []).first { $0.name == repo }?.main_queue ?? []
        let prs = w.openPRs.filter { $0.repo == repo }
        func queued(_ p: PRRow) -> [QueuedJob] { p.queued_jobs ?? (p.queued_at == nil ? [] : [QueuedJob(job: "queued", queued_at: p.queued_at, id: nil)]) }
        return mains.map { QueueLeg(repo: repo, pr: nil, job: $0.job, queuedAt: $0.queued_at, jobID: $0.id) }
            + prs.flatMap { p in queued(p).map { QueueLeg(repo: repo, pr: p.number, job: $0.job, queuedAt: $0.queued_at, jobID: $0.id) } }
    }

    static func sortedEligible(_ legs: [QueueLeg]) -> [QueueLeg] {
        legs.sorted { a, b in
            let x = (a.main ? 0 : 1, a.queuedAt ?? "9999", a.jobID ?? .max, a.id), y = (b.main ? 0 : 1, b.queuedAt ?? "9999", b.jobID ?? .max, b.id)
            return x < y
        }
    }

    static func backendQueue(_ w: World) -> (legs: [QueueLeg], source: String) {
        if let rows = heartbeat(w, w.rigCI?.watch)?.queue {
            let legs = rows.filter { $0.repo == SiteConfig.shared.repoName("backend") }.map { r -> QueueLeg in
                let pr = (r.pr ?? 0) > 0 ? r.pr : nil
                let hold = r.state == "eligible" ? nil : r.why ?? r.state
                return QueueLeg(repo: "backend", pr: pr, job: r.job, queuedAt: r.queued_at, jobID: nil, hold: hold,
                                excluded: hold != nil && hold?.hasPrefix(guestBusy) != true)
            }
            return (legs, "ci-watch")
        }
        guard w.ciGuestsKnown else { return (sortedEligible(githubLegs(w, repo: "backend")), "github") }
        let busy = Set(w.rigCIGuests.filter { $0.repo == "backend" }.compactMap(\.pr))
        let legs = githubLegs(w, repo: "backend").filter { let n = $0.job.lowercased(); return n.contains("fast") || n.contains("integration") || n == "queued" }.map { l -> QueueLeg in
            var l = l
            if let n = l.pr, busy.contains(n) { l.hold = guestBusy }
            return l
        }
        return (sortedEligible(legs), "github")
    }

    static func webQueue(_ w: World) -> [QueueLeg] {
        let legs = githubLegs(w, repo: "web")
        let mains = sortedEligible(legs.filter(\.main))
        var firstOfPR: [Int: QueueLeg] = [:]
        for l in sortedEligible(legs.filter { !$0.main }) { if let n = l.pr, firstOfPR[n] == nil { firstOfPR[n] = l } }
        return mains + sortedEligible(Array(firstOfPR.values))
    }

    static func queues(_ w: World) -> Queues {
        let backend = backendQueue(w)
        var mac = (w.fresh(.repos) ? githubLegs(w, repo: "ios") : [])
        let prs = Dictionary(w.openPRs.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        for i in mac.indices where !mac[i].main {
            guard let p = mac[i].prID.flatMap({ prs[$0] }), p.queue_tier == 1, p.review != "approved" else { continue }
            mac[i].hold = "held until reviewed"
            mac[i].excluded = true
        }
        func macKey(_ l: QueueLeg) -> (Int, Int, Int, String, Int, String) {
            let p = l.prID.flatMap { prs[$0] }
            return (l.main ? 0 : 1, p?.queue_tier ?? 0, l.excluded ? 1 : 0, l.queuedAt ?? "9999", l.jobID ?? .max, l.id)
        }
        let web = webQueue(w)
        return Queues(backend: backend.legs, web: web, ios: mac.sorted { macKey($0) < macKey($1) }, source: backend.source, capacity: rigCapacity(w),
                      interLaneKnown: web.allSatisfy(\.main) || backend.legs.allSatisfy { $0.main || $0.excluded })
    }

    static func estimates(_ w: World) -> [String: Estimate] {
        let q = queues(w)
        var out: [String: Estimate] = [:]
        for l in q.backend + q.web + q.ios { if let id = l.prID { out[id, default: Estimate()].pending = true } }
        func group(_ l: QueueLeg) -> String { l.repo == "web" ? "web" : l.prID ?? "main:" + l.repo }
        let lanes = w.fresh(.rig) ? w.rigCI?.lanes : nil
        let rigJobs = w.ciGuestsKnown ? LaneTiming.backend(w.rigCIGuests, lanes?.backend, age: w.elapsed(.rig)) + LaneTiming.web(w.rigCIGuests, lanes?.web, age: w.elapsed(.rig)) : []
        let backend = q.backend.filter { !$0.excluded }
        let mains = sortedEligible(backend.filter(\.main) + q.web.filter(\.main))
        let queuedIntegration = Set(q.backend.filter { $0.job.lowercased().contains("integration") }.compactMap(\.prID))
        let unqueued = runningJobs(w).filter { $0.value.repo == "backend" && $0.value.leg == "fast" && !queuedIntegration.contains($0.key) }
            .values.compactMap(\.pr).sorted().map { QueueLeg(repo: "backend", pr: $0, job: "integration", queuedAt: nil, jobID: nil) }
        for l in unqueued { if let id = l.prID { out[id, default: Estimate()].pending = true } }
        let backendPRs = backend.filter { !$0.main } + unqueued, webPRs = q.web.filter { !$0.main }
        let backendFirst = mains + backendPRs + webPRs
        let orders = q.interLaneKnown ? [backendFirst] : [backendFirst, mains + webPRs + backendPRs]
        var busy: [String: Int?] = [:]
        for j in rigJobs {
            if j.repo == "web" { busy["web"] = .some(j.remaining) } else { busy[j.pr.map { "backend#\($0)" } ?? "main:backend"] = .some(j.remaining) }
        }
        func timed(_ order: [QueueLeg], slots: Int) -> [String: (start: Int?, end: Int?)] {
            let jobs = order.map { l in (group: group(l), length: l.repo == "web" ? lanes?.web?.run_s : legLength(l.job, w)) }
            let r = LaneTiming.schedule(running: rigJobs.map(\.remaining), slots: slots, jobs: jobs, busy: busy)
            return Dictionary(zip(order.map(\.id), r), uniquingKeysWith: { a, _ in a })
        }
        var orderDecides = Set<String>()
        for slots in q.capacity.slots {
            let timings = orders.map { timed($0, slots: slots) }
            for l in backendFirst {
                guard let id = l.prID, let first = timings[0][l.id] else { continue }
                let agreed = timings.allSatisfy { $0[l.id]?.start == first.start && $0[l.id]?.end == first.end }
                if !agreed { orderDecides.insert(id) }
                var e = out[id] ?? Estimate()
                e.absorb(slots: slots, end: agreed ? first.end : nil, start: agreed ? first.start : nil)
                if q.capacity.basis == "admitted" { e.admitted = q.capacity.admitted }
                out[id] = e
            }
        }
        for l in q.backend where l.hold != nil { if let id = l.prID, out[id]?.hasTime != true { out[id, default: Estimate()].hold = l.hold } }
        if q.capacity.basis == "unknown", let note = q.capacity.note {
            for l in q.backend + q.web { if let id = l.prID, out[id]?.hold == nil { out[id, default: Estimate()].hold = note } }
        }
        for l in backendPRs + webPRs {
            guard let id = l.prID, orderDecides.contains(id), out[id]?.hasTime != true, out[id]?.hold == nil else { continue }
            out[id]?.hold = l.repo == "web" ? "order with backend unknown" : "order with web unknown"
        }
        if w.iosJobs != nil {
            let lane = w.mac?.iosLane
            let running = LaneTiming.ios(w.iosJobs ?? [], lane, age: w.elapsed(.mac))
            let macOrder = q.ios.filter { !$0.excluded }
            var macBusy: [String: Int?] = [:]
            for j in running { if let n = j.pr, let r = j.repo { macBusy["\(r)#\(n)"] = .some(j.remaining) } }
            let timed = macOrder.map { l in (group: group(l), length: l.job == "queued" ? lane?.run_s.map { $0 * 2 } : lane?.run_s) }
            let r = LaneTiming.schedule(running: running.map(\.remaining), slots: 1, jobs: timed, busy: macBusy)
            for (i, l) in macOrder.enumerated() {
                guard let id = l.prID else { continue }
                var e = out[id] ?? Estimate()
                e.absorb(slots: 1, end: r[i].end, start: r[i].start)
                e.admitted = 1
                out[id] = e
            }
            for l in q.ios where l.excluded { if let id = l.prID, out[id]?.hasTime != true { out[id, default: Estimate()].hold = l.hold } }
        }
        for l in q.backend + q.ios where l.excluded { if let id = l.prID { out[id, default: Estimate()].legWithoutEnd() } }
        return out
    }

    static func ciState(_ w: World) -> [String: CIState] {
        let now = w.now
        let running = runningJobs(w)
        let estimated = estimates(w)
        let q = queues(w)
        var queueRank: [String: Int] = [:]
        for legs in [q.backend, q.web, q.ios] {
            for l in legs.filter({ !$0.excluded }) + legs.filter(\.excluded) {
                if let id = l.prID, queueRank[id] == nil { queueRank[id] = queueRank.count }
            }
        }
        var out: [String: CIState] = [:]
        func place(_ p: PRRow) -> (Int, Int, Double, Int) {
            let legs = p.queued_jobs ?? []
            let at = legs.compactMap { isoDate($0.queued_at)?.timeIntervalSince1970 }.min()
                ?? isoDate(p.queued_at)?.timeIntervalSince1970
                ?? w.timelines[p.id].flatMap { t in t.inStage(now: now.timeIntervalSince1970).map { now.timeIntervalSince1970 - $0 } }
                ?? isoDate(p.updated)?.timeIntervalSince1970 ?? .infinity
            let id = legs.compactMap(\.id).min() ?? .max
            guard macLanes.contains(p.repo) else { return (0, 0, at, id) }
            return (p.queue_tier ?? 0, p.queue_tier == 1 && p.review != "approved" ? 1 : 0, at, id)
        }
        for (n, p) in w.openPRs.sorted(by: { place($0) < place($1) }).enumerated() {
            if let j = running[p.id] {
                let more = estimated[p.id].flatMap { $0.pending ? $0 : nil }
                let own = j.remaining.map { _ in j.timeText }
                out[p.id] = CIState(timer: more == nil ? own : more?.timer, progress: j.progress ?? 0.05, rank: 0,
                                    progressKnown: j.progress != nil, estimate: more)
            } else if PRStatus.of(p) == .waitingCI || PRStatus.of(p) == .inCI {
                let e = estimated[p.id]
                var timer = e?.timer ?? e?.hold
                let waited = w.timelines[p.id].flatMap { $0.inStage(now: now.timeIntervalSince1970) }
                if e?.startsNow == true, let waited, waited > stuckAfter { timer = "waiting \(clock(Int(waited)))" }
                out[p.id] = CIState(timer: timer, progress: nil, rank: 1 + (queueRank[p.id] ?? queueRank.count + n), hold: e?.hold, estimate: e)
            }
        }
        return out
    }
}
