import SwiftUI



struct MissionTab: View {
    @ObservedObject var m: Monitor
    @Binding var tab: Int

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            verdict
            LaunchCard(m: m, tab: $tab)
            OpsSection(title: "Delivery flow", key: "board", trailing: bottleneckView, expandable: true) { DeliveryBoard(m: m) }
            OpsSection(title: "Machines") {

                TimelineView(.periodic(from: .now, by: 1)) { ctx in machines(at: ctx.date) }
            }
            OpsSection(title: "Budgets") { budgets }
        }
    }





    private var verdict: some View {
        let v = m.verdict
        let health = v.health
        let tint: Color = health == .ok ? Theme.hud : color(health)
        return TapCard(padding: 12) {
            HStack(spacing: 10) {
                Image(systemName: health == .ok ? "checkmark.seal.fill" : healthSymbol(health))
                    .font(.title2).foregroundStyle(tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text(v.headline).font(.headline).lineLimit(1)
                    if let report = m.summaryText {
                        Text(report).font(.caption).foregroundStyle(.secondary).lineLimit(4).fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text(pulse).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer()
                if !v.open.isEmpty {
                    Button { m.dismissShown() } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).frame(minWidth: 20, minHeight: 20).contentShape(Rectangle()) }
                        .buttonStyle(.plain).help("Dismiss all until they change")
                }
            }
        } detail: {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(v.open) { i in
                    item(i.health, i.title, i.detail) { m.dismiss([(i.key, i.says)]) }
                }
                if !v.waiting.isEmpty { Text("Waiting on you").font(.caption.weight(.medium)).padding(.top, 2) }
                ForEach(v.waiting) { i in
                    HStack(spacing: 6) { Text(i.title).foregroundStyle(.primary); Text(i.detail).lineLimit(1) }
                }
                HealthGrid(checks: m.checks)
            }
        }
    }

    private func item(_ h: Health, _ title: String, _ detail: String, dismiss: @escaping () -> Void) -> some View {
        HStack(spacing: 6) {
            Image(systemName: healthSymbol(h)).font(.caption2).foregroundStyle(color(h)).accessibilityHidden(true)
            Text(title).foregroundStyle(.primary)
            Text(detail).lineLimit(1)
            Spacer(minLength: 0)
            Button(action: dismiss) { Image(systemName: "xmark").frame(minWidth: 20, minHeight: 20).contentShape(Rectangle()) }.buttonStyle(.plain).help("Dismiss until it changes")
        }
    }


    private var pulse: String {
        var parts: [String] = []
        let waiting = m.verdict.waiting.count
        if waiting > 0 { parts.append("\(waiting) waiting on you") }
        if let merged = m.merged24h { parts.append("\(merged) merged in 24 h") }
        if m.fresh(.repos) { parts.append("\(FlowBoard.countText(m.world, exact: m.world.prsKnown ? m.openPRs.count : nil, observed: m.openPRs.count)) open PRs") }
        if let a = m.agentsWorking { parts.append(a == 1 ? "1 agent working" : "\(a) agents working") }
        return parts.isEmpty ? "activity unknown" : parts.joined(separator: " · ")
    }




    private var bottleneckView: AnyView? {
        guard let top = FlowBoard.bottleneck(m.world) else { return nil }
        return AnyView(HStack(spacing: 4) {
            Text("bottleneck").font(.caption).foregroundStyle(.secondary)
            StatusPill(label: "\(top.stage.label) \(top.count)", color: stageColor(top.stage), font: .caption)
        })
    }



    private func machines(at date: Date) -> some View {
        let now = date.timeIntervalSince1970
        let rigAge = m.age(.rig, at: date), macAge = m.age(.mac, at: date)
        let lanes = m.rigCI?.lanes
        let host = m.fresh(.rig) ? m.host : nil
        return VStack(spacing: 6) {
            HStack(alignment: .top, spacing: 6) {
                tile("rig", "CI host", "cpu", face: host?.temp_c.map { "\($0)°C" }) {
                    if let h = host {
                        Text("CPU \(h.cpu.map { "\($0)%" } ?? "–") · load \(h.load?.first ?? "–")")
                        Text("Memory \(h.mem_used_gb.map { String(format: "%.0f", $0) } ?? "–") of \(h.mem_total_gb.map(String.init) ?? "–") GB")
                        Text("Disk \(h.disk_pct.map { "\($0)%" } ?? "–") · up \(h.uptime_h.map { duration($0 * 3600) } ?? "–")")
                    } else { Text("no data") }
                    Text(m.world.storageFact.line)
                }
                tile("orch", "Agents", "sparkles", face: m.agentsWorking.map { "\($0) working" }) {
                    let list = (m.agentsKnown ? m.agents ?? [] : []).filter { $0.inTurn || $0.waiting }
                    if !m.agentsKnown { Text("no data") } else if list.isEmpty { Text("none running") }
                    ForEach(AgentKind.tally(list.filter(\.inTurn).map(\.kind)), id: \.0) { kind, n in
                        HStack { Text(kind.rawValue); Spacer(); Text("\(n)").monospacedDigit() }
                    }
                    let waiting = list.filter(\.waiting).count
                    if waiting > 0 { HStack { Text("Waiting on you"); Spacer(); Text("\(waiting)").monospacedDigit() }.foregroundStyle(Theme.caution) }
                    if let feed = check("vm-feed") { Text("VM feed: \(feed.detail)") }
                }
                tile("ci-light", "Review gate", "checkmark.shield", face: nil) {
                    Text(check("ci-light")?.detail ?? "no data")
                    Text("Merge publisher: \(m.world.publisherFact.detail)")
                }
            }
            LaneCard(title: "Backend CI", symbol: "server.rack", host: "CI host", check: check("ci-backend"),
                     jobs: FlowBoard.linked(m.fresh(.rig) ? LaneTiming.backend(m.rigCIGuests, lanes?.backend, age: rigAge) : [], m.world),
                     idle: m.ciGuestsKnown ? LaneTiming.idle(lastDone: lanes?.backend?.last_done, now: now) : nil,
                     typical: [lanes?.backend?.fast_s.map { "fast ~\(duration($0))" },
                               lanes?.backend?.integration_s.map { "integration ~\(duration($0))" },
                               lanes?.backend?.fast_queue_s.map { "queue ~\(duration($0))" },
                               FlowBoard.rigCapacity(m.world).note].compactMap { $0 })
            LaneCard(title: "Web CI", symbol: "globe", host: "CI host", check: check("ci-web"),
                     jobs: FlowBoard.linked(m.fresh(.rig) ? LaneTiming.web(m.rigCIGuests, lanes?.web, age: rigAge) : [], m.world),
                     idle: m.ciGuestsKnown ? LaneTiming.idle(lastDone: lanes?.web?.last_done, now: now) : nil,
                     typical: [lanes?.web?.run_s.map { "run ~\(duration($0))" },
                               FlowBoard.waitNote(FlowBoard.heartbeat(m.world, m.rigCI?.web))].compactMap { $0 })
            LaneCard(title: "iOS CI", symbol: "iphone", host: "Mac", check: check("ci-ios"),
                     jobs: FlowBoard.linked(LaneTiming.ios(m.iosJobs ?? [], m.mac?.iosLane, age: macAge), m.world),
                     idle: m.iosJobs == nil || Rules.iosWork(m.world) != nil ? nil : LaneTiming.idle(lastDone: m.mac?.iosLane?.last_done, now: now),
                     typical: [m.mac?.iosLane?.run_s.map { "job ~\(duration($0))" },
                               m.mac?.iosLane?.queue_s.map { "queue ~\(duration($0))" }].compactMap { $0 })
        }
    }

    private func check(_ id: String) -> Check? { m.checks.first { $0.id == id } }



    private func tile<D: View>(_ id: String, _ title: String, _ symbol: String, face: String?,
                               @ViewBuilder detail: () -> D) -> some View {
        let c = check(id)
        let h = c?.health ?? .unknown
        return TapCard {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Image(systemName: symbol).font(.caption2).foregroundStyle(.secondary)
                    Text(title).font(.caption.weight(.medium)).lineLimit(1)
                    Spacer(minLength: 0)
                    Image(systemName: h == .ok ? "checkmark.circle.fill" : healthSymbol(h)).font(.caption).foregroundStyle(h == .ok ? Theme.hud : color(h)).accessibilityLabel(h == .ok ? "OK" : h == .warn ? "Warning" : h == .bad ? "Problem" : "Unknown")
                }
                Text(face ?? c?.detail ?? "no data").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        } detail: {
            VStack(alignment: .leading, spacing: 2) { detail() }
        }
        .help("\(title): \(c?.detail ?? "no data")")
    }



    private var budgets: some View {

        let gh = check("github")?.health == .unknown ? nil : m.world.githubBudget?.budget.value
        let codex = check("codex")?.health == .unknown ? nil : m.mac?.codex?.value
        let now = Date().timeIntervalSince1970
        return HStack(alignment: .top, spacing: 6) {
            meter("GitHub API", value: gh.map { Double($0.core) / Double(max($0.core_limit, 1)) }, low: true, health: check("github")?.health) {
                if let g = gh {
                    Text("\(g.core) of \(g.core_limit) left")
                    Text("resets in \(duration(max(0, g.core_reset - Int(now))))")
                    if let out = Analytics.projection(m.flow, key: "github", limit: 0, now: now), Int(out) < g.core_reset - Int(now) {
                        Text("runs out in ~\(duration(Int(out))) at this pace")
                    }
                    if let q = g.graphql { Text("GraphQL \(q) left") }
                } else { Text("unknown") }
            }
            meter(codex?.isWeekly == false ? "Codex window" : "Codex week", value: codex.map { $0.used_pct / 100 }, low: false, health: check("codex")?.health) {
                if let c = codex {
                    Text(String(format: "%.0f%% used", c.used_pct))
                    if let r = c.resets_at { Text("resets in \(duration(max(0, r - Int(now))))") }
                    if c.used_pct < 100, let full = Analytics.projection(m.flow, key: "codex", limit: 100, now: now) {
                        Text("100% in ~\(duration(Int(full))) at this pace")
                    }
                    if c.used_pct >= 95 { Text("reviews run on credits") }
                } else { Text("unknown") }
            }
        }
    }




    private func meter<D: View>(_ title: String, value: Double?, low: Bool, health: Health?, @ViewBuilder detail: () -> D) -> some View {
        let v = value ?? 0
        let bad = value != nil && health == .bad
        let warn = value != nil && health == .warn
        let tint: Color = value == nil ? .secondary : bad ? Theme.warning : warn ? Theme.caution : Theme.hud
        return TapCard {
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.caption.weight(.medium))
                Bar(value: v, tint: tint)
            }
        } detail: {
            VStack(alignment: .leading, spacing: 2) { detail() }
        }
    }
}


