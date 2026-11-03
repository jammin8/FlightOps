import Charts
import SwiftUI





struct Glyph: View {
    let symbol: String
    var tint: Color? = nil
    var body: some View {
        Image(systemName: symbol)
            .symbolRenderingMode(.hierarchical)
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(tint ?? Color.secondary)
            .frame(width: 22, height: 22)
    }
}


struct ItemRow: View {
    let symbol: String
    let title: String
    var subtitle: String? = nil
    var value: String? = nil
    var tint: Color? = nil
    var meter: Double? = nil
    var url: URL? = nil
    var action: (() -> Void)? = nil
    var help: String? = nil
    var indent = false
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
            if divider { Divider().padding(.leading, indent ? 60 : 44).opacity(0.5) }
        }
    }

    private var content: some View {
        HStack(spacing: 10) {
            Glyph(symbol: symbol, tint: tint)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.callout).lineLimit(1)
                if let subtitle { Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer(minLength: 6)
            VStack(alignment: .trailing, spacing: 3) {
                if let value { Text(value).font(.caption).monospacedDigit().foregroundStyle(.secondary).lineLimit(1) }
                if let meter { MiniMeter(value: meter) }
            }
        }
        .padding(.leading, indent ? 26 : 10).padding(.trailing, 10).padding(.vertical, 6)
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

struct MiniMeter: View {
    let value: Double
    var width: CGFloat = 56
    var body: some View {
        let v = min(max(value, 0), 1)
        Capsule().fill(.quaternary).frame(width: width, height: 4)
            .overlay(alignment: .leading) { Capsule().fill(level(v)).frame(width: max(4, width * v), height: 4) }
    }
}


struct CapacityBar: View {
    let fraction: Double?
    var body: some View {
        GeometryReader { g in
            let f = min(max(fraction ?? 0, 0), 1)
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                if fraction != nil {
                    Capsule().fill(level(f)).frame(width: max(4, g.size.width * f))
                }
            }
        }
        .frame(height: 5)
    }
}




struct Stat: Identifiable {
    let id: String
    let symbol: String
    let value: Int?
    let label: String
    var action: (() -> Void)? = nil
}


