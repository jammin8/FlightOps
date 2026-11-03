import SwiftUI



struct DeliveryBoard: View {
    @ObservedObject var m: Monitor
    @Namespace private var ns

    @Environment(\.sectionOpen) private var sectionOpen

    @State private var openStacks: Set<String> = []
    private var all: Bool { sectionOpen ?? false }


    @State private var picked: String? = UserDefaults.standard.string(forKey: "boardPick")
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.accessibilityDifferentiateWithoutColor) private var noColorOnly


    @State private var fx: [String: CardFX] = [:]
    @State private var ghosts: [String: Ghost] = [:]

    private struct Ghost: Equatable { let stage: PRStage; let index: Int; let label: String; let start: Date }


    private var stageMap: [String: PRStage] {
        var out: [String: PRStage] = [:]
        for p in m.openPRs { out[p.id] = PRStage.of(p) }
        let day = Date().addingTimeInterval(-86400)
        for mg in m.repos?.recent ?? [] where (mg.mergedDate ?? .distantPast) > day { out[mg.id] = .merged }
        return out
    }



    private func preview() {
        let ids = lastCols.values.flatMap { $0 }
        guard ids.count >= 4 else { return }
        let now = Date()
        let kinds: [CardFX.Kind] = [.hop, .confetti, .bounce, .sweep]
        for (i, k) in kinds.enumerated() {
            let start = now.addingTimeInterval(Double(i) * 1.2)
            DispatchQueue.main.asyncAfter(deadline: .now() + Double(i) * 1.2) {
                fx[ids[i]] = CardFX(kind: k, start: start)
                if k == .sweep, let col = lastCols.first(where: { $0.value.contains(ids[i]) }) {
                    ghosts[ids[i]] = Ghost(stage: col.key, index: col.value.firstIndex(of: ids[i]) ?? 0, label: "#preview", start: start)
                }
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 6.5) { fx = [:]; ghosts = [:] }
    }


    private func react(from old: [String: PRStage], to new: [String: PRStage], cols: [PRStage: [String]]) {
        guard !old.isEmpty, !reduceMotion else { return }
        let now = Date()
        for (id, to) in new {
            guard let from = old[id], from != to else { continue }
            if to.rawValue > from.rawValue {
                fx[id] = CardFX(kind: to == .merged ? .bounce : to == .review ? .confetti : .hop, start: now)
            } else {
                fx[id] = CardFX(kind: .sweep, start: now)
                let short = ["backend": "BE", "web": "Web", "ios": "iOS"][String(id.split(separator: "#")[0])] ?? ""
                ghosts[id] = Ghost(stage: from, index: cols[from]?.firstIndex(of: id) ?? 0,
                                   label: "\(short) #\(id.split(separator: "#").last ?? "")", start: now)
            }
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_800_000_000)
            fx = fx.filter { now.timeIntervalSince($0.value.start) < 0 || Date().timeIntervalSince($0.value.start) < 1.7 }
            ghosts = ghosts.filter { Date().timeIntervalSince($0.value.start) < 1.0 }
        }
    }

    private typealias Item = FlowItem

    private func columns(at now: Date) -> [PRStage: [Item]] {
        var w = m.world
        w.now = now
        return FlowBoard.columns(w)
    }

    private func count(_ s: PRStage, _ items: [Item]) -> String {
        FlowBoard.stageText(m.world, s, items)
    }


    @State private var lastCols: [PRStage: [String]] = [:]
    private var columnIDs: [PRStage: [String]] { columns(at: Date()).mapValues { $0.map(\.id) } }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in board(at: ctx.date) }
            .onChange(of: stageMap) { old, new in react(from: old, to: new, cols: lastCols) }

            .onChange(of: columnIDs, initial: true) { _, ids in lastCols = ids }
            .onReceive(NotificationCenter.default.publisher(for: .previewBoardMotion)) { _ in preview() }
    }

    private func board(at now: Date) -> some View {
        let cols = columns(at: now)
        return VStack(alignment: .leading, spacing: 8) {

        HStack(alignment: .top, spacing: 18) {
            ForEach(PRStage.allCases) { s in
                let items = cols[s] ?? []
                VStack(alignment: .leading, spacing: 4) {
                    Text(count(s, items)).font(.title3.weight(.semibold)).monospacedDigit()
                    Text(s.label).font(.caption).foregroundStyle(.secondary).lineLimit(1)


                    ForEach(["backend", "ios", "web"], id: \.self) { r in
                        let group = items.filter { $0.repo == r }
                        let total = FlowBoard.stackObserved(m.world, s, r, group)
                        let open = all || openStacks.contains("\(s.rawValue)|\(r)")


                        if open && !group.isEmpty && total > 1 { stackHeader(r, total: total, text: FlowBoard.stackText(m.world, s, r, group), open: open, stage: s, empty: false) }
                        if group.isEmpty {
                            RoundedRectangle(cornerRadius: 7, style: .continuous)
                                .strokeBorder(Color.secondary.opacity(0.18), style: StrokeStyle(lineWidth: 0.8, dash: [3, 3]))
                                .frame(height: 22).padding(.top, 8).padding(.bottom, 7).accessibilityHidden(true)
                                .overlay { if s == .merged, total > 0 { Text("\(total) earlier").font(.caption2).foregroundStyle(.secondary) } }
                        } else if open {
                            VStack(alignment: .leading, spacing: 4) {
                                ForEach(Array(group.enumerated()), id: \.element.id) { i, it in
                                    let f = fx[it.id]
                                    chip(it, stage: s)
                                        .cardMotion(f)
                                        .matchedGeometryEffect(id: f?.kind == .sweep ? it.id + "~swept" : it.id, in: ns)
                                        .overlay { if f?.kind == .confetti, let st = f?.start { ConfettiBurst(start: st) } }
                                        .transition(.opacity.combined(with: .scale(scale: 0.9)))

                                        .animation(reduceMotion ? .easeInOut(duration: 0.2)
                                                   : .spring(response: 0.48, dampingFraction: 0.66).delay(Double(i) * 0.035),
                                                   value: group.map(\.id))
                                }
                                if s == .merged && total > group.count {
                                    Text("+\(total - group.count) earlier").font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                            .padding(.bottom, 7)
                        } else {
                            let f = group.first.flatMap { fx[$0.id] }
                            stack(group, repo: r, stage: s, total: total)
                                .cardMotion(f)
                                .matchedGeometryEffect(id: (group.first?.id ?? r) + (f?.kind == .sweep ? "~swept" : ""), in: ns)
                                .overlay { if f?.kind == .confetti, let st = f?.start { ConfettiBurst(start: st) } }
                        }
                    }
                    if let g = ghosts.values.first(where: { $0.stage == s }) { ShatterGhost(label: g.label, start: g.start) }
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
        }

        HStack(spacing: 12) {
            ForEach(["backend", "ios", "web"], id: \.self) { r in
                HStack(spacing: 4) {
                    RoundedRectangle(cornerRadius: 3, style: .continuous).fill(repoTint(r).opacity(0.35)).frame(width: 14, height: 9)
                    Text(repoLabel(r)).font(.caption2).foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 3) {
                Image(systemName: "hourglass").font(.system(size: 9, weight: .semibold)).foregroundStyle(Theme.caution)
                Text("Overdue").font(.caption2).foregroundStyle(.secondary)
            }
        }
        if let id = picked, let it = cols.values.flatMap({ $0 }).first(where: { $0.id == id }),
           let stage = cols.first(where: { $0.value.contains { $0.id == id } })?.key {

            detail(it, stage: stage).transition(.opacity)
        }
        }
        .padding(10)
        .card()
    }




    private func detail(_ it: Item, stage: PRStage) -> some View {
        let now = Date().timeIntervalSince1970
        let t = m.timelines[it.id]
        let opened = it.opened?.timeIntervalSince1970 ?? t?.opened
        let end = it.merged?.timeIntervalSince1970 ?? now
        let age = opened.map { duration(Int(end - $0)) }

        let live = m.openPRs.first { $0.id == it.id }
        let status: PRStatus? = it.merged != nil ? .merged : it.progress != nil ? .inCI : live.map(PRStatus.of) ?? t?.current
        let inStage = t.flatMap { $0.current == status ? $0.inStage(now: now) : nil }.map { duration(Int($0)) }
        let worst = t?.bottleneck(now: now)
        let totals = t?.totals(now: now) ?? [:]
        let tracked = totals.filter { $0.key != .merged }.values.reduce(0, +)
        let pm = m.shownModels[it.id]
        let facts = m.prFacts[it.id]
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("\(repoLabel(it.repo)) #\(it.number)").font(.caption.weight(.semibold))
                Spacer()
                Link(destination: prURL(it.repo, it.number)) { Image(systemName: "arrow.up.forward.app") }
                    .help("Open on GitHub").accessibilityLabel("Open on GitHub")
                Button { withAnimation(.easeInOut(duration: 0.18)) { picked = nil } } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }
                .buttonStyle(.plain).help("Close").accessibilityLabel("Close details")
            }
            Text(it.title).font(.caption2).foregroundStyle(.secondary).lineLimit(2).padding(.top, -6)
            HStack(spacing: 6) {
                tile(age ?? "–", it.merged != nil ? "Open → merged" : "Age")
                if let mg = it.merged {
                    tile("\(duration(Int(now - mg.timeIntervalSince1970))) ago", "Merged")
                } else {
                    tile(it.timer ?? inStage ?? "–", (status?.label ?? "Now") + (it.timer == nil && inStage != nil ? " so far" : ""),
                         tint: status.flatMap { ChartsTab.statusColor[$0] })
                }
                tile(worst.map { duration(Int($0.seconds)) } ?? "–", worst.map { "Most: " + $0.status.label } ?? "Most time",
                     tint: worst.flatMap { ChartsTab.statusColor[$0.status] })
            }
            if tracked > 0 {
                let parts = PRStatus.allCases.filter { $0 != .merged && (totals[$0] ?? 0) > 0 }
                StackBar(label: "", trailing: "",
                         parts: parts.map { (totals[$0] ?? 0, ChartsTab.statusColor[$0] ?? Theme.gunmetal) }, total: tracked)
                    .help(parts.map { "\($0.label) \(duration(Int(totals[$0] ?? 0)))" }.joined(separator: " · "))
            }
            if let f = facts, !f.heads.isEmpty {
                HStack(spacing: 6) {
                    Text("CI tries").font(.caption2).foregroundStyle(.secondary)
                    dots(f)
                    Spacer(minLength: 4)

                    let tries = f.strip().compactMap { mark -> String? in
                        if case let .head(outcome, _, _, _, _) = mark, outcome == "pass" || outcome == "fail" { return outcome }
                        return nil
                    }
                    let failed = tries.filter { $0 == "fail" }.count
                    if !tries.isEmpty {
                        HStack(spacing: 3) {
                            Image(systemName: failed > 0 ? "xmark.circle.fill" : "checkmark.circle.fill").font(.caption2)
                            Text(failed > 0 ? "failed \(failed) of \(tries.count)" : "\(tries.count) passed").font(.caption2.weight(.medium)).monospacedDigit()
                        }
                        .foregroundStyle(failed > 0 ? Theme.warning : Theme.hud)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background((failed > 0 ? Theme.warning : Theme.hud).opacity(0.12), in: Capsule())
                    }
                }

                .help(f.ciLines(now: now).joined(separator: "\n"))
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(f.stripSpoken)
            }
            if let line = modelsLine(pm, builder: t?.builder) {
                HStack(spacing: 5) {
                    Image(systemName: "sparkles").font(.caption2).foregroundStyle(.secondary)
                    Text(line).font(.caption2).foregroundStyle(.secondary).lineLimit(2)
                }
                .help(pm.map { "Tokens: " + $0.tokenLine(opened: opened) } ?? "")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }


    private func tile(_ value: String, _ caption: String, tint: Color? = nil) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value).font(.callout.weight(.semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.8)
            HStack(spacing: 3) {
                if let tint { Circle().fill(tint).frame(width: 6, height: 6) }
                Text(caption).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
    }



    private func dots(_ f: PRFact) -> some View {



        return ViewThatFits(in: .horizontal) {
            ForEach([12, 8, 5, 3], id: \.self) { n in dotRow(f.strip(), last: n) }
        }
    }

    private func dotRow(_ all: [PRFact.Mark], last n: Int) -> some View {
        let shown = Array(all.suffix(n))
        let hidden = all.count - shown.count
        return HStack(spacing: 4) {
            if hidden > 0 { Text("+\(hidden)").font(.caption2).foregroundStyle(.secondary).monospacedDigit() }
            ForEach(Array(shown.enumerated()), id: \.offset) { _, mark in
                switch mark {
                case .gap: Rectangle().fill(Color.secondary.opacity(0.35)).frame(width: 1, height: 9)
                case .merged: Image(systemName: "diamond.fill").font(.system(size: 7)).foregroundStyle(Theme.hud)
                case let .head(outcome, sync, replaced, _, r0):
                    let tint: Color = outcome == "pass" ? Theme.hud : outcome == "fail" ? Theme.warning : .secondary
                    Group {
                        if sync { Image(systemName: "diamond").font(.system(size: 7)).foregroundStyle(tint) }
                        else if outcome == "pass" || outcome == "fail" { Circle().fill(tint).frame(width: 7, height: 7) }
                        else { Circle().strokeBorder(tint, lineWidth: 1).frame(width: 7, height: 7) }
                    }
                    .padding(2)
                    .overlay { if r0 { Circle().strokeBorder(Color.primary.opacity(0.5), lineWidth: 0.8) } }
                    .opacity(replaced ? 0.35 : 1)
                }
            }
        }
    }


    private func modelsLine(_ pm: PRModels?, builder: String?) -> String? {
        var parts: [String] = []

        func short(_ s: String) -> String {
            s.replacingOccurrences(of: " · claudeAgent", with: "").replacingOccurrences(of: " · codex", with: "")
                .replacingOccurrences(of: "Claude ", with: "").replacingOccurrences(of: " · ", with: " ")
        }
        if let b = ModelName.builderLine(models: pm, timelineBuilder: builder) { parts.append(short(b) + " built") }
        if let line = pm?.reworkSummary(short: short) { parts.append(line) }
        let reviewers = pm?.reviews.map { Array(Set($0.compactMap(\.label))).sorted() } ?? pm?.reviewed_by?.map(ModelName.display)
        if let r = reviewers, !r.isEmpty { parts.append((pm?.isLive == true ? "review title: " : "reviewed by ") + Array(Set(r.map(short))).sorted().joined(separator: ", ")) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }


    private func spoken(_ it: Item, stage: PRStage) -> String {
        var parts = ["\(repoLabel(it.repo)) PR \(it.number)", stage.label]
        if it.mark == .bad { parts.append("checks failing") }
        if it.conflicts { parts.append("conflicts with main") }
        if it.aging { parts.append("overdue, in this column longer than usual") }

        if let p = it.progress { parts.append(it.progressKnown ? "CI still running, about \(Int(p * 100)) percent of its usual time elapsed" : "CI running") }
        if let t = it.timer { parts.append(stage == .merged ? "merged " + t : t.replacingOccurrences(of: "~", with: "about ")) }
        parts.append(it.title)
        return parts.joined(separator: ", ")
    }



    private func chip(_ it: Item, stage: PRStage, base: Bool = false) -> some View {
        Button { withAnimation(.easeInOut(duration: 0.18)) { picked = picked == it.id ? nil : it.id } } label: {
            chipFace(it)
                .background(base ? Color(nsColor: .windowBackgroundColor) : .clear, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
        }
        .buttonStyle(.plain)
        .help("\(repoLabel(it.repo)) #\(it.number): \(it.title)")
        .accessibilityLabel(spoken(it, stage: stage))
        .accessibilityHint("Shows this PR's details")
    }


    private func chipFace(_ it: Item) -> some View {
            HStack(spacing: 3) {

                if let mark = it.mark {
                    Image(systemName: mark == .bad ? "xmark.octagon.fill" : "arrow.triangle.branch")
                        .font(.caption2).foregroundStyle(color(mark)).accessibilityHidden(true)
                }
                if it.approved { Image(systemName: "checkmark.seal.fill").font(.caption2).foregroundStyle(Theme.hud).accessibilityHidden(true) }


                if noColorOnly {
                    Text(["backend": "BE", "web": "Web", "ios": "iOS"][it.repo] ?? it.repo).foregroundStyle(.secondary).fixedSize()
                }
                Text("#\(it.number)").fontWeight(.medium).fixedSize()

                if it.aging {
                    Image(systemName: "hourglass").font(.system(size: 9, weight: .semibold)).foregroundStyle(Theme.caution)
                        .accessibilityHidden(true)
                }
                Spacer(minLength: 0)

                if let t = it.timer { Text(t).font(.subheadline.monospacedDigit()).foregroundStyle(.primary.opacity(0.8)).fixedSize() }
            }
            .font(.caption).monospacedDigit().lineLimit(1)
            .padding(.horizontal, 6).padding(.vertical, 4)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(alignment: .leading) {
                GeometryReader { g in
                    ZStack(alignment: .leading) {

                        repoTint(it.repo).opacity(contrast == .increased ? 0.32 : 0.20)


                        if let p = it.progress { repoTint(it.repo).opacity(contrast == .increased ? 0.75 : 0.55).frame(width: g.size.width * p) }
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
            }
            .overlay {
                if contrast == .increased {
                    RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(repoTint(it.repo), lineWidth: 1)
                }
            }
    }




    private func stack(_ group: [Item], repo: String, stage: PRStage, total: Int) -> some View {
        ZStack(alignment: .top) {
            ForEach(Array([2, 1].filter { $0 < total }), id: \.self) { depth in
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(repoTint(repo).opacity(0.12 + 0.04 * Double(2 - depth)))
                    .frame(height: 22)
                    .scaleEffect(x: 1 - 0.05 * Double(depth), y: 1, anchor: .top)
                    .offset(y: 3.5 * Double(depth))
                    .accessibilityHidden(true)
            }
            if let top = group.first {
                chip(top, stage: stage, base: true)
            }
        }


        .overlay(alignment: .topTrailing) {
            if total > 1 {
                Button { withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) { toggleStack(stage, repo) } } label: {
                    CountBadge(count: total, tint: stage == .merged ? Color(red: 0.22, green: 0.52, blue: 0.34) : Color(red: 0.72, green: 0.32, blue: 0.29),
                               text: FlowBoard.stackText(m.world, stage, repo, group))
                        .padding(4).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .offset(x: 13, y: -14)
                .help("\(repoLabel(repo)): \(FlowBoard.stackText(m.world, stage, repo, group)) in \(stage.label.lowercased()). Click to show them")
                .accessibilityLabel("\(repoLabel(repo)), \(FlowBoard.stackText(m.world, stage, repo, group)) PRs \(stage.label)")
                .accessibilityHint("Shows this repo's PRs in this column")
            }
        }
        .padding(.top, 8)
        .padding(.bottom, 7)
    }



    private func stackHeader(_ repo: String, total: Int, text: String, open: Bool, stage: PRStage, empty: Bool) -> some View {
        Button {
            guard !empty, total > 1, !all else { return }
            withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) { toggleStack(stage, repo) }
        } label: {
            HStack(spacing: 4) {
                Text(repoLabel(repo)).font(.caption2).foregroundStyle(.tertiary)
                Spacer(minLength: 2)
                if !empty && total > 1 {

                    HStack(spacing: 2) {
                        Text(text).font(.caption2.weight(.medium)).monospacedDigit()
                        Image(systemName: open ? "chevron.up" : "chevron.down").font(.system(size: 7, weight: .bold))
                    }
                    .foregroundStyle(open ? Theme.cyan : .secondary)
                    .padding(.horizontal, 6).padding(.vertical, 1)
                    .background(open ? Theme.cyan.opacity(0.10) : .clear, in: Capsule())
                    .overlay(Capsule().strokeBorder(open ? Theme.cyan : Color.secondary.opacity(0.35), lineWidth: 0.7))
                }
            }
            .frame(height: 16)
            .padding(.horizontal, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(repoLabel(repo)), \(total) in \(stage.label)")
        .accessibilityHint(open ? "Folds this repo's PRs" : "Shows this repo's PRs in this column")
    }

    private func toggleStack(_ stage: PRStage, _ repo: String) {
        let key = "\(stage.rawValue)|\(repo)"
        if openStacks.contains(key) { openStacks.remove(key) } else { openStacks.insert(key) }
    }
}


struct CardFX: Equatable {
    enum Kind { case hop, confetti, bounce, sweep }
    let kind: Kind
    let start: Date
}

private struct Motion {
    var scale = 1.0, x = 0.0, y = 0.0, rot = 0.0, opacity = 1.0
}

extension View {

    func cardMotion(_ f: CardFX?) -> some View {
        self
            .keyframeAnimator(initialValue: Motion(), trigger: f?.kind == .hop || f?.kind == .confetti ? f?.start : nil) { v, m in
                v.scaleEffect(m.scale).offset(y: m.y)
            } keyframes: { _ in
                KeyframeTrack(\.y) { SpringKeyframe(-7, duration: 0.18); SpringKeyframe(0, duration: 0.35, spring: .bouncy) }
                KeyframeTrack(\.scale) { SpringKeyframe(1.06, duration: 0.18); SpringKeyframe(1, duration: 0.35) }
            }
            .keyframeAnimator(initialValue: Motion(), trigger: f?.kind == .bounce ? f?.start : nil) { v, m in
                v.scaleEffect(m.scale).offset(y: m.y)
            } keyframes: { _ in
                KeyframeTrack(\.y) {
                    CubicKeyframe(-10, duration: 0.16); CubicKeyframe(0, duration: 0.14)
                    CubicKeyframe(-4, duration: 0.12); CubicKeyframe(0, duration: 0.12)
                }
                KeyframeTrack(\.scale) {
                    CubicKeyframe(1.14, duration: 0.16); CubicKeyframe(0.94, duration: 0.14)
                    CubicKeyframe(1.04, duration: 0.12); CubicKeyframe(1, duration: 0.12)
                }
            }
            .keyframeAnimator(initialValue: Motion(), trigger: f?.kind == .sweep ? f?.start : nil) { v, m in
                v.offset(x: m.x).rotationEffect(.degrees(m.rot)).opacity(m.opacity)
            } keyframes: { _ in

                KeyframeTrack(\.opacity) { LinearKeyframe(0, duration: 0.01); LinearKeyframe(0, duration: 0.55); LinearKeyframe(1, duration: 0.2) }
                KeyframeTrack(\.x) { LinearKeyframe(90, duration: 0.56); SpringKeyframe(-4, duration: 0.3); SpringKeyframe(0, duration: 0.25) }
                KeyframeTrack(\.rot) { LinearKeyframe(-8, duration: 0.56); SpringKeyframe(0, duration: 0.45) }
            }
    }
}


struct ConfettiBurst: View {
    let start: Date
    private static let pieces: [(angle: Double, speed: Double, spin: Double, size: Double)] = (0..<14).map { i in
        let a = Double(i) / 14 * 2 * .pi + Double((i * 37) % 11) * 0.05
        return (a, 38 + Double((i * 53) % 23), Double((i * 29) % 7) - 3, 2.5 + Double(i % 3))
    }

    var body: some View {
        TimelineView(.animation) { ctx in
            Canvas { g, size in
                let t = ctx.date.timeIntervalSince(start)
                guard t >= 0, t < 1.1 else { return }
                let c = CGPoint(x: size.width / 2, y: size.height / 2)
                for (i, p) in Self.pieces.enumerated() {
                    let x = c.x + cos(p.angle) * p.speed * t
                    let y = c.y + sin(p.angle) * p.speed * t * 0.7 + 40 * t * t
                    var piece = g
                    piece.translateBy(x: x, y: y)
                    piece.rotate(by: .radians(p.spin * t * 3))
                    piece.opacity = max(0, 1 - t / 1.1)
                    let shade = i % 3 == 0 ? Theme.hud : i % 3 == 1 ? Color.green.opacity(0.5) : Theme.hud.opacity(0.6)
                    piece.fill(Path(roundedRect: CGRect(x: -p.size / 2, y: -p.size / 4, width: p.size, height: p.size / 2),
                                    cornerRadius: 0.6), with: .color(shade))
                }
            }
        }
        .frame(width: 140, height: 90)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}


struct ShatterGhost: View {
    let label: String
    let start: Date

    var body: some View {
        TimelineView(.animation) { ctx in
            let t = min(1, max(0, ctx.date.timeIntervalSince(start) / 0.8))
            ZStack {
                ForEach(0..<4, id: \.self) { i in
                    let dx = (i % 2 == 0 ? -1.0 : 1.0) * (6 + Double(i) * 3) * t * 2
                    let dy = (i < 2 ? -1.0 : 1.0) * 5 * t * 2 + 14 * t * t
                    Text(i == 0 ? label : "")
                        .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        .frame(maxWidth: .infinity, minHeight: 18)
                        .background(Theme.warning.opacity(0.18), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        .mask(alignment: .topLeading) {
                            GeometryReader { geo in
                                Rectangle()
                                    .frame(width: geo.size.width / 2, height: geo.size.height / 2)
                                    .offset(x: i % 2 == 0 ? 0 : geo.size.width / 2, y: i < 2 ? 0 : geo.size.height / 2)
                            }
                        }
                        .offset(x: dx, y: dy)
                        .rotationEffect(.degrees((i % 2 == 0 ? -1 : 1) * 14 * t))
                        .opacity(1 - t)
                }
            }
        }
        .frame(height: 18)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

extension Notification.Name {
    static let previewBoardMotion = Notification.Name("previewBoardMotion")
}
