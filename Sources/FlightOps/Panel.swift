import AppKit
import SwiftUI

enum Tab: Int, CaseIterable {
    case now, trends

    var title: String {
        switch self {
        case .now: return "Now"
        case .trends: return "Trends"
        }
    }
}

struct Panel: View {
    @ObservedObject var m: Monitor
    @AppStorage("tab") private var tab = Tab.now.rawValue
    @AppStorage("repo") private var repo = "all"
    @State private var choosing = false

    private var selectedRepo: String { m.status?.views[repo] != nil ? repo : "all" }

    var body: some View {
        VStack(spacing: 0) {
            header.padding(.horizontal, Theme.margin).padding(.top, 14).padding(.bottom, 10)
            if choosing {
                RepoChooser(m: m, done: { choosing = false })
                    .frame(maxHeight: .infinity, alignment: .top)
            } else {
                controls.padding(.horizontal, Theme.margin).padding(.bottom, 10)
                if m.demo { demoBanner.padding(.horizontal, Theme.margin).padding(.bottom, 10) }
                ScrollView(.vertical) {
                    page
                        .frame(width: Theme.width - 2 * Theme.margin, alignment: .topLeading)
                        .padding(.horizontal, Theme.margin).padding(.bottom, 12)
                }
                .frame(maxHeight: .infinity, alignment: .top)
            }
            Divider()
            footer.padding(.horizontal, Theme.margin).padding(.vertical, 8)
        }
        .frame(width: Theme.width, height: min(760, (NSScreen.main?.visibleFrame.height ?? 900) - 40), alignment: .top)
        .background { shortcuts.opacity(0).allowsHitTesting(false) }
    }

    @ViewBuilder private var page: some View {
        if let status = m.status, let view = status.view(selectedRepo) {
            switch Tab(rawValue: tab) ?? .now {
            case .now: NowTab(status: status, view: view)
            case .trends: TrendsTab(status: status, view: view, repo: selectedRepo)
            }
        } else {
            Text(m.problem ?? "No status yet.").foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 200)
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(nsImage: JetIcon.image).resizable().frame(width: 16, height: 16).foregroundStyle(.secondary)
            Text("FlightOps").font(.headline)
            Spacer(minLength: 12)
            if let line = m.status?.view(selectedRepo)?.statusLine {
                Text(line).font(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail).help(line)
            }
        }
    }

    private var controls: some View {
        HStack(spacing: 10) {
            Picker("", selection: $repo) {
                Text("All repos").tag("all")
                if let repos = m.status?.repos, !repos.isEmpty {
                    Divider()
                    ForEach(repos) { r in
                        Label { Text(r.name) } icon: { Image(systemName: "circle.fill").foregroundStyle(r.tint) }.tag(r.id)
                    }
                }
            }
            .labelsHidden().fixedSize().help("Filter every tab by repo")
            Picker("", selection: $tab) {
                ForEach(Tab.allCases, id: \.rawValue) { Text($0.title).tag($0.rawValue) }
            }
            .pickerStyle(.segmented).labelsHidden()
        }
    }

    private var demoBanner: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkles").foregroundStyle(.secondary)
            Text("Sample data. Connect your repos to see your own work.").font(.callout)
            Spacer()
            Button("Connect your repos") { choosing = true }.buttonStyle(.borderedProminent).controlSize(.small)
        }
        .card()
    }

    private var shortcuts: some View {
        HStack {
            ForEach(Array(Tab.allCases.enumerated()), id: \.offset) { i, t in
                Button("") { tab = t.rawValue }.keyboardShortcut(KeyEquivalent(Character(String(i + 1))))
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if m.collecting {
                ProgressView().controlSize(.small)
                Text("Reading GitHub…").font(.caption).foregroundStyle(.secondary)
            } else if let problem = m.problem {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.caution).font(.caption)
                Text(problem).font(.caption).foregroundStyle(.secondary).lineLimit(1).help(problem)
            } else {
                Text(updatedText).font(.caption).foregroundStyle(.secondary).help(sourceHelp)
            }
            Spacer()
            Button { Task { await m.collect() } } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).help("Refresh now (⌘R)").keyboardShortcut("r").disabled(m.demo || m.collecting)
            Menu {
                Button("Choose Repos…") { choosing = true }
                Button("Open GitHub") { openLink("https://github.com/pulls") }
                Divider()
                Button("Quit FlightOps") { NSApplication.shared.terminate(nil) }.keyboardShortcut("q")
            } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().help("More")
        }
    }

    private var updatedText: String {
        guard let status = m.status else { return "" }
        if m.demo { return "Sample data" }
        let github = status.sources?["github"]
        guard let read = github?.readAt else { return github?.error ?? "Not read yet" }
        let age = Clock.age(read)
        return (age == "now" ? "GitHub read just now" : "GitHub read \(age) ago") + (github?.state == "partial" ? " · some repos failed" : "")
    }

    private var sourceHelp: String {
        guard let status = m.status else { return "" }
        let failed = status.repos.filter { $0.error != nil }.map { "\($0.id): \($0.error ?? "")" }
        return failed.isEmpty ? "Every repo read" : failed.joined(separator: "\n")
    }
}
