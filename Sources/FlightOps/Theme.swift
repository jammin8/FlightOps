import AppKit
import SwiftUI

enum Theme {
    static let caution = Color.orange.opacity(0.85)
    static let warning = Color.red.opacity(0.85)
    static let calm = Color.blue.opacity(0.7)
    static let corner: CGFloat = 10
    static let margin: CGFloat = 16
    static let gap: CGFloat = 12
    static let width: CGFloat = 520
}

extension View {
    func card() -> some View {
        padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.fill.quaternary, in: RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).strokeBorder(.separator.opacity(0.5), lineWidth: 0.5))
    }
}

func openLink(_ text: String?) {
    guard let text, let url = URL(string: text) else { return }
    NSWorkspace.shared.open(url)
}

struct CardTitle: View {
    var text: String
    var detail: String? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(text).font(.subheadline.weight(.semibold))
            Spacer()
            if let detail { Text(detail).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct RepoDot: View {
    var color: Color
    var body: some View { Circle().fill(color).frame(width: 8, height: 8) }
}

func verdictTint(_ verdict: String?) -> Color? {
    switch verdict {
    case "busy": return Theme.calm
    case "slowing": return Theme.caution
    case "stuck": return Theme.warning
    default: return nil
    }
}
