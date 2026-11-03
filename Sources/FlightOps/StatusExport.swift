import Foundation

struct StatusDocument: Encodable, Equatable {
    var schema = "flightops-status/1"
    let generated_at: String
    let sources: [Source]
    let attention: Attention
    let counts: Counts
    let rig: Rig
    let merge_publisher: Publisher
    var vm_feed: VMFeed? = nil
    let bottleneck: Bottleneck?
    let flow: [Stage]
    let lanes: [Lane]
    let prs: [PR]

    struct Text: Encodable, Equatable {
        let text: String
        var trust = "untrusted"
    }

    struct Source: Encodable, Equatable {
        let id: String
        let label: String
        let state: String
        let age_s: Int?
        let error: String?
        var via: String? = nil
    }

    struct VMFeed: Encodable, Equatable {
        let state: String
        let reason: String?
        let published_at: String?
        let age_s: Int?
    }

    struct Need: Encodable, Equatable {
        let id: String
        let kind: String
        let tier: String
        let health: String
        let title: Text
        let reason: Text
    }

    struct Problem: Encodable, Equatable {
        let id: String
        let label: String
        let detail: String
        let health: String
        let source: String
    }

    struct Attention: Encodable, Equatable {
        let state: String
        let complete: Bool
        let gaps: [String]
        let waiting_on_owner: Int
        let needs: [Need]
        let problems: [Problem]
    }

    struct Counts: Encodable, Equatable {
        let open_prs: Int?
        let merged_24h: Int?
        let prs_complete: Bool?
        let agents_working: Int?
        let agents_working_vm: Int?
        let agents_working_mac: Int?
        let ci_jobs_running: Int?
        let awaiting_review: Int?
    }

    struct Rig: Encodable, Equatable {
        let state: String
        let reason: String?
        let storage_state: String?
        let guest_free_gib: Double?
        let srv_vm_free_gib: Double?
        let reboot_needed: Bool?
        let updated_at: String?
        let age_s: Int?
        let checked_at: String?
        let host_age_s: Int?
        let host_reason: String?
        let cleanup_debt_gib: Double?
        let last_cleanup: String?
        let cleanup_age_s: Int?
        let cleanup_reason: String?
    }

    struct Publisher: Encodable, Equatable {
        let state: String
        let detail: String
        let required: Bool
    }

    struct Bottleneck: Encodable, Equatable {
        let stage: String
        let label: String
        let open_prs: Int
    }

    struct Item: Encodable, Equatable {
        let id: String
        let repo: String
        let number: Int
        let timer: String?
        let hold: String?
        let estimate: Estimate?
        let progress: Double?
        let failing: Bool
        let conflicts: Bool
        let approved: Bool
        let overdue: Bool
    }

    struct Stack: Encodable, Equatable {
        let repo: String
        let count: Int?
        let observed: Int
        let prs: [Item]
    }

    struct Stage: Encodable, Equatable {
        let stage: String
        let label: String
        let count: Int?
        let stacks: [Stack]
    }

    struct Job: Encodable, Equatable {
        let id: String
        let pr: String?
        let label: String
        let elapsed_s: Int?
        let typical_s: Int?
        let remaining_s: Int?
        let overdue: Bool
        let timer: String?
    }

    struct Estimate: Encodable, Equatable {
        let basis: String
        let admitted_s: Int?
        let one_slot_s: Int?
        let two_slots_s: Int?
    }

    struct QueueRow: Encodable, Equatable {
        let job: String
        let pr: String?
        let main: Bool
        let queued_at: String?
        let state: String
        let hold: String?
    }

    struct Capacity: Encodable, Equatable {
        let basis: String
        let admitted_slots: Int?
        let shared_with: String?
        let order: String
        let inter_lane_order: String?
        let holds_known: Bool
    }

    struct Lane: Encodable, Equatable {
        let lane: String
        let host: String
        let health: String?
        let detail: String?
        let idle_s: Int?
        let jobs: [Job]
        let wait: String?
        let capacity: Capacity
        let queue: [QueueRow]
    }

