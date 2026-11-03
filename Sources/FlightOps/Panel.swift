import AppKit
import ServiceManagement
import SwiftUI




struct Panel: View {
    @Environment(\.openSettings) private var openSettings
    @ObservedObject var m: Monitor
    @AppStorage("tab") private var tab = Tab.mission.rawValue

    var body: some View {
        VStack(spacing: 0) {
            header.padding(.horizontal, Theme.margin).padding(.top, 14).padding(.bottom, 10)
            Picker("", selection: $tab) {
                ForEach(Tab.allCases, id: \.rawValue) { Text($0.title).tag($0.rawValue) }
            }
            .pickerStyle(.segmented).labelsHidden()
            .padding(.horizontal, Theme.margin).padding(.bottom, 12)
            ScrollView(.vertical) {


                page
                    .frame(width: 520 - 2 * Theme.margin, alignment: .topLeading)
                    .padding(.horizontal, Theme.margin).padding(.bottom, 12)
                    .frame(maxWidth: .infinity, alignment: .top)
            }
            .scrollIndicators(.automatic)
            .frame(maxHeight: .infinity, alignment: .top)
            Divider()
            footer.padding(.horizontal, Theme.margin).padding(.vertical, 8)
        }
        .frame(width: 520, height: min(780, (NSScreen.main?.visibleFrame.height ?? 900) - 40), alignment: .top)
        .background {

            shortcuts.opacity(0).allowsHitTesting(false)
        }
        .onAppear {
            if Tab(rawValue: tab) == nil { tab = Tab.mission.rawValue }
            m.recompute()
        }
    }

    @ViewBuilder private var page: some View {
        switch Tab(rawValue: tab) ?? .mission {
        case .mission: MissionTab(m: m, tab: $tab)
        case .charts: ChartsTab(m: m)
        case .models: ModelsTab(m: m)
        }
    }

    private var shortcuts: some View {
        HStack {

            ForEach(Array(Tab.allCases.enumerated()), id: \.offset) { i, t in
                Button("") { tab = t.rawValue }.keyboardShortcut(KeyEquivalent(Character(String(i + 1))))
            }
        }
    }

    private var header: some View {
        let v = m.verdict
        return HStack(spacing: 8) {
            HStack(spacing: 5) {
                Image(nsImage: JetIcon.image).resizable().frame(width: 16, height: 16).foregroundStyle(.secondary)
                Text("FlightOps").font(.headline)
            }
            Spacer()
            Button { tab = Tab.mission.rawValue } label: {
                HStack(spacing: 6) {
                    verdictMark(v)
                    Text(v.text).font(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                }
            }
            .buttonStyle(.plain)
            .help("What needs attention right now; click for Mission. Partial data: a source is out of date, so this may be missing something.")
            if !v.open.isEmpty {
                Button { m.dismissShown() } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary).frame(minWidth: 20, minHeight: 20).contentShape(Rectangle()) }
                    .buttonStyle(.plain).help("Dismiss until it changes")
            }
        }
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder private func verdictMark(_ v: SystemVerdict) -> some View {
        switch v.health {
        case .bad, .warn:
            Circle().fill(v.health == .bad ? Theme.warning : Theme.caution).frame(width: 8, height: 8)
        case .unknown:
            Image(systemName: "questionmark.circle").font(.caption).foregroundStyle(.secondary)
        case .ok:
            Image(systemName: "checkmark.circle").font(.caption).foregroundStyle(.secondary)
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            SourceDots(m: m)
            Spacer()
            Button { m.refreshAll() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).help("Refresh (⌘R)").keyboardShortcut("r")
            Menu {
                Button("Open Linear") { open(SiteConfig.shared.linearURL) }
                Button("Open GitHub") { open(SiteConfig.shared.githubURL) }
                Button("Copy Status") { copy(m.statusText) }
                Button("Preview Board Animations") { NotificationCenter.default.post(name: .previewBoardMotion, object: nil) }
                Button("Show Dismissed (\(m.dismissed.count))") { m.restoreDismissed() }.disabled(m.dismissed.isEmpty)
                Divider()

                Button("Settings…") { NSApp.activate(ignoringOtherApps: true); openSettings() }.keyboardShortcut(",")
                Button("Quit FlightOps") { NSApplication.shared.terminate(nil) }.keyboardShortcut("q")
            } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().help("More")
        }
    }
}


struct SourceDots: View {
    @ObservedObject var m: Monitor

    var body: some View {
        HStack(spacing: 8) {
            ForEach(SourceID.allCases, id: \.self) { s in
                HStack(spacing: 3) {
                    dot(m.freshness(s), failed: (m.status[s]?.failures ?? 0) > 0)
                    Text(shortName(s)).font(.caption2).foregroundStyle(.secondary)
                }
                .help(helpText(s))
                .accessibilityElement(children: .combine)
            }
        }
    }

    private func shortName(_ s: SourceID) -> String { s == .agents ? "VM" : s.label }

    @ViewBuilder private func dot(_ f: Freshness, failed: Bool) -> some View {
        switch f {
        case .fresh:
            Circle().fill(Color.secondary.opacity(0.5)).frame(width: 6, height: 6)
        case .stale:
            Circle().fill(Theme.caution).frame(width: 6, height: 6)
        case .missing:
            Circle().strokeBorder(failed ? Theme.caution : Color.secondary, lineWidth: 1).frame(width: 6, height: 6)
        }
    }

    private func helpText(_ s: SourceID) -> String {
        let st = m.status[s] ?? SourceStatus()
        let seen = st.sampleAt.map { "updated \(ago($0)) ago" } ?? "no data yet"
        let why = st.error.map { " · \($0)" } ?? ""
        return "\(s.label): \(seen)\(why)"
    }
}