struct StatStrip: View {
    let stats: [Stat]
    var body: some View {
        HStack(spacing: 0) {
            ForEach(stats) { s in
                Button { s.action?() } label: { cell(s) }
                    .buttonStyle(.plain)
                    .help(s.value == nil ? "\(s.label): unknown (its data is out of date)" : s.label)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .card()
    }

    private func cell(_ s: Stat) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Image(systemName: s.symbol).font(.caption).foregroundStyle(.secondary)
                Text(s.value.map { String($0) } ?? "–")
                    .font(.title3.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(s.value == nil ? Color.secondary : Color.primary)
                    .contentTransition(.numericText())
            }
            Text(s.label).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}




let planStages: [(key: String, label: String, opacity: Double)] = [
    ("done", "Done", 1.0), ("in_review", "In review", 0.62), ("in_progress", "In progress", 0.36),
    ("todo", "To do", 0), ("backlog", "Backlog", 0)]

struct StackedBar: View {
    let counts: [String: Int]
    var height: CGFloat = 8
    var body: some View {
        let total = max(counts.values.reduce(0, +), 1)
        GeometryReader { geo in
            HStack(spacing: 1) {
                ForEach(planStages.filter { $0.opacity > 0 }, id: \.key) { st in
                    let n = counts[st.key] ?? 0
                    if n > 0 {
                        Rectangle().fill(Color.accentColor.opacity(st.opacity))
                            .frame(width: max(2, geo.size.width * CGFloat(n) / CGFloat(total)))
                    }
                }
                Spacer(minLength: 0)
            }
            .background(.quaternary)
            .clipShape(Capsule())
        }
        .frame(height: height)
        .accessibilityLabel("\(counts["done"] ?? 0) done of \(total)")
    }
}


struct StageLegend: View {
    let counts: [String: Int]
    var body: some View {
        HStack(spacing: 0) {
            ForEach(planStages.filter { $0.key != "done" }, id: \.key) { st in
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 4) {
                        swatch(st.key, st.opacity)
                        Text("\(counts[st.key] ?? 0)").font(.callout.weight(.semibold)).monospacedDigit()
                    }
                    Text(st.label).font(.caption).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    @ViewBuilder private func swatch(_ key: String, _ opacity: Double) -> some View {
        if opacity > 0 {
            RoundedRectangle(cornerRadius: 3, style: .continuous).fill(Color.accentColor.opacity(opacity)).frame(width: 8, height: 8)
        } else if key == "todo" {
            RoundedRectangle(cornerRadius: 3, style: .continuous).fill(Color.secondary.opacity(0.35)).frame(width: 8, height: 8)
        } else {
            RoundedRectangle(cornerRadius: 3, style: .continuous).strokeBorder(Color.secondary.opacity(0.6), lineWidth: 1).frame(width: 8, height: 8)
        }
    }
}


struct BarRow: View {
    let title: String
    let pct: Int
    let counts: [String: Int]
    var chevron: Bool? = nil
    var url: URL? = nil
    var action: (() -> Void)? = nil
    var indent = false
    var target: String? = nil
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
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(indent ? .callout : .body).lineLimit(1)
                Spacer(minLength: 8)
                if let t = targetText(target) {
                    Text(t.1 < 0 ? "\(t.0), late" : t.0).font(.caption)
                        .foregroundStyle(t.1 < 0 ? Theme.warning : t.1 <= 14 ? Theme.caution : Color.secondary)
                }
                Text("\(pct)%").font(.callout.weight(.medium)).monospacedDigit()
                if let chevron { Chevron(open: chevron) }
            }
            StackedBar(counts: counts, height: indent ? 4 : 6)
        }
        .padding(.leading, indent ? 24 : 10).padding(.trailing, 10).padding(.vertical, 8)
        .background(hover && tap != nil ? Color.primary.opacity(0.06) : Color.clear, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
        .padding(.horizontal, 4)
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .help("\(title): \(counts["done"] ?? 0) done, \(counts["in_review"] ?? 0) in review, \(counts["in_progress"] ?? 0) in progress, \((counts["todo"] ?? 0) + (counts["backlog"] ?? 0)) not started")
        .contextMenu {
            if let url {
                Button("Open in Linear") { open(url) }
                Button("Copy Link") { copy(url.absoluteString) }
            }
        }
    }
}

struct Chevron: View {
    let open: Bool
    var body: some View {
        Image(systemName: "chevron.right").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            .rotationEffect(.degrees(open ? 90 : 0)).frame(width: 12)
    }
}




struct HealthGrid: View {
    let checks: [Check]
    var body: some View {
        let rows: [[Check]] = stride(from: 0, to: checks.count, by: 2).map { Array(checks[$0..<min($0 + 2, checks.count)]) }
        VStack(spacing: 0) {
            ForEach(Array(rows.enumerated()), id: \.offset) { i, pair in
                HStack(spacing: 0) {
                    ForEach(pair) { c in item(c).frame(maxWidth: .infinity, alignment: .leading) }
                    if pair.count == 1 { Spacer().frame(maxWidth: .infinity) }
                }
                .padding(.horizontal, 12).padding(.vertical, 7)
                if i < rows.count - 1 { Divider().padding(.leading, 14).opacity(0.5) }
            }
        }
        .padding(.vertical, 4)
    }

    func item(_ c: Check) -> some View {
        HStack(spacing: 7) {
            Image(systemName: healthSymbol(c.health))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(exceptionTint(c.health) ?? Color.secondary)
            VStack(alignment: .leading, spacing: 0) {
                Text(c.label).font(.callout)
                Text(c.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .help("\(c.label): \(c.detail)")
        .accessibilityElement(children: .combine)
    }
}




struct SeriesPoint: Identifiable {
    let id: Int
    let t: Date
    let v: Double
    let segment: Int
}


func segmented(_ samples: [Sample], gap: TimeInterval, value: (Sample) -> Double?) -> [SeriesPoint] {
    var out: [SeriesPoint] = []
    var segment = 0
    var last: Date? = nil
    for (i, s) in samples.enumerated() {
        guard let v = value(s) else {
            if last != nil { segment += 1 }
            last = nil
            continue
        }
        if let l = last, s.t.timeIntervalSince(l) > gap { segment += 1 }
        out.append(SeriesPoint(id: i, t: s.t, v: v, segment: segment))
        last = s.t
    }
    return out
}


struct Sparkline: View {
    let points: [SeriesPoint]
    var tint: Color = .accentColor
    var domain: ClosedRange<Double>? = nil

    private var yDomain: ClosedRange<Double> {
        if let domain { return domain }
        let values = points.map { $0.v }
        let lo = values.min() ?? 0
        let hi = values.max() ?? 1
        return hi - lo < 1 ? (lo - 1)...(hi + 1) : lo...hi
    }

    var body: some View {
        Chart(points) { p in
            LineMark(x: .value("Time", p.t), y: .value("Value", p.v), series: .value("Series", "s\(p.segment)"))
                .foregroundStyle(tint)
                .lineStyle(StrokeStyle(lineWidth: 1.5))
                .interpolationMethod(.monotone)
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartLegend(.hidden)
        .chartYScale(domain: yDomain)
    }
}


struct HistoryChart: View {
    let history: [Sample]
    var body: some View {
        let cpu: [SeriesPoint] = segmented(history, gap: 60) { Double($0.cpu) }
        let mem: [SeriesPoint] = segmented(history, gap: 60) { $0.mem }
        Chart {
            ForEach(cpu) { p in
                LineMark(x: .value("Time", p.t), y: .value("Percent", p.v), series: .value("Series", "cpu\(p.segment)"))
                    .foregroundStyle(Color.accentColor)
                    .lineStyle(StrokeStyle(lineWidth: 2))
            }
            ForEach(mem) { p in
                LineMark(x: .value("Time", p.t), y: .value("Percent", p.v), series: .value("Series", "mem\(p.segment)"))
                    .foregroundStyle(Color.purple)
                    .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 2]))
            }
        }
        .chartYScale(domain: 0...100)
        .chartLegend(.hidden)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                AxisValueLabel(format: .dateTime.hour().minute())
            }
        }
        .chartYAxis {
            AxisMarks(position: .trailing, values: [0, 50, 100]) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                AxisValueLabel()
            }
        }
        .frame(height: 80)
        .accessibilityLabel("CPU and memory over the last \(history.count) samples")
    }
}




