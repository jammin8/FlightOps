import Foundation
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

struct Feed: Equatable {
    var state: String
    var reason: String? = nil
    var publishedAt: Date? = nil
    var owned: Set<SourceID> = []

    func live(now: Date) -> Bool {
        guard let publishedAt else { return false }
        let age = now.timeIntervalSince(publishedAt)
        return age <= PublishedStatus.fileLife && age >= -clockSkew
    }
    func owns(_ s: SourceID, now: Date) -> Bool { owned.contains(s) && live(now: now) }
    func phase(now: Date) -> String { state == "fresh" && !live(now: now) ? "stale" : state }
}

struct LiveModels {
    static let schema = "flightops-models/1"
    static let limit: TimeInterval = 3600
    static let linkedRole = "linked"
    static let unallocated = "unallocated"
    static let linkEstimate = "orch-link-estimate"
    static let allocations: Set<String> = ["shared", "orch-link", unallocated]
    static let malformed = "unreadable collector output"
    static let buckets = ["input", "cache_read", "cache_write", "output", "reasoning"]
    static let refKeys: Set<String> = ["run", "role", "pr_via", "role_via", "head", "family_hint"]

    var records: [String: PRModels]
    var at: Date
    var attributed: Int
    var github: GitHubCoverage?

    struct GitHubCoverage {
        var at: Date?
        var state: String
        var prs: Int?
        var unknown: Int?

        func line(now: Double) -> String {
            let age = at.map { now - $0.timeIntervalSince1970 }
            guard state == "fresh", let age else { return "GitHub coverage unknown (" + state + ")" }
            guard age >= -clockSkew else { return "GitHub coverage unknown (future)" }
            guard age <= SourceID.repos.staleAfter else { return "GitHub coverage unknown (stale)" }
            guard let prs, let unknown else { return "GitHub coverage unknown" }
            return "\(unknown) of \(prs) GitHub PRs unknown · GitHub age " + duration(Int(max(0, age)))
        }
    }

    func current(now: Date) -> Bool {
        let age = now.timeIntervalSince(at)
        return age <= Self.limit && age >= -clockSkew
    }

    func statusLine(now: Double) -> String {
        var parts = ["Models: live from VM", "\(attributed) PRs on Orchestrator records"]
        parts.append(github?.line(now: now) ?? "GitHub coverage unknown")
        parts += ["builders unknown", "settings as requested", "report age " + duration(Int(max(0, now - at.timeIntervalSince1970)))]
        return parts.joined(separator: " · ")
    }
}

enum PublishedStatus {
    static let schema = "flightops-snapshot/1"
    static let role = "hud-read-only"
    static let fileLife: TimeInterval = 600
    static let sizeLimit = 32 * 1024 * 1024
    static let modelsLimit = 16 * 1024 * 1024
    static let modelsTooLarge = "Models report too large to publish"
    static let withheldTitle = "title withheld"
    static let origin = "snapshot"

    struct Spec {
        let source: String
        let clock: String
        let limit: TimeInterval
    }

    static let specs: [String: Spec] = [
        "github": Spec(source: "repos", clock: "fetched", limit: SourceID.repos.staleAfter),
        "vm": Spec(source: "vm", clock: "collected", limit: SourceID.vmLocalStaleAfter),
        "orch": Spec(source: "vm", clock: "collected", limit: SourceID.vmLocalStaleAfter),
        "orch_token": Spec(source: "vm", clock: "collected", limit: SourceID.vmLocalStaleAfter),
        "agents": Spec(source: "vm", clock: "collected", limit: SourceID.vmLocalStaleAfter),
        "rig_local": Spec(source: "rig-status", clock: "updated_at", limit: StorageFact.maxAge),
        "rig_host": Spec(source: "rig-status", clock: "checked_at", limit: StorageFact.maxAge),
        "rig_cleanup": Spec(source: "rig-status", clock: "last_cleanup", limit: StorageFact.maxAge),
        models: Spec(source: "orch+agent-tokens", clock: "generated", limit: LiveModels.limit),
        ledger: Spec(source: "evidence-ledger", clock: "generated", limit: LiveModels.limit),
        best: Spec(source: "orch+github+models", clock: "generated", limit: LiveBest.limit),
        failures: Spec(source: "evidence-ledger+github", clock: "generated", limit: RepeatedFailures.limit),
    ]
    static let models = "models"
    static let ledger = "ledger"
    static let ledgerSchema = "flightops-ledger-pass/1"
    static let best = "best"
    static let failures = "failures"
    static let delivery = "delivery"
    static let notFromVM: [[String: String]] = [
        ["field": "rig.cpu", "why": "needs CI host access"],
        ["field": "rig.guests", "why": "needs CI host access"],
        ["field": "ci.minute_health", "why": "needs CI host access"],
        ["field": "merge_publisher", "why": "needs CI host access"],
        ["field": "mac", "why": "Mac-only"],
        ["field": "linear", "why": "not collected on the VM"],
    ]

