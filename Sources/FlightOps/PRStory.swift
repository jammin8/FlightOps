import Foundation





enum PRStory {
    static let keepDays = 365
    static let keepPending = 10_000
    static var dir: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("FlightOps/prs")
    }
    private struct Pending: Codable {
        let id: String
        let line: String
        var sig: String? = nil
        var closed: Double? = nil
    }
    private struct State: Codable {
        var sig: [String: String] = [:]
        var closed: [String: Double] = [:]
        var pending: [Pending]? = nil
        var refused: Int? = nil
        var refusedSig: [String: String]? = nil
    }
    private static let queue = DispatchQueue(label: "flightops.prstory", qos: .utility)
    private static let unsavedLock = NSLock()
    private static var unsaved: [String: State] = [:]

    private static func unsavedState(_ dir: URL) -> State? {
        unsavedLock.lock()
        defer { unsavedLock.unlock() }
        return unsaved[dir.path]
    }

    private static func setUnsaved(_ state: State?, _ dir: URL) {
        unsavedLock.lock()
        defer { unsavedLock.unlock() }
        unsaved[dir.path] = state
    }

    static func record(prs: [PRRow], merged: [Merge], timelines: [String: PRTimeline], models: [String: PRModels],
                       now: Double, then done: @escaping @Sendable (String?, Int) -> Void = { _, _ in }) {
        queue.async {
            let problem = store(prs: prs, merged: merged, timelines: timelines, models: models, in: dir, now: now)
            done(problem, refusedCount(in: dir))
        }
    }

    static func refusedCount(in dir: URL) -> Int {
        (unsavedState(dir) ?? (try? JSONDecoder().decode(State.self, from: Data(contentsOf: dir.appendingPathComponent(".state.json")))))?.refused ?? 0
    }

    @discardableResult
    static func store(prs: [PRRow], merged: [Merge], timelines: [String: PRTimeline], models: [String: PRModels],
                      in dir: URL, now: Double, limit: Int = keepPending) -> String? {
        let fm = FileManager.default
        let stateFile = dir.appendingPathComponent(".state.json")
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        var st = unsavedState(dir) ?? (try? JSONDecoder().decode(State.self, from: Data(contentsOf: stateFile))) ?? State()
        var pending = st.pending ?? []
        var refused = 0
        func refuse(_ id: String, _ sig: String) {
            refused += 1
            guard st.refusedSig?[id] != sig else { return }
            st.refusedSig = (st.refusedSig ?? [:]).merging([id: sig]) { _, new in new }
            st.refused = (st.refused ?? 0) + 1
        }
        for p in prs {
            let pm = models[p.id]
            let entry: [String: Any?] = [
                "event": "update", "status": PRStatus.of(p).label, "ci": p.ci, "merge_state": p.merge_state,
                "review": p.review, "failed_checks": p.failed_checks, "flaky": p.flaky, "head": p.head,
                "branch": p.branch, "title": p.title, "built_by": timelines[p.id]?.builder,
                "reviewed_by": pm?.reviewed_by, "reworked_by": pm?.reworked_by, "rework_runs": pm?.rework_runs,
            ]
            let sig = signature(entry.filter { $0.key != "title" })
            if (pending.last { $0.id == p.id && $0.sig != nil }?.sig ?? st.sig[p.id]) == sig { st.refusedSig?[p.id] = nil; continue }
            if pending.count >= limit { refuse(p.id, sig); continue }
            st.refusedSig?[p.id] = nil
            if let line = line(p.id, entry, now: now) { pending.append(Pending(id: p.id, line: line, sig: sig)) }
        }
        for mg in merged where st.closed[mg.id] == nil && !pending.contains(where: { $0.id == mg.id && $0.closed != nil }) {
            guard let t = timelines[mg.id], let mergedAt = mg.mergedDate?.timeIntervalSince1970 else { continue }
            let totals = t.totals(now: mergedAt).reduce(into: [String: Int]()) { $0[$1.key.label] = Int($1.value) }
            let rw = Analytics.rework(t, now: mergedAt)
            let pm = models[mg.id]
            let entry: [String: Any?] = [
                "event": "merged", "merged_at": Int(mergedAt), "opened": t.opened.map { Int($0) },
                "lifecycle_s": t.opened.map { Int(mergedAt - $0) }, "seconds_by_status": totals,
                "rework_loops": rw.loops, "rework_s": Int(rw.seconds), "flaky": t.flaky,
                "built_by": t.builder, "reviewed_by": pm?.reviewed_by, "reworked_by": pm?.reworked_by,
                "rework_runs": pm?.rework_runs, "title": mg.title,
            ]
            if pending.count >= limit { refuse(mg.id, "merged"); continue }
            st.refusedSig?[mg.id] = nil
            if let line = line(mg.id, entry, now: now) { pending.append(Pending(id: mg.id, line: line, closed: now)) }
        }
        var blocked = Set<String>()
        var kept: [Pending] = []
        for e in pending {
            let file = dir.appendingPathComponent(e.id.replacingOccurrences(of: "#", with: "-") + ".jsonl")
            if !blocked.contains(e.id), append(e.line, to: file) {
                if let sig = e.sig { st.sig[e.id] = sig }
                if let closed = e.closed { st.closed[e.id] = closed }
            } else {
                blocked.insert(e.id)
                kept.append(e)
            }
        }
        st.pending = kept.isEmpty ? nil : kept
        let cutoff = now - Double(keepDays) * 86400
        st.closed = st.closed.filter { $0.value > cutoff }
        st.sig = st.sig.filter { k, _ in prs.contains { $0.id == k } || st.closed[k] != nil }
        var problem: String? = kept.isEmpty ? nil : kept.count == 1 ? "1 entry waiting to be written" : "\(kept.count) entries waiting to be written"
        if refused > 0 { problem = (problem.map { $0 + " · " } ?? "") + "\(refused) new paused until these land" }
        do {
            try JSONEncoder().encode(st).write(to: stateFile, options: .atomic)
            setUnsaved(nil, dir)
        } catch {
            setUnsaved(st, dir)
            problem = "progress not saved"
        }
        for f in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        where f.pathExtension == "jsonl" {
            let mod = (try? f.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? Date()
            if mod.timeIntervalSince1970 < cutoff { try? fm.removeItem(at: f) }
        }
        return problem
    }

    private static func signature(_ d: [String: Any?]) -> String {
        d.keys.sorted().map { "\($0)=\(String(describing: d[$0]!))" }.joined(separator: "|")
    }

    private static func line(_ id: String, _ entry: [String: Any?], now: Double) -> String? {
        var obj: [String: Any] = ["t": Int(now), "pr": id]
        for (k, v) in entry { if let v { obj[k] = v } }
        return (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])).flatMap { String(data: $0, encoding: .utf8) }
    }

    private static func append(_ line: String, to file: URL) -> Bool {
        let data = Data((line + "\n").utf8)
        guard FileManager.default.fileExists(atPath: file.path) else {
            return FileManager.default.createFile(atPath: file.path, contents: data, attributes: [.posixPermissions: 0o600])
        }
        do {
            let handle = try FileHandle(forWritingTo: file)
            defer { try? handle.close() }
            try handle.seekToEnd()
            try handle.write(contentsOf: data)
            return true
        } catch {
            return false
        }
    }
}