    struct PR: Encodable, Equatable {
        let id: String
        let repo: String
        let number: Int
        let status: String
        let stage: String
        let ci: String
        let failed_checks: Int?
        let review: String
        let approved: Bool
        let merge_ready: Bool?
        let created: String
        let updated: String
        let in_stage_s: Int?
        let timer: String?
        let hold: String?
        let estimate: Estimate?
        let url: String
        let title: Text
    }
}

enum StatusExport {
    static let stageKeys = ["work", "ci", "review", "merged"]
    static let statusKeys = ["draft", "fixing", "waiting_ci", "in_ci", "review", "approved", "merged"]
    static let healthKeys: [Health: String] = [.ok: "ok", .unknown: "unknown", .warn: "warn", .bad: "bad"]
    static let kindKeys: [NeedItem.Kind: String] = [.problem: "problem", .agent: "agent", .merge: "merge", .warning: "warning", .owner: "owner", .failing: "failing"]
    static let tierKeys: [NeedItem.Tier: String] = [.now: "now", .today: "today", .later: "later"]

    static func iso(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }

    static func estimate(_ e: FlowBoard.Estimate?) -> StatusDocument.Estimate? {
        guard let e, e.hasTime else { return nil }
        if let a = e.admitted { return .init(basis: "admitted", admitted_s: e.ends[a], one_slot_s: nil, two_slots_s: nil) }
        return .init(basis: "scenarios", admitted_s: nil, one_slot_s: e.ends[1], two_slots_s: e.ends[2])
    }