    struct Input {
        var repos: Data?
        var reposError: String?
        var vm: Data?
        var vmError: String?
        var models: Data? = nil
        var modelsError: String? = nil
        var ledger: Data? = nil
        var ledgerError: String? = nil
        var best: Data? = nil
        var bestError: String? = nil
        var failures: Data? = nil
        var failuresError: String? = nil
    }

    struct Candidate {
        var at: String? = nil
        var exact: Double? = nil
        var value: Any? = nil
        var error: String? = nil
    }

    static func object(_ data: Data?) -> [String: Any]? {
        data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
    }

    static func instant(_ raw: Any?) -> Double? {
        guard !(raw is Bool), let n = raw as? NSNumber, n.doubleValue.isFinite, n.doubleValue > 0 else { return nil }
        return n.doubleValue
    }

    static func epoch(_ raw: Double?) -> String? { raw.map { StatusExport.iso(Date(timeIntervalSince1970: $0)) } }

    static func present(_ raw: Any?) -> Any? { raw is NSNull ? nil : raw }

    static func candidates(_ input: Input, now: Date) -> [String: Candidate] {
        var out: [String: Candidate] = [:]
        let bestValue = object(input.best).flatMap { (try? LiveBest.parse($0, now: now)) == nil ? nil : $0 }
        let bestAt = instant(bestValue?["generated"])
        let bestUnusable = bestValue == nil ? (input.best == nil ? "not collected yet" : LiveBest.malformed) : nil
        out[best] = Candidate(at: epoch(bestAt), exact: bestAt, value: bestValue, error: input.bestError ?? bestUnusable)
        let reported = object(input.models).flatMap { modelsRecords($0) == nil ? nil : $0 }
        let generated = instant(reported?["generated"])
        let unusable = reported == nil ? (input.models == nil ? "not collected yet" : LiveModels.malformed) : nil
        out[models] = Candidate(at: epoch(generated), exact: generated, value: reported, error: input.modelsError ?? unusable)
        let counted = object(input.failures).flatMap { RepeatedFailures.parse($0) == nil ? nil : $0 }
        let countedAt = instant(counted?["generated"])
        let countedWhy = counted == nil ? (input.failures == nil ? "not collected yet" : RepeatedFailures.malformed) : nil
        out[failures] = Candidate(at: epoch(countedAt), exact: countedAt, value: counted, error: input.failuresError ?? countedWhy)
        let pass = object(input.ledger).flatMap { ledgerProgress($0) == nil ? nil : $0 }
        let passed = instant(pass?["generated"])
        let unread = pass == nil ? (input.ledger == nil ? "not collected yet" : LiveModels.malformed) : nil
        out[ledger] = Candidate(at: epoch(passed), exact: passed, value: pass.flatMap(ledgerProgress), error: input.ledgerError ?? unread)
        if var repos = object(input.repos) {
            let read = input.repos.flatMap { Ingest.decoded(RepoSnap.self, $0) }
            let readable = read?.repoRowsComplete == true && read?.prRowsComplete == true
            let why = input.reposError ?? present(repos["stale_reason"]) as? String ?? present(repos["error"]) as? String ?? (readable ? nil : LiveModels.malformed)
            let exact = instant(repos["fetched"])
            repos["stale_reason"] = nil
            repos["error"] = nil
            out["github"] = Candidate(at: epoch(exact), exact: exact, value: readable ? repos : nil, error: why)
        } else {
            out["github"] = Candidate(error: input.reposError ?? "not collected yet")
        }
        guard let vm = object(input.vm), let read = input.vm.flatMap({ Ingest.decoded(VMLocalSnap.self, $0) }) else {
            let why = input.vmError ?? (input.vm == nil ? "not collected yet" : LiveModels.malformed)
            for name in specs.keys where name != "github" && name != models && name != ledger && name != best && name != failures { out[name] = Candidate(error: why) }
            return out
        }
        let exact = instant(vm["collected"])
        let at = epoch(exact)
        let local = present(vm["vm"]) as? [String: Any]
        var counters = local
        counters?["orch"] = nil
        counters?["orch_token_expires"] = nil
        let localWhy = input.vmError ?? present(vm["vm_error"]) as? String ?? (local == nil ? "no VM data" : nil)
        out["vm"] = Candidate(at: at, exact: exact, value: counters, error: localWhy)
        let state = present(local?["orch"]) as? String
        out["orch"] = Candidate(at: at, exact: exact, value: state.map { ["state": $0] }, error: state == nil ? localWhy ?? "Orchestrator state unknown" : input.vmError)
        let expires = present(local?["orch_token_expires"])
        out["orch_token"] = Candidate(at: at, exact: exact, value: expires.map { ["expires": $0] }, error: expires == nil ? "expiry unknown" : input.vmError)
        let listed = present(vm["agents"]) as? [Any]
        let rowsRead = read.agents != nil && !read.unreadable.contains { $0 == "agents" || $0.hasPrefix("agents:") }
        let agentsWhy = listed == nil ? present(vm["agents_error"]) as? String ?? "no agent data" : rowsRead ? nil : "agent list unreadable"
        out["agents"] = Candidate(at: at, exact: exact, value: rowsRead ? listed : nil, error: input.vmError ?? agentsWhy)
        let rig = present(vm["rig_status"]) as? [String: Any]
        let rigWhy = input.vmError ?? (rig == nil ? present(vm["rig_status_error"]) as? String ?? "rig-status.json unreadable" : nil)
        func group(_ clock: String, _ keys: [String]) -> Candidate {
            guard let rig else { return Candidate(error: rigWhy) }
            let values = Dictionary(uniqueKeysWithValues: keys.map { ($0, rig[$0] ?? NSNull()) })
            return Candidate(at: present(rig[clock]) as? String, value: values, error: rigWhy)
        }
        out["rig_local"] = group("updated_at", ["storage_state", "guest_free_gib"])
        out["rig_host"] = group("checked_at", ["srv_vm_free_gib", "reboot_needed"])
        out["rig_cleanup"] = group("last_cleanup", ["cleanup_debt_gib"])
        return out
    }

