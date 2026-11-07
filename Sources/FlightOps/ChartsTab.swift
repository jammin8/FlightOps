import Charts
import SwiftUI



struct ChartsTab: View {
    @ObservedObject var m: Monitor
    @AppStorage("progressSource") private var progressSource = "linear"

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            rings
            if let launch = m.launch, let f = launch.forecast {
                OpsSection(title: "Forecast") { ForecastCard(f: f, launch: launch, retained: m.world.retainedPlanNote).padding(10).card() }
            }
            OpsSection(title: progressSource == "github" ? "Repositories" : "Initiatives") {
                VStack(alignment: .leading, spacing: 8) {

                    Picker("", selection: $progressSource) {
                        Text("Linear").tag("linear")
                        Text("GitHub").tag("github")
                    }
                    .pickerStyle(.segmented).labelsHidden().controlSize(.small).frame(maxWidth: 160)
                    if progressSource == "github" { repoProgress } else { initiatives }
                }
                .padding(10).card()
            }
            OpsSection(title: "PRs") { prs.padding(10).card() }
            OpsSection(title: "Bottleneck", note: "24 h") { bottleneck.padding(10).card() }
            OpsSection(title: "Phases", note: "median time per PR, 7 d") { phases.padding(10).card() }
            OpsSection(title: "Signals", note: "24 h") { signals.padding(10).card() }
        }
    }



    static let statusColor: [PRStatus: Color] = [.draft: Theme.gunmetal, .fixing: Theme.warning, .waitingCI: Theme.caution.opacity(0.55), .inCI: Theme.caution,
                                                 .review: Theme.cyan, .approved: Theme.hud.opacity(0.6), .merged: Theme.hud]
    private static let stages: [(key: String, label: String, color: Color)] = [
        ("done", "Done", Theme.hud), ("in_review", "Review", Theme.cyan), ("in_progress", "Doing", Theme.caution),
        ("todo", "Todo", Theme.gunmetal.opacity(0.55)), ("backlog", "Backlog", Theme.gunmetal.opacity(0.25))]



    private var rings: some View {


        let launchPct = m.fresh(.linear) ? m.launch?.counts?.pct_done : nil
        let openText = m.fresh(.repos) ? m.repos?.prs.map { String($0.count) } : nil
        let openWhole = m.repos?.openInventoryComplete == true
        return HStack(spacing: 6) {
            VStack(spacing: 4) {
                Gauge(value: Double(launchPct ?? 0), in: 0...100) {
                    Text("Launch")
                } currentValueLabel: {
                    Text(launchPct.map { "\($0)%" } ?? "–").monospacedDigit()
                }
                .gaugeStyle(.accessoryCircularCapacity).tint(Theme.cyan)
                Text("launch").font(.subheadline).foregroundStyle(.secondary)
            }
            .padding(.vertical, 10).frame(maxWidth: .infinity).card()
            number(openText ?? "–", openWhole ? "open PRs" : "observed open PRs")
            number(m.merged24h.map(String.init) ?? "–", "merged 24h")
        }
    }


    private var openShare: Double? {
        m.fresh(.repos) ? Analytics.openShare(m.repos) : nil
    }
    private var mergedShare: Double? { openShare.map { 1 - $0 } }

    private func number(_ value: String, _ label: String) -> some View {
        VStack(spacing: 2) {
            Text(value).font(.largeTitle.weight(.semibold)).monospacedDigit().contentTransition(.numericText())
            Text(label).font(.subheadline).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, minHeight: 80).padding(.vertical, 10).card()
        .accessibilityElement(children: .combine)
    }

    private func ring(value: Double?, big: String?, small: String, tint: Color) -> some View {
        VStack(spacing: 4) {
            ZStack {
                Circle().stroke(Color.primary.opacity(0.1), lineWidth: 7)
                Circle().trim(from: 0, to: min(max(value ?? 0, 0), 1))
                    .stroke(tint, style: StrokeStyle(lineWidth: 7, lineCap: .round)).rotationEffect(.degrees(-90))
                Text(big ?? "–").font(.headline).monospacedDigit()
            }
            .frame(width: 64, height: 64)
            Text(small).font(.caption2).foregroundStyle(.secondary)
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .card()
    }



    @ViewBuilder private var initiatives: some View {
        let list = (m.linear?.initiatives ?? []).filter { $0.scope > 0 }
            .sorted { ($0.number ?? 999, $0.title) < ($1.number ?? 999, $1.title) }
        if list.isEmpty {
            Text("–").foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                if let retained = m.world.retainedPlanNote { StaleNote(text: retained) }
                ForEach(list) { i in
                    StackBar(label: i.title, trailing: "\(i.pct_done)%",
                             parts: Self.stages.map { (Double(i.counts[$0.key] ?? 0), $0.color) }, total: Double(i.scope),
                             detail: Self.stages.map { "\(i.counts[$0.key] ?? 0) \($0.label.lowercased())" }.joined(separator: " · ")
                                 + " · \(i.scope) issues")
                }
                Legend(items: Self.stages.map { ($0.label, $0.color) })
            }
        }
    }





    @ViewBuilder private var repoProgress: some View {
        if !m.fresh(.repos) {
            Text("–").foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(["backend", "ios", "web"], id: \.self) { key in
                    let summary = Analytics.repoCounts(m.repos, repo: key)
                    let n = summary.counts
                    let total = n.compactMap { $0 }.map(Double.init).reduce(0, +)
                    let order: [PRStatus] = [.merged, .approved, .review, .inCI, .waitingCI, .fixing, .draft]
                    VStack(alignment: .leading, spacing: 3) {
                        StackBar(label: repoLabel(key), trailing: summary.mergedShare.map { "\(Int(($0 * 100).rounded()))%" } ?? "–",
                                 parts: order.map { (Double(n[$0.rawValue] ?? 0), Self.statusColor[$0] ?? .gray) }, total: max(total, 1),
                                 detail: order.map { "\(n[$0.rawValue].map(String.init) ?? "unknown") \($0.label.lowercased())" }.joined(separator: " · "))
                        if !summary.note.isEmpty { Text(summary.note).font(.caption).foregroundStyle(.secondary) }
                    }
                }
                Legend(items: [PRStatus.merged, .approved, .review, .inCI, .waitingCI, .fixing, .draft].map { ($0.label, Self.statusColor[$0] ?? .gray) })
                Text("% is the share merged in the last 24 h").font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }



    @ViewBuilder private var prs: some View {
        if !m.fresh(.repos) {
            Text("–").foregroundStyle(.secondary)
        } else {
            let rows = m.repos?.repos ?? []
            let counts: [(String, Analytics.RepoCounts)] = ["backend", "ios", "web"].map { key in
                (key, Analytics.repoCounts(m.repos, repo: key))
            }
            let widest = counts.map { $0.1.counts.compactMap { $0 }.map(Double.init).reduce(0, +) }.max() ?? 1


            VStack(alignment: .leading, spacing: 8) {
                ForEach(counts, id: \.0) { key, summary in
                    let n = summary.counts
                    let main = rows.first { $0.name == key }?.main
                    VStack(alignment: .leading, spacing: 3) {
                    StackBar(label: repoLabel(key), trailing: rows.first { $0.name == key }.map { "\($0.open) " + (m.repos?.openInventoryComplete == true ? "open" : "observed open") } ?? "open unknown",
                             dot: main == "success" ? Theme.hud : main == "failure" ? Theme.warning : main == "pending" ? Theme.caution : Theme.gunmetal,
                             dotHelp: "main: \(main ?? "unknown")",
                             parts: PRStatus.allCases.map { (Double(n[$0.rawValue] ?? 0), Self.statusColor[$0] ?? Theme.gunmetal) },
                             total: max(widest, 1),
                             detail: PRStatus.allCases.map { "\(n[$0.rawValue].map(String.init) ?? "unknown") \($0.label.lowercased())" }
                                 .joined(separator: " · ") + " · main \(main ?? "unknown")")
                    if !summary.note.isEmpty { Text(summary.note).font(.caption).foregroundStyle(.secondary) }
                    }
                }
                Legend(items: PRStatus.allCases.map { ($0.label, Self.statusColor[$0] ?? Theme.gunmetal) })
                Text("The symbol before a repo is its main branch").font(.subheadline).foregroundStyle(.secondary)
            }
        }
    }




    @ViewBuilder private var phases: some View {
        let now = Date().timeIntervalSince1970
        let ph = Analytics.phases(Array(m.timelines.values), since: now - 7 * 86400, now: now)
        if ph.isEmpty {
            Text("history builds from now").font(.caption2).foregroundStyle(.secondary)
        } else {
            let order = FlowSample.waiting
            let total = order.compactMap { ph[$0]?.median }.reduce(0, +)
            VStack(alignment: .leading, spacing: 6) {

                StackBar(label: "Median time per status", trailing: "",
                         parts: order.map { (ph[$0]?.median ?? 0, Self.statusColor[$0] ?? .gray) }, total: max(total, 1),
                         detail: order.compactMap { s in ph[s].map { "\(s.label) \(duration(Int($0.median))) (p85 \(duration(Int($0.p85))))" } }
                             .joined(separator: " · "))
                Legend(items: order.map { ($0.label, Self.statusColor[$0] ?? .gray) })
            }
        }
    }




    private var signals: some View {
        let now = Date().timeIntervalSince1970
        let tl = Array(m.timelines.values)
        let day = now - 86400
        let lanes = m.rigCI?.lanes
        let queues: [(String, Int?, Int?)] = [("Backend fast", lanes?.backend?.fast_queue_s, lanes?.backend?.fast_s),
                                              ("Backend integration", lanes?.backend?.integration_queue_s, lanes?.backend?.integration_s),
                                              ("iOS", m.mac?.iosLane?.queue_s, m.mac?.iosLane?.run_s)].filter { $0.1 != nil || $0.2 != nil }
        return VStack(alignment: .leading, spacing: 8) {
            ForEach(queues, id: \.0) { name, q, r in
                let qs = Double(q ?? 0), rs = Double(r ?? 0)
                StackBar(label: name, trailing: "queue \(q.map { duration($0) } ?? "–") · run \(r.map { duration($0) } ?? "–")",
                         parts: [(qs, Theme.caution), (rs, Theme.cyan)], total: max(qs + rs, 1),
                         detail: "median wait for a runner vs median run time, last 20 jobs")
            }
            if !queues.isEmpty { Legend(items: [("Queue", Theme.caution), ("Run", Theme.cyan)]) }
            ForEach(Analytics.signals(tl, flow: m.flow, since: day, now: now), id: \.name) { row($0) }
        }
    }

    private func row(_ s: Analytics.Signal) -> some View {
        HStack(spacing: 6) {
            Image(systemName: s.state == .warn ? "exclamationmark.triangle.fill" : s.state == .ok ? "checkmark.circle.fill" : s.state == .partial ? "circle.lefthalf.filled" : "questionmark.circle").font(.caption2)
                .foregroundStyle(s.state == .warn ? Theme.caution : s.state == .ok ? Theme.hud : Color.secondary).accessibilityHidden(true)
            Text(s.name).font(.caption)
            Spacer()
            Text(s.value).font(.caption.weight(.medium)).monospacedDigit().lineLimit(1)
        }
        .accessibilityElement(children: .combine)
    }



    private struct FlowPoint: Identifiable {
        let t: Date, status: PRStatus, count: Int, run: Int
        var id: String { "\(t.timeIntervalSince1970)\(status.rawValue)" }
        var series: String { "\(status.label) \(run)" }
    }

    @ViewBuilder private var bottleneck: some View {
        let now = Date().timeIntervalSince1970
        VStack(alignment: .leading, spacing: 8) {
            if let line = Analytics.sentenceParts(Array(m.timelines.values), window: 86400, now: now) {
                HStack(spacing: 6) {
                    StatusPill(label: line.status.label, color: Self.statusColor[line.status] ?? .gray, font: .callout.weight(.semibold))
                    Text(line.rest).font(.callout.weight(.medium))
                }
            }
            HStack(spacing: 6) {
                ForEach([("12h", 12.0), ("24h", 24.0), ("7d", 168.0)], id: \.0) { name, hours in
                    let b = FlowSample.bottleneck(m.flow, since: now - hours * 3600)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(name).font(.caption2).foregroundStyle(.secondary)
                        HStack(spacing: 4) {
                            if let st = b?.status { StatusPill(label: st.label, color: Self.statusColor[st] ?? .gray) }
                            else { Text(FlowSample.covered(m.flow, since: now - hours * 3600, now: now) ? "none" : "–") }
                        }
                        Text(b.map { String(format: "avg %.0f", $0.avg) } ?? " ").font(.caption2).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            let runs = FlowSample.countedRuns(m.flow)
            if runs.joined().count > 1 {
                let points: [FlowPoint] = runs.enumerated().flatMap { run, samples in
                    samples.flatMap { s in
                        FlowSample.waiting.map { FlowPoint(t: Date(timeIntervalSince1970: s.t), status: $0, count: s.count($0), run: run) }
                    }
                }
                Chart(points) { p in
                    AreaMark(x: .value("Time", p.t), y: .value("PRs", p.count))
                        .foregroundStyle(by: .value("Status", p.series))
                }
                .chartForegroundStyleScale(domain: runs.indices.flatMap { run in FlowSample.waiting.map { "\($0.label) \(run)" } },
                                           range: runs.indices.flatMap { _ in FlowSample.waiting.map { Self.statusColor[$0] ?? Theme.gunmetal } })
                .chartLegend(.hidden)
                .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) { _ in AxisValueLabel(format: .dateTime.weekday(.abbreviated).hour()) } }
                .frame(height: 90)
            } else {
                Text("history builds from now").font(.caption2).foregroundStyle(.secondary)
            }
            Legend(items: FlowSample.waiting.map { ($0.label, Self.statusColor[$0] ?? Theme.gunmetal) })
        }
    }
}