struct Bar: View {
    let value: Double
    let tint: Color
    var height: CGFloat = 5
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule().fill(tint).frame(width: g.size.width * min(max(value, 0), 1))
            }
        }
        .frame(height: height)
    }
}



struct LaneCard: View {
    let title: String
    let symbol: String
    let host: String
    let check: Check?
    let jobs: [LaneJob]
    let idle: Int?
    var typical: [String] = []

    var body: some View {
        let h = check?.health ?? .unknown
        TapCard {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Image(systemName: symbol).font(.caption).foregroundStyle(.secondary).frame(width: 14)
                    Text(title).font(.caption.weight(.medium))
                    Spacer()
                    Text(summary).font(.caption2).foregroundStyle(jobs.contains { $0.overdue } ? Theme.caution : .secondary)
                        .monospacedDigit().lineLimit(1).truncationMode(.tail)

                    if h != .ok {
                        Image(systemName: healthSymbol(h)).font(.caption).foregroundStyle(color(h))
                            .accessibilityLabel(h == .warn ? "Warning" : h == .bad ? "Problem" : "Unknown")
                    }
                }

                ForEach(jobs) { j in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 5) {
                            if let n = j.pr { Text("#\(n)").font(.caption2.weight(.semibold)).monospacedDigit() }
                            Text(j.name ?? j.title).font(.caption2).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                            Spacer(minLength: 6)
                            Text(j.cardText).font(.caption2).monospacedDigit()
                                .foregroundStyle(j.overdue ? Theme.caution : .secondary)
                        }

                        Bar(value: j.progress ?? 0.03, tint: j.overdue ? Theme.caution : j.repo.map(repoTint) ?? Theme.cyan, height: 4)
                    }
                }
            }
        } detail: {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(jobs) { j in Text("\(j.title) · \(timing(j))") }
                if !typical.isEmpty { Text("usually " + typical.joined(separator: " · ")) }
                if let i = idle { Text("last finished \(duration(i)) ago") }
                Text("\(host) · \(check?.detail ?? "no data")")
            }
        }
        .help("\(title) on \(host): \(check?.detail ?? "no data")")
    }


    private var summary: String {
        if jobs.isEmpty { return idle.map { "idle \(duration($0))" } ?? (check?.detail ?? "no data") }
        if jobs.count > 1 { return "\(jobs.count) running" }
        if jobs.contains(where: { $0.overdue }) { return "running long" }
        return ""
    }


    private func timing(_ j: LaneJob) -> String {
        guard let e = j.elapsed else { return "running" }
        guard let t = j.typical else { return duration(e) }
        if j.overdue { return "\(duration(e)) · usually ~\(duration(t))" }
        return "\(duration(e)) of ~\(duration(t))"
    }
}