    static func clockProblem(_ raw: String?, exact: Double? = nil, now: Date) -> String? {
        guard let raw else { return "clock missing" }
        guard let at = exact.map(Date.init(timeIntervalSince1970:)) ?? clockDate(raw) else { return "clock unreadable" }
        return at.timeIntervalSince(now) > clockSkew ? "clock in the future" : nil
    }

    static func text(_ value: Any?, include: Bool) -> Any {
        guard let value = present(value) else { return NSNull() }
        return include ? ["text": value, "trust": "untrusted"] : ["withheld": true, "trust": "untrusted"]
    }

    static func wrapRow(_ row: Any, keys: [String], include: Bool) -> Any {
        guard var d = row as? [String: Any] else { return row }
        for key in keys where d[key] != nil { d[key] = text(d[key], include: include) }
        return d
    }

    static func wrapAgent(_ row: Any, include: Bool) -> Any {
        guard var d = row as? [String: Any] else { return row }
        if let data = try? JSONSerialization.data(withJSONObject: d), let agent = try? JSONDecoder().decode(AgentSnap.self, from: data) {
            d["pr_key"] = agent.prKey ?? NSNull()
            d["issue_id"] = agent.issue ?? NSNull()
            d["agent_kind"] = agent.kind.rawValue
        }
        return wrapRow(d, keys: ["title"], include: include)
    }

    static func wrapped(_ name: String, _ value: Any, include: Bool) -> Any {
        switch name {
        case "github":
            guard var d = value as? [String: Any] else { return value }
            for key in ["prs", "recent"] {
                if let rows = d[key] as? [Any] { d[key] = rows.map { wrapRow($0, keys: ["title", "branch"], include: include) } }
            }
            return d
        case "agents":
            return (value as? [Any])?.map { wrapAgent($0, include: include) } ?? value
        default:
            return value
        }
    }

