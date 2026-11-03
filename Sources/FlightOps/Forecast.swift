import Charts
import SwiftUI




struct Outlook {
    let word: String
    let tint: Color?
    let chance: String?
    let finish: String?

    static func of(_ f: Forecast?, target: String?, retained: Bool = false) -> Outlook {
        guard let f else { return Outlook(word: "no forecast", tint: nil, chance: nil, finish: nil) }
        if retained { return Outlook(word: "out of date", tint: nil, chance: nil, finish: nil) }

        if f.scope == 0 { return Outlook(word: "no scope", tint: nil, chance: nil, finish: "the launch projects have no issues") }
        if f.remaining == 0 { return Outlook(word: "done", tint: nil, chance: nil, finish: nil) }
        if f.insufficient { return Outlook(word: "pace unknown", tint: nil, chance: nil, finish: "not enough history to forecast") }
        let label = shortDate(target) ?? "launch"
        let chance: String? = f.prob_by_target.map { "\(chanceText($0)) by \(label)" }
        let finish = finishText(f)
        guard let p = f.prob_by_target else {

            return Outlook(word: f.prob_note == nil ? "no target" : "target beyond horizon", tint: nil, chance: nil, finish: finish)
        }
        if p >= 85 { return Outlook(word: "on track", tint: nil, chance: chance, finish: finish) }
        if p >= 50 { return Outlook(word: "tight", tint: Theme.caution, chance: chance, finish: finish) }
        return Outlook(word: "at risk", tint: Theme.warning, chance: chance, finish: finish)
    }

    static func finishText(_ f: Forecast) -> String {
        let horizon = Calendar.current.date(byAdding: .day, value: f.horizon_days ?? 730, to: Date()) ?? Date()
        guard let p50 = shortDate(f.p50) else { return "beyond \(monthAndYear(horizon)) at this pace" }
        guard let p85 = shortDate(f.p85) else { return "likely \(p50) · 85% beyond \(monthAndYear(horizon))" }
        return "likely \(p50) · 85% by \(p85)"
    }
}


func daysText(_ days: Int) -> String {
    if days == 0 { return "· today" }
    if days < 0 { return "· \(-days) days ago" }
    return days == 1 ? "· 1 day" : "· \(days) days"
}


func driftText(_ d: Drift) -> String? {
    var parts: [String] = []
    if let p = d.p50_days, p != 0 { parts.append("finish \(p > 0 ? "+" : "")\(p) d") }
    if let s = d.scope_change, s != 0 { parts.append("scope \(s > 0 ? "+" : "")\(s)") }
    if let n = d.done_change { parts.append("\(n) done") }
    guard !parts.isEmpty else { return nil }
    return "Since \(shortDate(d.since) ?? d.since): " + parts.joined(separator: " · ")
}



struct LaunchCard: View {
    @ObservedObject var m: Monitor
    @Binding var tab: Int

    var body: some View {
        OpsSection(title: "Launch", note: note, key: "launch", expandable: true) { content }
    }

    private var note: String? {
        guard let l = m.launch, l.error == nil else { return nil }
        return l.projectIds.count == 1 ? "1 project" : "\(l.projectIds.count) projects"
    }

    @ViewBuilder private var content: some View {
        if let linear = m.linear, linear.ok {
            if let launch = linear.launch, launch.error == nil {
                card(launch)
            } else {
                Line(title: "Launch scope unavailable", dot: .warn, action: { tab = Tab.charts.rawValue },
                     help: linear.launch?.error ?? "The Linear collector reported no launch", divider: false)
                    .padding(.vertical, 4).card()
            }
        } else if m.linear?.error == "no_key" {
            Line(title: "Connect Linear to see the launch", dot: .warn, action: { tab = Tab.charts.rawValue }, divider: false)
                .padding(.vertical, 4).card()
        } else {
            Line(title: "Launch data unavailable", dot: .unknown, help: m.status[.linear]?.error ?? "Waiting for Linear", divider: false)
                .padding(.vertical, 4).card()
        }
    }

