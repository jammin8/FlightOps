import AppKit
import SwiftUI













enum Theme {

    static let hud = Color.green.opacity(0.7)
    static let cyan = Color.blue.opacity(0.6)
    static let caution = Color.orange.opacity(0.8)
    static let warning = Color.red.opacity(0.8)
    static let gunmetal = Color.gray.opacity(0.55)
    static let corner: CGFloat = 10
    static let margin: CGFloat = 16
    static let gap: CGFloat = 14
}

func color(_ h: Health) -> Color {
    switch h {
    case .ok: return Color.secondary
    case .warn: return Theme.caution
    case .bad: return Theme.warning
    case .unknown: return Color.secondary
    }
}

func healthSymbol(_ h: Health) -> String {
    switch h {
    case .ok: return "checkmark.circle"
    case .warn: return "exclamationmark.triangle.fill"
    case .bad: return "exclamationmark.octagon.fill"
    case .unknown: return "questionmark.circle"
    }
}


func exceptionTint(_ h: Health) -> Color? {
    switch h {
    case .bad: return Theme.warning
    case .warn: return Theme.caution
    case .ok, .unknown: return nil
    }
}


func level(_ x: Double) -> Color { x < 0.75 ? Theme.cyan : x < 0.9 ? Theme.caution : Theme.warning }

func open(_ url: URL) { NSWorkspace.shared.open(url) }


func animate(_ reduceMotion: Bool, _ change: () -> Void) {
    if reduceMotion {
        change()
    } else {
        withAnimation(.snappy(duration: 0.2), change)
    }
}
func copy(_ s: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(s, forType: .string)
}

extension View {
    func card() -> some View {

        background(.fill.quaternary, in: RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).strokeBorder(.separator.opacity(0.5), lineWidth: 0.5))
    }
}




private struct SectionOpenKey: EnvironmentKey { static let defaultValue: Bool? = nil }
extension EnvironmentValues {
    var sectionOpen: Bool? {
        get { self[SectionOpenKey.self] }
        set { self[SectionOpenKey.self] = newValue }
    }
}



struct OpsSection<Content: View>: View {
    let title: String
    var note: String? = nil

    var key: String? = nil

    var trailing: AnyView? = nil


    var expandable = false
    @ViewBuilder let content: Content
    @AppStorage("openSections") private var raw = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var id: String { key ?? title }
    private var isOpen: Bool { raw.split(separator: ",").contains { $0 == id } }

    private func toggle() {
        var s = Set(raw.split(separator: ",").map(String.init))
        if s.contains(id) { s.remove(id) } else { s.insert(id) }
        let next = s.sorted().joined(separator: ",")
        if reduceMotion { raw = next } else { withAnimation(.easeInOut(duration: 0.2)) { raw = next } }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                if expandable {
                    SectionToggle(title: title, open: isOpen, action: toggle)
                } else {
                    Text(title).font(.headline).foregroundStyle(.secondary).accessibilityAddTraits(.isHeader)
                }
                Spacer()
                if let trailing { trailing } else if let note { Text(note).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }
            .padding(.horizontal, 4)
            if expandable { content.environment(\.sectionOpen, isOpen) } else { content }
        }
    }
}


struct SectionToggle: View {
    let title: String
    let open: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Text(title).font(.headline)

                Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).rotationEffect(.degrees(open ? 90 : 0))
            }
            .foregroundStyle(open ? Theme.cyan : .secondary)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(open ? Theme.cyan.opacity(0.10) : .clear, in: Capsule())
            .overlay(Capsule().strokeBorder(open ? Theme.cyan : Color.secondary.opacity(0.35), lineWidth: open ? 1.2 : 0.6))
        }
        .buttonStyle(.plain)
        .help(open ? "Show less" : "Show more")
        .accessibilityHint(open ? "Collapses the section" : "Opens the section for more")
    }
}


struct Block<Content: View>: View {
    let title: String
    var note: String? = nil
    @ViewBuilder let content: Content
    var body: some View {
        OpsSection(title: title, note: note) {
            VStack(alignment: .leading, spacing: 0) { content }
                .padding(.vertical, 4)
                .frame(maxWidth: .infinity, alignment: .leading)
                .card()
        }
    }
}