    static func build(_ input: Input, delivery doc: [String: Any], lastGood: [String: [String: Any]], now: Date, includeText: Bool)
        -> (document: [String: Any], lastGood: [String: [String: Any]]) {
        var kept = lastGood
        var facts: [String: Any] = [:]
        let found = candidates(input, now: now)
        for (name, spec) in specs {
            let c = found[name] ?? Candidate()
            var error = c.error
            if let value = c.value {
                if let problem = clockProblem(c.at, exact: c.exact, now: now) {
                    error = error ?? problem
                } else if error == nil, let at = c.at, let date = clockDate(at) {
                    let held = (kept[name]?["at"] as? String).flatMap(clockDate)
                    if held == nil || held! <= date { kept[name] = ["at": at, "value": value] }
                }
            }
            let held = kept[name]
            let heldAt = (held?["at"] as? String).flatMap(clockDate)
            let state: String
            if held == nil || heldAt == nil {
                state = "missing"
            } else if error != nil || now.timeIntervalSince(heldAt!) > spec.limit {
                state = "stale"
            } else {
                state = "fresh"
            }
            facts[name] = ["source": spec.source, "clock": spec.clock, "stale_after_s": Int(spec.limit), "state": state,
                           "at": held?["at"] ?? NSNull(), "expires_at": heldAt.map { StatusExport.iso($0.addingTimeInterval(spec.limit)) } ?? NSNull(),
                           "error": error ?? NSNull(), "value": held?["value"].map { wrapped(name, $0, include: includeText) } ?? NSNull()] as [String: Any]
        }
        let published = StatusExport.iso(now), expires = StatusExport.iso(now.addingTimeInterval(fileLife))
        facts[delivery] = ["source": "derived", "clock": "published_at", "stale_after_s": Int(fileLife), "state": "fresh", "at": published,
                           "expires_at": expires, "error": NSNull(), "value": doc] as [String: Any]
        return (["schema": schema, "role": role, "published_at": published, "expires_at": expires, "text_included": includeText,
                 "not_from_vm": notFromVM, "facts": facts], kept)
    }

    static func sized(_ input: Input, delivery doc: [String: Any], lastGood: [String: [String: Any]], now: Date, includeText: Bool, limit: Int = sizeLimit)
        -> (data: Data, lastGood: [String: [String: Any]])? {
        func serial(_ document: [String: Any]) -> Data? {
            JSONSerialization.isValidJSONObject(document) ? try? JSONSerialization.data(withJSONObject: document, options: [.sortedKeys]) : nil
        }
        let built = build(input, delivery: doc, lastGood: lastGood, now: now, includeText: includeText)
        guard let data = serial(built.document), JSONSerialization.isValidJSONObject(built.lastGood) else { return nil }
        if data.count <= limit { return (data, built.lastGood) }
        var lean = input
        lean.models = nil
        lean.modelsError = modelsTooLarge
        var kept = lastGood
        kept[models] = nil
        let shed = build(lean, delivery: doc, lastGood: kept, now: now, includeText: includeText)
        guard let smaller = serial(shed.document), smaller.count <= limit else { return nil }
        return (smaller, shed.lastGood)
    }