    private func card(_ launch: LaunchInfo) -> some View {
        let f = launch.forecast
        let retained = m.world.retainedPlanNote
        let outlook = Outlook.of(f, target: launch.target, retained: retained != nil)
        let target = targetText(launch.target)
        let projects: [PlanProject] = launch.projectIds.compactMap { m.linear?.project($0) }
        return ExpandCard(id: "home-launch") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(target?.0 ?? "No target").font(.title2.weight(.semibold))
                    if let t = target { Text(daysText(t.1)).foregroundStyle(.secondary) }
                    Spacer()
                    Text(outlook.word).font(.callout.weight(.medium)).foregroundStyle(outlook.tint ?? Color.secondary)
                }
                if let counts = launch.counts { StackedBar(counts: counts.dict, height: 8) }
                HStack(alignment: .center, spacing: 10) {
                    summary(launch, outlook)
                    Spacer(minLength: 4)
                    if let f, f.burnup.count > 1 {
                        BurnupSpark(f: f).frame(width: 84, height: 34)
                    }
                }
                if let retained { StaleNote(text: retained) }
            }
        } detail: {
            ForEach(projects) { p in
                BarRow(title: p.name, pct: p.pct_done, counts: p.counts, url: p.link, target: p.target_date, divider: true)
            }
            Line(title: "Open the forecast", action: { tab = Tab.charts.rawValue }, divider: false)
        }
    }

    private func summary(_ launch: LaunchInfo, _ outlook: Outlook) -> some View {
        let pct = launch.counts.map { "\($0.pct_done)% done" } ?? "progress unknown"
        let first = outlook.chance.map { pct + " · " + $0 } ?? pct
        return VStack(alignment: .leading, spacing: 2) {
            Text(first).font(.callout).lineLimit(1)
            if let finish = outlook.finish {
                Text(finish).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            if let d = launch.drift, let text = driftText(d) {
                Text(text).font(.caption).lineLimit(1)
                    .foregroundStyle((d.p50_days ?? 0) > 0 ? Theme.caution : Color.secondary)
            }
        }
    }
}


struct BurnupSpark: View {
    let f: Forecast
    var body: some View {
        Chart {
            ForEach(f.burnup) { p in
                AreaMark(x: .value("Week", p.date), y: .value("Done", Double(p.done)))
                    .foregroundStyle(Color.accentColor.opacity(0.18))
                LineMark(x: .value("Week", p.date), y: .value("Done", Double(p.done)), series: .value("Series", "done"))
                    .foregroundStyle(Color.accentColor)
                    .lineStyle(StrokeStyle(lineWidth: 1.5))
                LineMark(x: .value("Week", p.date), y: .value("Done", Double(p.scope)), series: .value("Series", "scope"))
                    .foregroundStyle(Color.secondary)
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [2, 2]))
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartLegend(.hidden)
        .accessibilityLabel("Burn-up: \(f.done) of \(f.scope) done")
    }
}



struct ForecastCard: View {
    let f: Forecast
    let launch: LaunchInfo
    var retained: String? = nil

    private var target: Date? { ymdDate(launch.target) }
    private var lastPoint: Forecast.Point? { f.burnup.last }
    private var p50: Date? { ymdDate(f.p50) }
    private var p85: Date? { ymdDate(f.p85) }

    private func addDays(_ n: Int, _ d: Date) -> Date { Calendar.current.date(byAdding: .day, value: n, to: d) ?? d }



    private var end: Date {
        let base = max(target ?? Date(), lastPoint?.date ?? Date())
        var e = addDays(21, base)
        if let p85, p85 > e, p85 <= addDays(60, e) { e = addDays(7, p85) }
        return e
    }

    private var start: Date { min(f.burnup.first?.date ?? Date(), end) }


    private func projection(_ end: Date) -> (Date, Double)? {
        guard let last = lastPoint, let p50 else { return nil }
        if p50 <= end { return (p50, Double(f.scope)) }
        let span = p50.timeIntervalSince(last.date)
        guard span > 0 else { return nil }
        let frac = end.timeIntervalSince(last.date) / span
        return (end, Double(last.done) + (Double(f.scope) - Double(last.done)) * frac)
    }

    private func band(_ end: Date) -> (Date, Date)? {
        guard let p50, p50 <= end else { return nil }
        return (p50, min(p85 ?? end, end))
    }

    var body: some View {
        let outlook = Outlook.of(f, target: launch.target, retained: retained != nil)
        VStack(alignment: .leading, spacing: 8) {
            if let retained { StaleNote(text: retained) }
            Text(outlook.chance ?? outlook.word.capitalized)
                .font(.title2.weight(.semibold)).monospacedDigit()
                .foregroundStyle(outlook.tint ?? Color.primary)
            if let finish = outlook.finish {
                let capitalized: String = finish.prefix(1).uppercased() + String(finish.dropFirst())
                Text(capitalized).foregroundStyle(.secondary).lineLimit(1)
            }
            if f.burnup.count > 1 { chart }
            stats
            assumptions
            if let d = launch.drift, let text = driftText(d) {
                Label(text, systemImage: "arrow.left.arrow.right").font(.caption)
                    .foregroundStyle((d.p50_days ?? 0) > 0 ? Theme.caution : Color.secondary)
            }
        }
        .padding(12)
        .card()
    }

