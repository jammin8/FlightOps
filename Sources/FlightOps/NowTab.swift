import Charts
import SwiftUI

struct NowTab: View {
    var status: StatusFile
    var view: StatusView
    @State private var focus: String?
    @State private var showAllNeeds = false

    private var prs: [PullRequest] { view.prs.compactMap { status.pr($0) } }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.gap) {
            if view.state == "unknown" { unknownCard }
            KPIRow(view: view)
            StageStrip(stages: view.stages, focus: $focus)
            ForEach(view.stages.filter { $0.alarming || $0.verdict?.state == "busy" }) { stage in VerdictCard(stage: stage, status: status) }
            if !view.needsYou.isEmpty { needsYou }
            ForEach(view.stages.filter { $0.id != "merged" && (focus == nil || focus == $0.id) }) { stage in
                let inside = prs.filter { $0.stage == stage.id }
                if !inside.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        CardTitle(text: stage.label, detail: "\(inside.count)")
                        ForEach(inside) { pr in PRRow(pr: pr, repo: status.repo(pr.repo)) }
                    }
                    .card()
                }
            }
            if view.state != "unknown" && prs.isEmpty {
                Text("No open pull requests.").foregroundStyle(.secondary).frame(maxWidth: .infinity).card()
            }
        }
    }

    private var unknownCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            CardTitle(text: "Can't read GitHub yet")
            ForEach(status.repos.filter { $0.error != nil }) { r in
                HStack(spacing: 6) {
                    RepoDot(color: r.tint)
                    Text(r.id).font(.callout)
                    Text(r.error ?? "").font(.callout).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Text("FlightOps reads GitHub through the GitHub CLI. If it isn't set up, run gh auth login in Terminal.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .card()
    }

    private var needsYou: some View {
        let ids = showAllNeeds ? view.needsYou : Array(view.needsYou.prefix(6))
        return VStack(alignment: .leading, spacing: 8) {
            CardTitle(text: "Needs you", detail: "\(view.needsYou.count)")
            ForEach(ids, id: \.self) { id in
                if let pr = status.pr(id) {
                    Button { openLink(pr.url) } label: {
                        HStack(spacing: 8) {
                            RepoDot(color: status.repo(pr.repo)?.tint ?? .secondary)
                            Text(verbatim: "#\(pr.number)").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                            Text(pr.title).font(.callout).lineLimit(1)
                            Spacer(minLength: 8)
                            Text(pr.needsYou ?? "").font(.caption).foregroundStyle(Theme.caution).lineLimit(1)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            if view.needsYou.count > 6 {
                Button(showAllNeeds ? "Show fewer" : "Show all \(view.needsYou.count)") { showAllNeeds.toggle() }
                    .buttonStyle(.link).font(.caption)
            }
        }
        .card()
    }
}

struct StageStrip: View {
    var stages: [Stage]
    @Binding var focus: String?

    var body: some View {
        HStack(spacing: 6) {
            ForEach(stages) { stage in
                Button {
                    guard stage.id != "merged" else { return }
                    focus = focus == stage.id ? nil : stage.id
                } label: { tile(stage) }
                .buttonStyle(.plain)
                .help(help(stage))
            }
        }
    }

    private func tile(_ stage: Stage) -> some View {
        let selected = focus == stage.id
        let tint = verdictTint(stage.verdict?.state)
        return VStack(spacing: 2) {
            Text(stage.count.map(String.init) ?? "–").font(.title2.weight(.semibold).monospacedDigit())
            Text(stage.label).font(.caption2).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.center)
                .frame(height: 26, alignment: .top)
            if stage.alarming || stage.verdict?.state == "busy", let verdict = stage.verdict {
                Text(verdict.state == "busy" ? "Busy" : verdict.word).font(.caption2.weight(.semibold)).foregroundStyle(tint ?? .secondary)
            } else {
                Text(stage.oldestSince.map { Clock.age($0) } ?? " ").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity).padding(.vertical, 8)
        .background((tint ?? Color.secondary).opacity(selected ? 0.22 : tint == nil ? 0.07 : 0.15),
                    in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(selected ? Color.accentColor : .clear, lineWidth: 1))
        .contentShape(Rectangle())
    }

    private func help(_ stage: Stage) -> String {
        guard let count = stage.count else { return "\(stage.label): unknown" }
        if stage.id == "merged" { return "\(count) merged since midnight" }
        let oldest = stage.oldestSince.map { ", oldest waiting \(Clock.age($0))" } ?? ""
        let verdict = stage.verdict.map { "\n\($0.word): \($0.reason)." } ?? ""
        return "\(stage.label): \(count)\(oldest).\(verdict)\nClick to show only these."
    }
}

struct PRRow: View {
    var pr: PullRequest
    var repo: RepoInfo?

    var body: some View {
        Button { openLink(pr.url) } label: {
            HStack(alignment: .top, spacing: 8) {
                RoundedRectangle(cornerRadius: 1.5).fill(repo?.tint ?? .secondary).frame(width: 3, height: 32)
                VStack(alignment: .leading, spacing: 2) {
                    Text(pr.title).font(.callout).lineLimit(1)
                    Text(verbatim: "\(repo?.name ?? pr.repo) #\(pr.number) · waiting on \(pr.waitingOn)")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 8)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(Clock.age(pr.stageSince)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    ciMark
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("\(pr.title)\nby \(pr.author ?? "unknown") · \(Clock.age(pr.stageSince)) in this stage")
    }

    @ViewBuilder private var ciMark: some View {
        switch pr.ci {
        case "failing": Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.warning).font(.caption)
        case "running": Image(systemName: "circle.dotted").foregroundStyle(.secondary).font(.caption)
        case "queued": Image(systemName: "hourglass").foregroundStyle(.secondary).font(.caption)
        case "passed": Image(systemName: "checkmark.circle").foregroundStyle(.secondary).font(.caption)
        default: EmptyView()
        }
    }
}

struct VerdictCard: View {
    var stage: Stage
    var status: StatusFile

    private func headline(_ verdict: Verdict) -> String {
        let age = Clock.age(verdict.since)
        let lasting = verdict.since == nil || age == "now" || age == "–" ? "" : " for \(age)"
        return "\(stage.label): \(verdict.word)\(lasting)"
    }

    var body: some View {
        let verdict = stage.verdict!
        let tint = verdictTint(verdict.state) ?? .secondary
        let pr = verdict.oldestPr.flatMap { status.pr($0) }
        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: verdict.state == "busy" ? "water.waves" : verdict.state == "stuck" ? "exclamationmark.octagon.fill" : "tortoise.fill")
                .foregroundStyle(tint).font(.title3)
            VStack(alignment: .leading, spacing: 3) {
                Text(headline(verdict))
                    .font(.subheadline.weight(.semibold))
                Text(verdict.reason).font(.callout).foregroundStyle(.secondary)
                if let pr, verdict.state != "busy" {
                    Button { openLink(pr.url) } label: {
                        Text(verbatim: "Oldest: \(status.repo(pr.repo)?.name ?? pr.repo) #\(pr.number) \(pr.title)").font(.caption).lineLimit(1)
                    }
                    .buttonStyle(.link)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
    }
}

struct KPIRow: View {
    var view: StatusView

    var body: some View {
        let t = view.trends
        let last = max(0, t.days.count - 14)..<t.days.count
        HStack(spacing: 8) {
            tile("Open", view.counts.open, t.open[last].map(Double.init), .accentColor, "Open pull requests, last 14 days")
            tile("Merged today", view.counts.mergedToday, t.merged[last].map(Double.init), .green, "Merged per day, last 14 days")
            tile("Needs you", view.counts.needsYou, nil, (view.counts.needsYou ?? 0) > 0 ? Theme.caution : .secondary, "Pull requests waiting on you")
        }
    }

    private func tile(_ label: String, _ value: Int?, _ points: [Double]?, _ tint: Color, _ help: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .bottom, spacing: 8) {
                Text(value.map(String.init) ?? "–").font(.title.weight(.semibold).monospacedDigit())
                Spacer(minLength: 0)
                if let points, points.count > 1 { Sparkline(values: points, tint: tint).frame(width: 64, height: 24) }
            }
        }
        .card()
        .help(help)
    }
}

struct Sparkline: View {
    var values: [Double]
    var tint: Color

    var body: some View {
        Chart(Array(values.enumerated()), id: \.offset) { i, v in
            AreaMark(x: .value("Day", i), y: .value("Value", v)).foregroundStyle(tint.opacity(0.18)).interpolationMethod(.monotone)
            LineMark(x: .value("Day", i), y: .value("Value", v)).foregroundStyle(tint).interpolationMethod(.monotone)
        }
        .chartXAxis(.hidden).chartYAxis(.hidden).chartLegend(.hidden)
        .chartYScale(domain: 0...max(1, values.max() ?? 1))
    }
}