    static func write(_ data: Data, to url: URL) -> Bool {
        let manager = FileManager.default
        try? manager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let temporary = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).\(getpid())")
        guard manager.createFile(atPath: temporary.path, contents: data, attributes: [.posixPermissions: 0o600]) else { return false }
        guard rename(temporary.path, url.path) == 0 else {
            try? manager.removeItem(at: temporary)
            return false
        }
        return true
    }

    struct Fact {
        let name: String
        let clock: String
        let at: Date?
        let atText: String?
        let error: String?
        let value: Any?
        let state: String
        var expiresAt: Date? = nil
    }

    struct Reading {
        var state: String
        var reason: String? = nil
        var publishedAt: Date? = nil
        var textIncluded: Bool? = nil
        var facts: [String: Fact] = [:]
        var expiresAt: Date? = nil

        func until(_ name: String) -> Date? { [expiresAt, facts[name]?.expiresAt].compactMap { $0 }.min() }
        func usable(_ names: [String]) -> Bool { state == "fresh" && names.allSatisfy { facts[$0]?.state == "fresh" } }
    }

    static func read(_ data: Data?, now: Date) -> Reading {
        guard let data else { return Reading(state: "missing", reason: "no snapshot") }
        guard data.count <= sizeLimit else { return Reading(state: "invalid", reason: "snapshot too large") }
        guard let d = object(data) else { return Reading(state: "invalid", reason: "snapshot unreadable") }
        guard d["schema"] as? String == schema else { return Reading(state: "invalid", reason: "unsupported version") }
        guard d["role"] as? String == role else { return Reading(state: "invalid", reason: "unexpected role") }
        guard let included = d["text_included"] as? Bool, let facts = d["facts"] as? [String: Any] else { return Reading(state: "invalid", reason: "snapshot incomplete") }
        guard let published = clockDate(d["published_at"] as? String) else { return Reading(state: "invalid", reason: "publish time unreadable") }
        guard let expires = clockDate(d["expires_at"] as? String) else { return Reading(state: "invalid", reason: "expiry unreadable") }
        guard published.timeIntervalSince(now) <= clockSkew else { return Reading(state: "invalid", reason: "published in the future", publishedAt: nil) }
        let live = now.timeIntervalSince(published) <= fileLife && now <= expires
        var reading = Reading(state: live ? "fresh" : "stale", reason: live ? nil : "publisher stopped", publishedAt: published, textIncluded: included,
                              expiresAt: min(expires, published.addingTimeInterval(fileLife)))
        for (name, spec) in specs {
            let entry = facts[name] as? [String: Any]
            let atText = present(entry?["at"]) as? String
            let at = clockDate(atText)
            let ends = clockDate(present(entry?["expires_at"]) as? String)
            let error = present(entry?["error"]) as? String
            let value = present(entry?["value"])
            let state: String
            if entry == nil || value == nil {
                state = "missing"
            } else if at == nil {
                state = "invalid"
            } else if at!.timeIntervalSince(now) > clockSkew || at!.timeIntervalSince(published) > clockSkew {
                state = "invalid"
            } else if ends == nil || ends! < at! {
                state = "invalid"
            } else if error != nil || !live || now > ends! || now.timeIntervalSince(at!) > spec.limit {
                state = "stale"
            } else {
                state = "fresh"
            }
            reading.facts[name] = Fact(name: name, clock: spec.clock, at: state == "invalid" ? nil : at, atText: atText, error: error, value: value, state: state,
                                      expiresAt: ends.map { min($0, (at ?? $0).addingTimeInterval(spec.limit)) })
        }
        return reading
    }

    static func plain(_ value: Any) -> Any {
        if let list = value as? [Any] { return list.map(plain) }
        guard let d = value as? [String: Any] else { return value }
        var out: [String: Any] = [:]
        for (key, raw) in d {
            if let cell = raw as? [String: Any], cell["trust"] as? String == "untrusted" {
                if let text = cell["text"] {
                    out[key] = text
                } else if key == "title" {
                    out[key] = withheldTitle
                }
            } else {
                out[key] = plain(raw)
            }
        }
        return out
    }

    static func encoded(_ value: Any) -> Data? {
        JSONSerialization.isValidJSONObject(value) ? try? JSONSerialization.data(withJSONObject: value) : nil
    }

    static func reposPayload(_ reading: Reading) -> (data: Data, at: Date)? {
        guard reading.usable(["github"]), let fact = reading.facts["github"], let at = fact.at, let value = fact.value as? [String: Any],
              let fetched = (value["fetched"] as? NSNumber)?.doubleValue, abs(fetched - at.timeIntervalSince1970) < 2,
              let data = encoded(plain(value)) else { return nil }
        return (data, Date(timeIntervalSince1970: fetched))
    }

    static func rigStatus(_ reading: Reading) -> [String: Any]? {
        let f = reading.facts
        guard reading.state == "fresh", f["rig_local"]?.state == "fresh", var rig = f["rig_local"]?.value as? [String: Any], let updated = f["rig_local"]?.atText else { return nil }
        rig["updated_at"] = updated
        if let host = f["rig_host"], host.state != "invalid", let values = host.value as? [String: Any] {
            rig.merge(values) { _, new in new }
            rig["checked_at"] = host.atText
        }
        if let cleanup = f["rig_cleanup"], cleanup.state != "invalid", let values = cleanup.value as? [String: Any] {
            rig.merge(values) { _, new in new }
            rig["last_cleanup"] = cleanup.atText
        }
        return rig
    }

    static func vmPayload(_ reading: Reading) -> (data: Data, at: Date, complete: Bool)? {
        guard reading.state == "fresh" else { return nil }
        func fresh(_ name: String) -> Fact? { reading.facts[name].flatMap { $0.state == "fresh" ? $0 : nil } }
        let joined = ["vm", "orch", "agents"].compactMap(fresh)
        guard let at = joined.first?.at, joined.allSatisfy({ $0.at == at }) else { return nil }
        var vm = fresh("vm")?.value as? [String: Any] ?? [:]
        if let orch = fresh("orch")?.value as? [String: Any] { vm["orch"] = orch["state"] }
        if let token = fresh("orch_token")?.value as? [String: Any] { vm["orch_token_expires"] = token["expires"] }
        var snap: [String: Any] = ["collected": at.timeIntervalSince1970, "vm": vm]
        if let agents = fresh("agents")?.value {
            snap["agents"] = plain(agents)
        } else {
            snap["agents_error"] = reading.facts["agents"]?.error ?? "no agent data"
        }
        snap["rig_status"] = rigStatus(reading)
        return encoded(snap).map { ($0, at, joined.count == 3) }
    }

    static func modelsOutput(_ data: Data?, error: String?) -> (data: Data?, error: String?) {
        guard error == nil else { return (nil, error) }
        guard (data?.count ?? 0) <= modelsLimit else { return (nil, modelsTooLarge) }
        guard let d = object(data), d["schema"] as? String == LiveModels.schema else { return (nil, LiveModels.malformed) }
        if let why = d["error"] as? String { return (nil, why) }
        return modelsRecords(d) == nil ? (nil, LiveModels.malformed) : (data, nil)
    }

    static func failuresOutput(_ data: Data?, error: String?) -> (data: Data?, error: String?) {
        guard error == nil else { return (nil, error) }
        guard let d = object(data) else { return (nil, RepeatedFailures.malformed) }
        if let why = d["error"] as? String { return (nil, why) }
        return RepeatedFailures.parse(d) == nil ? (nil, RepeatedFailures.malformed) : (data, nil)
    }

    static func failuresPayload(_ reading: Reading) -> RepeatedFailures? {
        guard reading.usable([failures]), let value = reading.facts[failures]?.value as? [String: Any] else { return nil }
        return RepeatedFailures.parse(value)
    }

    static func ledgerProgress(_ report: [String: Any]) -> [String: Any]? {
        guard report["schema"] as? String == ledgerSchema, instant(report["generated"]) != nil else { return nil }
        return report["progress"] as? [String: Any]
    }

    static func ledgerOutput(_ data: Data?, error: String?) -> (data: Data?, error: String?) {
        guard error == nil else { return (nil, error) }
        guard let report = object(data), report["schema"] as? String == ledgerSchema else { return (nil, LiveModels.malformed) }
        if let why = report["error"] as? String { return (nil, why) }
        guard ledgerProgress(report) != nil else { return (nil, LiveModels.malformed) }
        let steps = report["errors"] as? [String] ?? []
        return (data, steps.isEmpty ? nil : steps.joined(separator: "; "))
    }

    static func bestOutput(_ data: Data?, error: String?, now: Date) -> (data: Data?, error: String?) {
        guard error == nil else { return (nil, error) }
        guard let d = object(data), d["schema"] as? String == LiveBest.schema else { return (nil, LiveBest.malformed) }
        if let why = d["error"] as? String { return (nil, why) }
        do {
            _ = try LiveBest.parse(d, now: now)
            return (data, nil)
        } catch {
            return (nil, (error as? DecisionError)?.errorDescription ?? LiveBest.malformed)
        }
    }

    static func bestPayload(_ reading: Reading, now: Date) -> (live: LiveBest?, problem: String?) {
        guard let fact = reading.facts[best], let value = fact.value as? [String: Any] else { return (nil, "live Best feed " + (reading.facts[best]?.state ?? reading.state)) }
        do {
            let live = try LiveBest.parse(value, now: now)
            let usable = reading.usable([best])
            return (live, usable ? nil : "live Best feed " + (reading.state == "fresh" ? fact.state : reading.state) + (fact.error.map { ": " + $0 } ?? ""))
        } catch {
            return (nil, "live Best feed refused: " + ((error as? DecisionError)?.errorDescription ?? LiveBest.malformed))
        }
    }

    static func modelsRow(_ ref: [String: Any], runs: [String: Any]) -> [String: Any]? {
        guard Set(ref.keys).isSubset(of: LiveModels.refKeys), let id = ref["run"] as? String, let run = runs[id] as? [String: Any] else { return nil }
        return run.merging(ref) { _, new in new }
    }

    static func modelsRefFits(_ ref: [String: Any], run: [String: Any], owners: Int) -> Bool {
        let via = ref["pr_via"] as? String
        let roleVia = present(ref["role_via"])
        let roleFits: Bool
        switch ref["role"] as? String {
        case "review": roleFits = roleVia as? String == "title-hint"
        case LiveModels.linkedRole: roleFits = roleVia == nil && via == "orch-link"
        default: roleFits = false
        }
        guard roleFits else { return false }
        switch run["allocation"] as? String {
        case "shared": return owners > 1 && (via == "orch-link" || via == "title-hint")
        case "orch-link": return owners == 1 && via == "orch-link"
        case LiveModels.unallocated: return owners == 1 && via == "title-hint"
        default: return false
        }
    }

    static func modelsTotalFits(_ record: [String: Any], rows: [[String: Any]]) -> Bool {
        let shared = rows.filter { $0["allocation"] as? String == "shared" }.count
        guard record["shared_runs"] as? Int == shared else { return false }
        let total = present(record["tokens"]), basis = present(record["tokens_basis"])
        guard let total else { return basis == nil }
        guard basis as? String == LiveModels.linkEstimate, let tokens = total as? [String: Any], record["history_complete"] as? Bool == true,
              rows.allSatisfy({ $0["allocation"] as? String == "orch-link" && $0["pr_via"] as? String == "orch-link" }) else { return false }
        for bucket in LiveModels.buckets {
            let amounts = rows.compactMap { (present($0["tokens"]) as? [String: Any])?[bucket] as? Int }
            guard amounts.count == rows.count, let sum = PRModels.Tokens.sum(amounts), tokens[bucket] as? Int == sum else { return false }
        }
        return true
    }

    static func modelsConsistent(_ prs: [String: Any], runs: [String: Any]) -> Bool {
        var owners: [String: Int] = [:]
        for raw in prs.values {
            let refs = (raw as? [String: Any])?["run_refs"] as? [[String: Any]] ?? []
            let ids = refs.compactMap { $0["run"] as? String }
            guard ids.count == refs.count, Set(ids).count == ids.count else { return false }
            for id in ids { owners[id, default: 0] += 1 }
        }
        for raw in prs.values {
            guard let record = raw as? [String: Any], let refs = record["run_refs"] as? [[String: Any]] else { return false }
            var rows: [[String: Any]] = []
            for ref in refs {
                guard let id = ref["run"] as? String, let run = runs[id] as? [String: Any], let row = modelsRow(ref, runs: runs),
                      modelsRefFits(ref, run: run, owners: owners[id] ?? 0) else { return false }
                rows.append(row)
            }
            guard modelsTotalFits(record, rows: rows) else { return false }
        }
        return true
    }

    static func modelsRecords(_ value: [String: Any]) -> LiveModels? {
        guard value["schema"] as? String == LiveModels.schema, let generated = instant(value["generated"]), let runs = value["runs"] as? [String: Any],
              let prs = value["prs"] as? [String: Any], !(value["coverage"] != nil && value["coverage"] as? [String: Any] == nil) else { return nil }
        guard modelsConsistent(prs, runs: runs) else { return nil }
        var records: [String: PRModels] = [:]
        for (key, raw) in prs {
            guard var record = raw as? [String: Any], let refs = record["run_refs"] as? [[String: Any]], !refs.isEmpty else { return nil }
            var rows: [[String: Any]] = []
            for ref in refs {
                guard let row = modelsRow(ref, runs: runs), LiveModels.allocations.contains(row["allocation"] as? String ?? ""),
                      row["settings_basis"] as? String == PRModels.Run.requestedBasis else { return nil }
                rows.append(row)
            }
            record["run_refs"] = nil
            record["runs"] = rows
            record["reviews"] = rows.filter { $0["role"] as? String == "review" }
            guard let data = encoded(record), let decoded = try? JSONDecoder().decode(PRModels.self, from: data),
                  decoded.isLive, decoded.unreadable.isEmpty, decoded.tokens_complete != true else { return nil }
            records[key] = decoded
        }
        let coverage = value["coverage"] as? [String: Any]
        let source = present(coverage?["github"])
        guard source == nil || (source as? [String: Any])?["state"] is String else { return nil }
        let github = source as? [String: Any]
        var unknown: Int?
        if let count = github?["unknown"] as? Int {
            guard let sum = PRModels.Tokens.sum([count, github?["unresolved"] as? Int ?? 0]) else { return nil }
            unknown = sum
        }
        let shown = (github?["state"] as? String).map {
            LiveModels.GitHubCoverage(at: instant(github?["at"]).map(Date.init(timeIntervalSince1970:)), state: $0, prs: github?["prs"] as? Int, unknown: unknown)
        }
        return LiveModels(records: PRModels.checkedIdentities(records), at: Date(timeIntervalSince1970: generated), attributed: prs.count, github: shown)
    }

    static func modelsPayload(_ reading: Reading) -> LiveModels? {
        guard reading.usable([models]), let fact = reading.facts[models], let at = fact.at, let value = fact.value as? [String: Any],
              var live = modelsRecords(value) else { return nil }
        live.at = at
        return live
    }

    static func modelsProblem(_ reading: Reading) -> String? {
        guard reading.state == "fresh" else { return "live Models feed " + reading.state }
        guard let fact = reading.facts[models], fact.state != "fresh" else { return "live Models data unreadable" }
        return "live Models feed " + fact.state + (fact.error.map { ": " + $0 } ?? "")
    }

    static func freshness(_ r: Reading, now: Date) -> [String: Any] {
        let facts = specs.keys.sorted().map { name -> [String: Any] in
            let f = r.facts[name]
            return ["fact": name, "state": f?.state ?? "missing", "clock": specs[name]?.clock ?? "", "at": f?.atText ?? NSNull(),
                    "age_s": f?.at.map { max(0, Int(now.timeIntervalSince($0))) } ?? NSNull(), "error": f?.error ?? NSNull()]
        }
        return ["state": r.state, "reason": r.reason ?? NSNull(), "published_at": r.publishedAt.map(StatusExport.iso) ?? NSNull(),
                "age_s": r.publishedAt.map { max(0, Int(now.timeIntervalSince($0))) } ?? NSNull(), "text_included": r.textIncluded ?? NSNull(), "facts": facts]
    }
}