struct ExpandCard<Header: View, Detail: View>: View {
    let id: String
    @ViewBuilder let header: Header
    @ViewBuilder let detail: Detail
    @AppStorage("openCards") private var raw = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.sectionOpen) private var sectionOpen
    @State private var hover = false

    var isOpen: Bool { sectionOpen ?? raw.split(separator: ",").contains { $0 == id } }

    private func toggle() {
        var s = Set(raw.split(separator: ",").map(String.init))
        if s.contains(id) { s.remove(id) } else { s.insert(id) }
        let next = s.sorted().joined(separator: ",")
        if reduceMotion {
            raw = next
        } else {
            withAnimation(.snappy(duration: 0.22)) { raw = next }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if sectionOpen != nil {
                header.frame(maxWidth: .infinity, alignment: .leading).padding(12)
            } else {
                Button(action: toggle) {
                    HStack(alignment: .top, spacing: 8) {
                        header.frame(maxWidth: .infinity, alignment: .leading)
                        Chevron(open: isOpen).padding(.top, 5)
                    }
                    .padding(12)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .onHover { hover = $0 }
                .accessibilityHint(isOpen ? "Collapses the card" : "Opens the card for more")
            }
            if isOpen {
                Divider().opacity(0.5)
                VStack(alignment: .leading, spacing: 0) { detail }
                    .padding(.vertical, 4)
                    .transition(reduceMotion ? AnyTransition.opacity : AnyTransition.opacity.combined(with: .move(edge: .top)))
            }
        }
        .card()
        .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
            .strokeBorder(Color.primary.opacity(hover ? 0.12 : 0), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
    }
}
