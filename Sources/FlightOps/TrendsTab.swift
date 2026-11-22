import Charts
import SwiftUI

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
            flow
            wait
            openWork
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
            .frame(height: 110)
        }
        .card()
    }
}

struct ModelsTab: View {
    var status: StatusFile

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            CardTitle(text: "Agent usage")
            Text("Not connected yet. A later update reads the usage logs Claude Code and Codex already write on your machines, and shows tokens, cost at list price and sessions by model, repo and day.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .card()
    }
}
