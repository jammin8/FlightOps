import Charts
import SwiftUI

private struct HealthCell: Identifiable {
    var id: String { "\(stage)-\(day)" }
    var day: Date
    var stage: String
    var state: String

    var word: String {
        switch state {
        case "busy": return "Busy, flowing"
        case "slowing": return "Slowing"
        case "stuck": return "Stuck"
        default: return "Clear"
        }
    }
}

private struct RepoTotal: Identifiable {
    var id: String { name }
    var name: String
    var tint: Color
    var merged: Int
    var opened: Int
}

private struct DayPoint: Identifiable {
    var id: String { "\(series)-\(day)" }
    var day: Date
    var series: String
    var value: Double
}

struct TrendsTab: View {
    var status: StatusFile
    var view: StatusView
    var repo: String
    @AppStorage("range") private var range = 30

    private static let dayParser: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.timeZone = TimeZone(identifier: "UTC")
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    private var window: Range<Int> {
        let count = view.trends.days.count
        return max(0, count - range)..<count
    }

    private func day(_ i: Int) -> Date { TrendsTab.dayParser.date(from: view.trends.days[i]) ?? Date() }

    private var tint: Color { repo == "all" ? .accentColor : (status.repo(repo)?.tint ?? .accentColor) }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            HStack {
                Picker("", selection: $range) {
                    Text("7 days").tag(7)
                    Text("30 days").tag(30)
                    Text("90 days").tag(90)
                }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
                Spacer()
                if !view.history.complete {
                    Text("Filling in history" + (view.history.oldest.map { " · back to \($0)" } ?? ""))
                        .font(.caption).foregroundStyle(.secondary)
                        .help("FlightOps reads 90 days of history from GitHub a little at a time.")
                }
            }
            health
            flow
            mergeTime
            wait
            openWork
            if repo == "all" && status.repos.count > 1 { byRepo }
            log
        }
    }

    private var flow: some View {
        let points = window.flatMap { i in
            [DayPoint(day: day(i), series: "Opened", value: Double(view.trends.opened[i])),
             DayPoint(day: day(i), series: "Merged", value: Double(view.trends.merged[i]))]
        }
        let opened = window.reduce(0) { $0 + view.trends.opened[$1] }
        let merged = window.reduce(0) { $0 + view.trends.merged[$1] }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Pull requests per day", detail: "\(opened) opened · \(merged) merged")
            Chart(points) { p in
                BarMark(x: .value("Day", p.day, unit: .day), y: .value("PRs", p.value))
                    .foregroundStyle(by: .value("Series", p.series))
                    .position(by: .value("Series", p.series))
            }
            .chartForegroundStyleScale(["Opened": tint.opacity(0.45), "Merged": tint])
            .chartLegend(position: .top, alignment: .leading)
            .chartXScale(domain: span)
            .frame(height: 140)
        }
        .card()
    }

    private var span: ClosedRange<Date> {
        let first = day(window.first ?? 0)
        let last = Calendar.current.date(byAdding: .day, value: 1, to: day(window.last ?? 0)) ?? first
        return first...last
    }

    private var wait: some View {
        let points = window.compactMap { i in view.trends.ciWaitP90Min[i].map { DayPoint(day: day(i), series: "Wait", value: $0) } }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "CI queue wait, slowest tenth", detail: points.isEmpty ? nil : "minutes")
                .help("How long the slowest tenth of CI jobs waited for a runner each day. Days with fewer than 5 jobs are left out.")
            if points.isEmpty {
                Text("Not enough CI jobs read yet.").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 60)
            } else {
                Chart(points) { p in
                    LineMark(x: .value("Day", p.day, unit: .day), y: .value("Minutes", p.value))
                        .interpolationMethod(.monotone).foregroundStyle(tint)
                    PointMark(x: .value("Day", p.day, unit: .day), y: .value("Minutes", p.value))
                        .symbolSize(14).foregroundStyle(tint)
                }
                .chartXScale(domain: span)
                .frame(height: 120)
            }
        }
        .card()
    }

    private var health: some View {
        let labels = Dictionary(view.stages.map { ($0.id, $0.label) }, uniquingKeysWith: { a, _ in a })
        let order = ["waiting_ci", "in_ci", "in_review"]
        let cells: [HealthCell] = order.flatMap { stage in
            window.compactMap { i -> HealthCell? in
                guard let state = view.trends.verdicts?[stage]?[i] ?? nil, state != "learning" else { return nil }
                return HealthCell(day: day(i), stage: labels[stage] ?? stage, state: state)
            }
        }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Stage health", detail: "worst each day")
                .help("Each stage against its own last 28 days. Blue is a wave that kept flowing, amber is slowing, red is stuck.")
            if cells.isEmpty {
                Text("Learning what normal looks like.").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 50)
            } else {
                Chart(cells) { c in
                    RectangleMark(x: .value("Day", c.day, unit: .day), y: .value("Stage", c.stage), width: .ratio(0.85), height: .ratio(0.7))
                        .foregroundStyle(by: .value("Verdict", c.word))
                        .cornerRadius(2)
                }
                .chartForegroundStyleScale(["Clear": Color.secondary.opacity(0.18), "Busy, flowing": Theme.calm, "Slowing": Theme.caution, "Stuck": Theme.warning])
                .chartYScale(domain: order.map { labels[$0] ?? $0 })
                .chartYAxis { AxisMarks(position: .leading) { AxisValueLabel().font(.caption2) } }
                .chartXScale(domain: span)
                .chartLegend(position: .top, alignment: .leading)
                .frame(height: 110)
            }
        }
        .card()
    }

    private var mergeTime: some View {
        let p50 = view.trends.mergeHoursP50 ?? []
        let p90 = view.trends.mergeHoursP90 ?? []
        let points = window.flatMap { i -> [DayPoint] in
            var found: [DayPoint] = []
            if i < p50.count, let v = p50[i] { found.append(DayPoint(day: day(i), series: "Typical", value: v)) }
            if i < p90.count, let v = p90[i] { found.append(DayPoint(day: day(i), series: "Slowest tenth", value: v)) }
            return found
        }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Time to merge", detail: points.isEmpty ? nil : "hours, open to merged")
                .help("For pull requests merged each day: the typical time from opening to merge, and the slowest tenth. Days with few merges are left out.")
            if points.isEmpty {
                Text("Not enough merges read yet.").font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 60)
            } else {
                Chart(points) { p in
                    LineMark(x: .value("Day", p.day, unit: .day), y: .value("Hours", p.value))
                        .foregroundStyle(by: .value("Series", p.series))
                        .interpolationMethod(.monotone)
                    PointMark(x: .value("Day", p.day, unit: .day), y: .value("Hours", p.value))
                        .foregroundStyle(by: .value("Series", p.series)).symbolSize(12)
                }
                .chartForegroundStyleScale(["Typical": tint, "Slowest tenth": tint.opacity(0.4)])
                .chartLegend(position: .top, alignment: .leading)
                .chartXScale(domain: span)
                .frame(height: 120)
            }
        }
        .card()
    }

    private var byRepo: some View {
        let rows = status.repos.map { r -> RepoTotal in
            let t = status.views[r.id]?.trends
            let lo = max(0, (t?.days.count ?? 0) - range)
            let merged = t.map { Array($0.merged[lo...]).reduce(0, +) } ?? 0
            let opened = t.map { Array($0.opened[lo...]).reduce(0, +) } ?? 0
            return RepoTotal(name: r.name, tint: r.tint, merged: merged, opened: opened)
        }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "By repo", detail: "merged in \(range) days")
            Chart(rows) { r in
                BarMark(x: .value("Merged", r.merged), y: .value("Repo", r.name))
                    .foregroundStyle(r.tint)
                    .cornerRadius(3)
                    .annotation(position: .trailing) {
                        Text("\(r.merged) merged · \(r.opened) opened").font(.caption2).foregroundStyle(.secondary)
                    }
            }
            .chartXAxis(.hidden)
            .chartXScale(domain: 0...max(1, Double(rows.map(\.merged).max() ?? 1) * 1.6))
            .frame(height: CGFloat(rows.count) * 26 + 4)
        }
        .card()
    }

    private var log: some View {
        let from = window.isEmpty ? Date.distantPast : day(window.first!)
        let inside = (view.bottlenecks ?? []).filter { (Clock.date($0.start) ?? .distantPast) >= from }.sorted { $0.hoursLost > $1.hoursLost }
        let label = Dictionary(view.stages.map { ($0.id, $0.label) }, uniquingKeysWith: { a, _ in a })
        let total = inside.reduce(0) { $0 + $1.hoursLost }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Bottlenecks", detail: inside.isEmpty ? nil : "\(inside.count) · \(hours(total)) of extra waiting")
                .help("Each time a stage was Slowing or Stuck, ranked by waiting beyond that stage's normal. Stages that keep coming back are worth fixing.")
            if inside.isEmpty {
                Text(view.history.complete ? "No slowdowns in this range." : "No slowdowns found yet. History is still filling in.")
                    .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 40)
            } else {
                ForEach(inside.prefix(6)) { e in
                    HStack(spacing: 8) {
                        Circle().fill(verdictTint(e.state) ?? .secondary).frame(width: 7, height: 7)
                        Text(label[e.stage] ?? e.stage).font(.callout)
                        Text(when(e)).font(.caption).foregroundStyle(.secondary)
                        Spacer(minLength: 8)
                        Text(hours(e.hoursLost)).font(.callout.monospacedDigit())
                    }
                }
            }
        }
        .card()
    }

    private func hours(_ h: Double) -> String { h >= 10 ? "\(Int(h.rounded())) h" : String(format: "%.1f h", h) }

    private func when(_ e: Episode) -> String {
        guard let start = Clock.date(e.start) else { return "" }
        let f = DateFormatter()
        f.setLocalizedDateFormatFromTemplate("MMM d")
        let length = e.end.flatMap { Clock.date($0) }.map { Clock.span($0.timeIntervalSince(start)) } ?? "ongoing"
        return "\(f.string(from: start)) · \(length == "now" ? "under 1m" : length)"
    }

    private var openWork: some View {
        let points = window.map { DayPoint(day: day($0), series: "Open", value: Double(view.trends.open[$0])) }
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Open pull requests", detail: "at the end of each day")
            Chart(points) { p in
                AreaMark(x: .value("Day", p.day, unit: .day), y: .value("Open", p.value))
                    .interpolationMethod(.monotone).foregroundStyle(tint.opacity(0.25))
                LineMark(x: .value("Day", p.day, unit: .day), y: .value("Open", p.value))
                    .interpolationMethod(.monotone).foregroundStyle(tint)
            }
            .chartXScale(domain: span)
            .frame(height: 110)
        }
        .card()
    }
}