    static func document(_ w: World, checks: [Check], needs: Needs) -> StatusDocument {
        let now = w.now
        let sources = SourceID.allCases.map { s -> StatusDocument.Source in
            let st = w.status[s] ?? SourceStatus()
            let state: String
            switch w.freshness(s) {
            case .fresh: state = "fresh"
            case .stale: state = "stale"
            case .missing: state = "missing"
            }
            return .init(id: s.rawValue, label: s.label, state: state,
                         age_s: st.sampleAt.map { max(0, Int(now.timeIntervalSince($0))) }, error: st.error, via: via(w, s))
        }
        let problems = checks.filter { $0.health != .ok }.sorted { ($1.health, $0.id) < ($0.health, $1.id) }
            .map { StatusDocument.Problem(id: $0.id, label: $0.label, detail: $0.detail, health: healthKeys[$0.health] ?? "unknown", source: $0.source.rawValue) }
        let needItems = needs.items.map {
            StatusDocument.Need(id: $0.id, kind: kindKeys[$0.kind] ?? "problem", tier: tierKeys[$0.tier] ?? "later",
                                health: healthKeys[$0.health] ?? "unknown", title: .init(text: $0.title), reason: .init(text: $0.reason))
        }
        let verdict = Rules.verdict(checks: checks, needs: needs)
        let attention = StatusDocument.Attention(state: verdict.state, complete: needs.complete, gaps: needs.gaps,
                                                 waiting_on_owner: verdict.waiting.count, needs: needItems, problems: problems)
        let reposFresh = w.fresh(.repos)
        let prsKnown = reposFresh && w.prsKnown
        let counts = StatusDocument.Counts(open_prs: prsKnown ? w.openPRs.count : nil,
                                           merged_24h: reposFresh ? Analytics.mergedCount(w.repos) : nil,
                                           prs_complete: reposFresh && w.repos?.prs != nil ? prsKnown : nil,
                                           agents_working: w.agentsWorking, agents_working_vm: w.agentsWorkingVM, agents_working_mac: w.agentsWorkingMac, ci_jobs_running: w.ciRunning,
                                           awaiting_review: w.awaitingReview)
        let bottleneck = FlowBoard.bottleneck(w).map { StatusDocument.Bottleneck(stage: stageKeys[$0.stage.rawValue], label: $0.stage.label, open_prs: $0.count) }
        let columns = FlowBoard.columns(w)
        let ci = FlowBoard.ciState(w)
        let flow = PRStage.allCases.map { s -> StatusDocument.Stage in
            let items = columns[s] ?? []
            let count = FlowBoard.stageCount(w, s, items)
            let stacks = ["backend", "ios", "web"].compactMap { r -> StatusDocument.Stack? in
                let group = items.filter { $0.repo == r }
                let observed = FlowBoard.stackObserved(w, s, r, group)
                guard !group.isEmpty || (s == .merged && observed > 0) else { return nil }
                return .init(repo: r, count: FlowBoard.stackCount(w, s, r, group), observed: observed, prs: group.map {
                    .init(id: $0.id, repo: $0.repo, number: $0.number, timer: $0.timer, hold: ci[$0.id]?.hold, estimate: estimate(ci[$0.id]?.estimate), progress: $0.progressKnown ? $0.progress : nil,
                          failing: $0.mark == .bad, conflicts: $0.conflicts, approved: $0.approved, overdue: $0.aging)
                })
            }
            return .init(stage: stageKeys[s.rawValue], label: s.label, count: count, stacks: stacks)
        }
        let lanes = lanesOf(w, checks: checks)
        let prs = w.openPRs.sorted { (FlowBoard.repoOrder[$0.repo] ?? 9, $0.number) < (FlowBoard.repoOrder[$1.repo] ?? 9, $1.number) }.map { p -> StatusDocument.PR in
            let st = PRStatus.of(p)
            let inStage = w.timelines[p.id].flatMap { $0.current == st ? $0.inStage(now: now.timeIntervalSince1970) : nil }.map(Int.init)
            let timer = columns.values.lazy.flatMap { $0 }.first { $0.id == p.id }?.timer ?? ci[p.id]?.timer
            return .init(id: p.id, repo: p.repo, number: p.number, status: statusKeys[st.rawValue], stage: stageKeys[PRStage.of(p).rawValue],
                         ci: p.ci, failed_checks: p.failed_checks, review: p.review, approved: p.approved, merge_ready: p.merge_ready,
                         created: p.created, updated: p.updated, in_stage_s: inStage, timer: timer, hold: ci[p.id]?.hold, estimate: estimate(ci[p.id]?.estimate), url: p.url.absoluteString,
                         title: .init(text: p.title))
        }
        let f = w.storageFact
        let rig = StatusDocument.Rig(state: f.known ? f.state ?? "unknown" : "unknown", reason: f.why, storage_state: f.state, guest_free_gib: f.guestFree,
                                       srv_vm_free_gib: f.srvFree, reboot_needed: f.reboot, updated_at: f.updatedAt, age_s: f.age,
                                       checked_at: f.checkedAt, host_age_s: f.hostAge, host_reason: f.hostWhy, cleanup_debt_gib: f.cleanupDebt,
                                       last_cleanup: f.lastCleanup, cleanup_age_s: f.cleanupAge, cleanup_reason: f.cleanupWhy)
        let publisher = StatusDocument.Publisher(state: w.publisherFact.state, detail: w.publisherFact.detail, required: false)
        let feed = w.feed.map { StatusDocument.VMFeed(state: $0.phase(now: now), reason: $0.reason, published_at: $0.publishedAt.map(iso),
                                                      age_s: $0.publishedAt.map { max(0, Int(now.timeIntervalSince($0))) }) }
        return StatusDocument(generated_at: iso(now), sources: sources, attention: attention, counts: counts, rig: rig, merge_publisher: publisher, vm_feed: feed,
                              bottleneck: bottleneck, flow: flow, lanes: lanes, prs: prs)
    }

    static func via(_ w: World, _ s: SourceID) -> String? {
        guard w.feed != nil, s == .repos || s == .agents else { return nil }
        return w.status[s]?.origin == PublishedStatus.origin ? PublishedStatus.origin : "fallback"
    }

