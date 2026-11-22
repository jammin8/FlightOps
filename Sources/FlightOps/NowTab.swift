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
            StageStrip(stages: view.stages, focus: $focus)
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
            machines
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
                            Text("#\(pr.number)").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
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

    private var machines: some View {
        VStack(alignment: .leading, spacing: 4) {
            CardTitle(text: "Machines")
            Text("Not connected yet. Machine health and agents working arrive in a later update.")
                .font(.caption).foregroundStyle(.secondary)
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
        let tint = verdictTint(stage.verdict)
        return VStack(spacing: 2) {
            Text(stage.count.map(String.init) ?? "–").font(.title2.weight(.semibold).monospacedDigit())
            Text(stage.label).font(.caption2).foregroundStyle(.secondary).lineLimit(2).multilineTextAlignment(.center)
                .frame(height: 26, alignment: .top)
            Text(stage.oldestSince.map { Clock.age($0) } ?? " ").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
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
        return "\(stage.label): \(count)\(oldest). Click to show only these."
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
                    Text("\(repo?.name ?? pr.repo) #\(pr.number) · waiting on \(pr.waitingOn)")
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