    private var chart: some View {
        let endDate = end
        let lo = start
        let proj = projection(endDate)
        let range = band(endDate)
        let top = Double(max(f.scope, 1)) * 1.08
        let finishOffChart = p50.map { $0 > endDate } ?? false
        return Chart {
            ForEach(f.burnup) { p in
                AreaMark(x: .value("Date", p.date), y: .value("Issues", Double(p.done)))
                    .foregroundStyle(Color.accentColor.opacity(0.15))
                LineMark(x: .value("Date", p.date), y: .value("Issues", Double(p.done)), series: .value("Series", "Done"))
                    .foregroundStyle(Color.accentColor)
                    .lineStyle(StrokeStyle(lineWidth: 2))
                LineMark(x: .value("Date", p.date), y: .value("Issues", Double(p.scope)), series: .value("Series", "Scope"))
                    .foregroundStyle(Color.secondary)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [3, 3]))
            }
            if let range {
                RectangleMark(xStart: .value("From", range.0), xEnd: .value("To", range.1),
                              yStart: .value("Low", 0.0), yEnd: .value("High", Double(f.scope)))
                    .foregroundStyle(Color.accentColor.opacity(0.10))
            }
            if let last = lastPoint, let proj {
                LineMark(x: .value("Date", last.date), y: .value("Issues", Double(last.done)), series: .value("Series", "Projection"))
                    .foregroundStyle(Color.accentColor.opacity(0.6))
                    .lineStyle(StrokeStyle(lineWidth: 2, dash: [4, 3]))
                LineMark(x: .value("Date", proj.0), y: .value("Issues", proj.1), series: .value("Series", "Projection"))
                    .foregroundStyle(Color.accentColor.opacity(0.6))
                    .lineStyle(StrokeStyle(lineWidth: 2, dash: [4, 3]))
                    .annotation(position: .leading, alignment: .center, spacing: 2) {
                        if finishOffChart {
                            Text("→ \(shortDate(f.p50) ?? "")").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
            }
            if let target {
                RuleMark(x: .value("Launch", target))
                    .foregroundStyle(Theme.caution)
                    .lineStyle(StrokeStyle(lineWidth: 1))
                    .annotation(position: .trailing, alignment: .top, spacing: 3) {
                        Text("Launch").font(.caption2).foregroundStyle(Theme.caution)
                    }
            }
        }
        .chartXScale(domain: lo...endDate)
        .chartYScale(domain: 0...top)
        .chartLegend(.hidden)
        .chartXAxis {
            AxisMarks(values: .stride(by: .month)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                AxisValueLabel(format: .dateTime.month(.abbreviated))
            }
        }
        .chartYAxis {
            AxisMarks(position: .trailing) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                AxisValueLabel()
            }
        }
        .frame(height: 130)
        .accessibilityLabel("Burn-up: \(f.done) of \(f.scope) done. \(Outlook.finishText(f))")
    }

    private var stats: some View {
        let visibleBand = band(end) != nil
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 14) {
                stat("\(f.remaining)", "to go")
                stat(String(format: "%.0f", f.per_week), "done / week")
                if visibleBand {
                    HStack(spacing: 4) {
                        RoundedRectangle(cornerRadius: 3, style: .continuous).fill(Color.accentColor.opacity(0.25)).frame(width: 10, height: 8)
                        Text("50–85% finish").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Spacer()
            }

            if !visibleBand, let range = offChartRange {
                Label(range, systemImage: "arrow.right.to.line").font(.caption).foregroundStyle(.secondary)
            }
        }
    }


    private var offChartRange: String? {
        guard let a = shortDate(f.p50) else { return nil }
        let b = shortDate(f.p85) ?? "beyond the horizon"
        return "50–85% finish: \(a) – \(b) (past the chart)"
    }

    private func stat(_ v: String, _ label: String) -> some View {
        HStack(spacing: 4) {
            Text(v).font(.callout.weight(.semibold)).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
    }


    private var assumptions: some View {
        let days = f.sample_days ?? 28
        let active = f.active_days.map { " (\($0) active)" } ?? ""
        var short = "Pace of the last \(days) days\(active) · scope fixed at today's \(f.scope) · burn-up rebuilt from today's issues"
        if let undated = f.undated_done, undated > 0 { short += " · \(undated) undated left out" }
        let full = "Simulated \(f.runs ?? 2000) times from the daily completions of the last \(days) days. Assumes no new scope. "
            + "The burn-up is rebuilt from today's issues, so past removals and reopenings don't show. "
            + "Done issues without a completion date count as done but not toward the pace."
        return Label(short, systemImage: "info.circle")
            .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            .help(full)
    }
}