struct StackBar: View {
    let label: String
    let trailing: String

    var dot: Color? = nil
    var dotHelp: String? = nil
    let parts: [(Double, Color)]
    let total: Double

    var detail: String? = nil
    @State private var tapped = false
    @Environment(\.sectionOpen) private var sectionOpen
    private var open: Bool { sectionOpen ?? tapped }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if !label.isEmpty || !trailing.isEmpty {
                HStack {
                    if let dot {
                        Image(systemName: dotHelp?.contains("failure") == true ? "xmark.octagon.fill"
                              : dotHelp?.contains("success") == true ? "checkmark.circle.fill" : "clock.fill")
                            .font(.caption2).foregroundStyle(dot).help(dotHelp ?? "").accessibilityLabel(dotHelp ?? "")
                    }
                    Text(label).font(.caption).lineLimit(1)
                    Spacer()
                    Text(trailing).font(.caption.weight(.medium)).monospacedDigit()
                }
            }
            GeometryReader { g in
                HStack(spacing: 1) {
                    ForEach(Array(parts.enumerated()), id: \.offset) { _, p in
                        if p.0 > 0 { Rectangle().fill(p.1).frame(width: max(2, g.size.width * p.0 / max(total, 1) - 1)) }
                    }
                    Spacer(minLength: 0)
                }
                .clipShape(Capsule())
                .background(Color.primary.opacity(0.06), in: Capsule())
            }
            .frame(height: 8)
            if open, let detail {
                Text(detail).font(.caption2).foregroundStyle(.secondary).transition(.opacity)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { if sectionOpen == nil { withAnimation(.easeInOut(duration: 0.18)) { tapped.toggle() } } }
        .help(label)
    }
}


struct Legend: View {
    let items: [(String, Color)]
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 74), spacing: 4, alignment: .leading)], alignment: .leading, spacing: 3) {
            ForEach(items, id: \.0) { name, c in
                HStack(spacing: 3) {
                    Circle().fill(c).frame(width: 6, height: 6)
                    Text(name).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
        }
        .padding(.top, 2)
    }
}