struct Line: View {
    let title: String
    var value: String? = nil
    var dot: Health? = nil
    var url: URL? = nil
    var action: (() -> Void)? = nil
    var help: String? = nil
    var divider = true
    @State private var hover = false

    private var tap: (() -> Void)? {
        if let action { return action }
        if let url { return { open(url) } }
        return nil
    }

    var body: some View {
        VStack(spacing: 0) {
            if let tap {
                Button(action: tap) { content }.buttonStyle(.plain)
            } else {
                content
            }
            if divider { Divider().padding(.leading, 14).opacity(0.5) }
        }
    }

    private var content: some View {
        HStack(spacing: 8) {
            if let dot {
                Circle().fill(dot == .bad ? Color.red : dot == .warn ? Color.orange : Color.secondary.opacity(0.5))
                    .frame(width: 7, height: 7)
                    .accessibilityLabel(dot == .bad ? "problem" : dot == .warn ? "warning" : "unknown")
            }
            Text(title).font(.callout).lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 8)
            if let value { Text(value).font(.callout).monospacedDigit().foregroundStyle(.secondary).lineLimit(1) }
        }
        .padding(.horizontal, 10).padding(.vertical, 7)
        .background(hover && tap != nil ? Color.primary.opacity(0.06) : Color.clear, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
        .padding(.horizontal, 4)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .help(help ?? title)
        .contextMenu {
            if let url {
                Button("Open in Browser") { open(url) }
                Button("Copy Link") { copy(url.absoluteString) }
            }
            Button("Copy Title") { copy(title) }
        }
    }
}


struct SubHead: View {
    let text: String
    var body: some View {
        Text(text.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
            .padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}


struct StaleNote: View {
    let text: String
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "clock.badge.questionmark").foregroundStyle(.secondary)
            Text(text).foregroundStyle(.secondary).lineLimit(2)
        }
        .font(.caption)
        .padding(.horizontal, 4)
    }
}



struct TapCard<Face: View, Detail: View>: View {
    var padding: CGFloat = 8
    @ViewBuilder let face: Face
    @ViewBuilder let detail: Detail
    @State private var tapped = false
    @Environment(\.sectionOpen) private var sectionOpen

    private var open: Bool { sectionOpen ?? tapped }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            face
            if open {
                Divider().opacity(0.5)
                detail.font(.caption2).foregroundStyle(.secondary)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(padding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture { if sectionOpen == nil { withAnimation(.easeInOut(duration: 0.18)) { tapped.toggle() } } }
        .card()
    }
}



struct StatusPill: View {
    let label: String
    let color: Color
    var font: Font = .caption.weight(.medium)

    var body: some View {
        Text(label)
            .font(font)
            .lineLimit(1)
            .padding(.horizontal, 6).padding(.vertical, 1)
            .background(color.opacity(0.14), in: Capsule())
            .overlay(Capsule().strokeBorder(color.opacity(0.55), lineWidth: 0.8))
    }
}



func repoTint(_ repo: String) -> Color {
    switch repo {
    case "backend": return Color(red: 0.85, green: 0.64, blue: 0.25)
    case "ios": return Color(red: 0.30, green: 0.80, blue: 0.48)
    case "web": return Color(red: 0.36, green: 0.53, blue: 0.75)
    default: return Color(red: 0.47, green: 0.52, blue: 0.56)
    }
}


func stageColor(_ s: PRStage) -> Color {
    [.work: Theme.warning, .ci: Theme.caution, .review: Theme.cyan, .merged: Theme.hud][s] ?? .gray
}


struct CountBadge: View {
    let count: Int

    var tint = Color(red: 0.72, green: 0.32, blue: 0.29)
    var text: String? = nil
    var body: some View {
        Text(text ?? (count > 99 ? "99+" : "\(count)"))
            .font(.system(size: 9.5, weight: .bold)).monospacedDigit()
            .foregroundStyle(.white)
            .padding(.horizontal, count > 9 || (text?.count ?? 0) > 1 ? 4.5 : 0)
            .frame(minWidth: 16, minHeight: 16)

            .background(tint, in: Capsule())
            .accessibilityLabel("\(text ?? String(count)) PRs")
    }
}