    static func lanesOf(_ w: World, checks: [Check]) -> [StatusDocument.Lane] {
        let now = w.now.timeIntervalSince1970
        let lanes = w.fresh(.rig) ? w.rigCI?.lanes : nil
        let guests = w.fresh(.rig) ? w.rigCIGuests : []
        let rigIdleKnown = w.ciGuestsKnown
        func check(_ id: String) -> Check? { checks.first { $0.id == id } }
        let running = FlowBoard.runningJobs(w), ci = FlowBoard.ciState(w)
        func jobs(_ list: [LaneJob], repo: String) -> [StatusDocument.Job] {
            list.map { j in
                let pr = running.filter { $0.value.id == j.id }.keys.sorted().first
                let number = pr.flatMap { $0.split(separator: "#").last.map(String.init) }
                return .init(id: j.id, pr: pr, label: number.map { "#" + $0 } ?? j.short ?? j.title,
                             elapsed_s: j.elapsed, typical_s: j.typical, remaining_s: j.remaining, overdue: j.overdue, timer: pr.flatMap { ci[$0]?.timer })
            }
        }
        let q = FlowBoard.queues(w)
        func rows(_ legs: [QueueLeg], holdsKnown: Bool) -> [StatusDocument.QueueRow] {
            legs.map { .init(job: $0.job, pr: $0.prID, main: $0.main, queued_at: $0.queuedAt,
                             state: $0.excluded ? "excluded" : $0.hold != nil ? "held" : holdsKnown ? "eligible" : "candidate", hold: $0.hold) }
        }
        func shared(holdsKnown: Bool) -> StatusDocument.Capacity {
            .init(basis: q.capacity.basis, admitted_slots: q.capacity.admitted, shared_with: "backend and web",
                  order: "candidates, not admission order", inter_lane_order: q.interLaneKnown ? nil : "unknown", holds_known: holdsKnown)
        }
        let macLane = StatusDocument.Capacity(basis: w.iosJobs == nil ? "unknown" : "admitted", admitted_slots: w.iosJobs == nil ? nil : 1, shared_with: nil,
                                              order: "candidates, not admission order", inter_lane_order: nil, holds_known: false)
        func lane(_ name: String, _ host: String, _ id: String, _ list: [StatusDocument.Job], _ lastDone: Int?, idleKnown: Bool = true,
                  wait: String?, capacity: StatusDocument.Capacity, queue: [QueueLeg]) -> StatusDocument.Lane {
            let c = check(id)
            return .init(lane: name, host: host, health: c.map { healthKeys[$0.health] ?? "unknown" }, detail: c?.detail,
                         idle_s: list.isEmpty && idleKnown ? LaneTiming.idle(lastDone: lastDone, now: now) : nil, jobs: list,
                         wait: wait, capacity: capacity, queue: rows(queue, holdsKnown: capacity.holds_known))
        }
        let ios = LaneTiming.ios(w.iosJobs ?? [], w.mac?.iosLane, age: w.elapsed(.mac))
        return [
            lane("backend", "rig", "ci-backend", jobs(LaneTiming.backend(guests, lanes?.backend, age: w.elapsed(.rig)), repo: "backend"), lanes?.backend?.last_done, idleKnown: rigIdleKnown,
                 wait: q.capacity.note, capacity: shared(holdsKnown: q.source == "ci-watch"), queue: q.backend),
            lane("web", "rig", "ci-web", jobs(LaneTiming.web(guests, lanes?.web, age: w.elapsed(.rig)), repo: "web"), lanes?.web?.last_done, idleKnown: rigIdleKnown,
                 wait: FlowBoard.waitNote(FlowBoard.heartbeat(w, w.rigCI?.web)), capacity: shared(holdsKnown: false), queue: q.web),
            lane("ios", "mac", "ci-ios", ios.map { j in jobs([j], repo: j.repo ?? "ios")[0] }, w.mac?.iosLane?.last_done, idleKnown: w.iosJobs != nil && Rules.iosWork(w) == nil,
                 wait: nil, capacity: macLane, queue: q.ios),
        ]
    }

    static func encode(_ d: StatusDocument) throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys]
        return try e.encode(d)
    }
}