extension Collected {
    mutating func importSnapshot(_ data: Data?, now: Date) -> (snap: RepoSnap, at: Date)? {
        let reading = PublishedStatus.read(data, now: now)
        let held = feed?.owned ?? []
        var next = Feed(state: reading.state, reason: reading.reason, publishedAt: reading.publishedAt)
        var fresh: (snap: RepoSnap, at: Date)?
        if let payload = PublishedStatus.reposPayload(reading) {
            let prior = status[.repos]
            let newer = (prior?.sampleAt).map { payload.at > $0 } ?? true
            if newer || prior?.origin != PublishedStatus.origin || prior?.refreshFailed == true {
                let applied = applyRepos(payload.data, now: now, origin: PublishedStatus.origin)
                if newer { fresh = applied }
            }
            if status[.repos]?.origin == PublishedStatus.origin, status[.repos]?.error == nil {
                next.owned.insert(.repos)
                status[.repos]?.expiresAt = reading.until("github")
            }
        }
        var partial = false
        if let payload = PublishedStatus.vmPayload(reading) {
            let prior = status[.agents]
            let localWorks = prior?.origin != PublishedStatus.origin && prior?.freshness(.agents, now: now) == .fresh
            let due = (prior?.sampleAt).map { payload.at > $0 } ?? true || prior?.origin != PublishedStatus.origin || prior?.refreshFailed == true
            if due && (payload.complete || !localWorks) { applyVM(payload.data, now: now, origin: PublishedStatus.origin) }
            if status[.agents]?.origin == PublishedStatus.origin, status[.agents]?.error == nil {
                status[.agents]?.expiresAt = reading.until(payload.complete ? "agents" : "vm") ?? reading.expiresAt
                if payload.complete { next.owned.insert(.agents) } else { partial = true }
            }
        } else if let rig = PublishedStatus.rigStatus(reading), let data = PublishedStatus.encoded(rig), let read = Ingest.decoded(RigStatus.self, data) {
            rigStatus = read
        }
        if !reading.usable([PublishedStatus.models]) || reading.facts[PublishedStatus.models]?.at != liveModels?.at {
            liveModels = PublishedStatus.modelsPayload(reading)
        }
        modelsProblem = liveModels == nil ? PublishedStatus.modelsProblem(reading) : nil
        let shownBest = PublishedStatus.bestPayload(reading, now: now)
        if let live = shownBest.live { liveBest = live }
        bestProblem = shownBest.problem
        repeatedFailures = PublishedStatus.failuresPayload(reading)
        if reading.state == "fresh", next.owned.count < 2 {
            let unusable = PublishedStatus.specs.keys.sorted().filter { reading.facts[$0]?.state != "fresh" }
            next.reason = unusable.isEmpty ? "facts unusable" : "not fresh: " + unusable.joined(separator: ", ")
        }
        for source in held.subtracting(next.owned) where status[source]?.origin == PublishedStatus.origin && !(partial && source == .agents) {
            fail(source, reading.reason ?? "snapshot fact unusable")
        }
        feed = next
        return fresh
    }
}
