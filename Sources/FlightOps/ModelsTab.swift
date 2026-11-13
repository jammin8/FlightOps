import SwiftUI














struct ModelsTab: View {
    @ObservedObject var m: Monitor
    @AppStorage("modelsWindow") private var window = 7
    @AppStorage("modelsGroup") private var group = Analytics.ModelGroup.setup.rawValue
    @AppStorage("modelsMetric") private var metric = Metric.ci.rawValue
    @AppStorage("modelsRole") private var role = "Review"
    @AppStorage("modelsInferredOpen") private var inferredOpen = false

    private var now: Double { Date().timeIntervalSince1970 }
    private var since: Double { now - Double(window) * 86400 }


    typealias Metric = Analytics.ModelMetric

    var body: some View {
        let by = Analytics.ModelGroup(rawValue: group) ?? .setup
        let rows = Analytics.modelStats(m.timelines, models: m.shownModels, since: since, now: now, by: by)
        let facts = Analytics.factsSummary(m.prFacts, since: since, now: now)
        VStack(alignment: .leading, spacing: Theme.gap) {
            OpsSection(title: "Best / Needs attention", note: "frozen 90-day first-ready cohorts") { decisionArea.padding(10).card() }
            Text("Exploration · recent PRs, separate from frozen comparisons").font(.headline)
            Picker("", selection: $window) {
                Text("24 h").tag(1)
                Text("7 days").tag(7)
            }
            .pickerStyle(.segmented).labelsHidden().controlSize(.small).frame(maxWidth: 160)
            Text(m.modelsStatus).font(.subheadline).foregroundStyle(.secondary)
            dials(facts)
            OpsSection(title: "Builders", note: "per PR") { builders(rows, by: by).padding(10).card() }
            OpsSection(title: "Commit-trailer attribution", note: "not model evidence") { trailerAttribution(facts).frame(maxWidth: .infinity, alignment: .leading).padding(10).card() }
            OpsSection(title: "Reviewers and rework", note: "per PR") { agents.frame(maxWidth: .infinity, alignment: .leading).padding(10).card() }
        }
    }





    private func dials(_ f: Analytics.FactsSummary) -> some View {
        let line = Analytics.coverageLine(m.factsFile, f, since: since, now: now, failedAt: m.factsFailedAt)
        let verified = f.r0["verified"] ?? 0
        let counts = Analytics.factsCounts(m.factsFile, f)
        return VStack(alignment: .leading, spacing: 6) {
            Text(line).font(.subheadline).foregroundStyle(.secondary)
            HStack(spacing: 6) {
            dial(f.prs == 0 ? nil : Double(verified) / Double(f.prs), big: counts.verified, small: "verified of " + counts.prs, tint: Theme.hud)
            VStack(spacing: 2) {
                Text(counts.prs).font(.largeTitle.weight(.semibold)).monospacedDigit().contentTransition(.numericText())
                Text("PRs first ready").font(.subheadline).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 80).padding(.vertical, 10).card()
            dial(f.heads == 0 ? nil : Double(f.headsComplete) / Double(f.heads), big: counts.complete,
                 small: "full CI history of " + counts.heads, tint: Theme.cyan)
        }
        }
        .help(line)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(line)
    }

    private func dial(_ value: Double?, big: String, small: String, tint: Color) -> some View {
        VStack(spacing: 4) {
            Gauge(value: value ?? 0) { EmptyView() } currentValueLabel: { Text(big).monospacedDigit() }
                .gaugeStyle(.accessoryCircularCapacity).tint(tint)
            Text(small).font(.subheadline).foregroundStyle(.secondary).lineLimit(1).minimumScaleFactor(0.8)
        }
        .padding(.vertical, 10).frame(maxWidth: .infinity).card()
    }




    private var decisionArea: some View {
        let source = m.shownDecision
        let shown = source.lastGood.flatMap { decision in m.bestPresentation?.decisionHash == decision.decisionHash ? m.bestPresentation : nil }
        let date = Date(timeIntervalSince1970: now)
        return VStack(alignment: .leading, spacing: 10) {
            Text(source.status(at: date)).font(.subheadline.weight(.semibold))
            if let decision = source.lastGood {
                Text("90-day first-ready population · as_of " + decision.asOf).font(.caption).foregroundStyle(.secondary)
                Text(decision.scope).font(.caption).foregroundStyle(.secondary)
                Text("Computed comparisons, not causal superiority. Upstream authenticity is not established by this envelope.")
                    .font(.caption).foregroundStyle(.secondary)
                if let shown {
                    bestContext(shown)
                } else {
                    Text("Joint setup IDs are shown verbatim. Provider, model, effort and tier are not supplied by decision/1; no legacy labels are substituted.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ForEach(decision.measures.keys.sorted(), id: \.self) { id in
                    decisionMeasure(id, measure: decision.measures[id]!, source: source, at: date, shown: shown)
                }
                if let shown { bestTraces(shown) }
                DisclosureGroup("Snapshot and integrity bindings") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Decision content digest checked; a self-hash is not source attestation.")
                        Text("Manifest/input bodies are absent; these references cannot establish their authenticity or coverage here.")
                        Text("Decision: " + decision.decisionHash)
                        Text("Manifest: " + decision.manifestHash)
                        Text("Input: " + decision.inputHash)
                        Text("Policy: " + decision.policyVersion + " · fixed reference " + decision.policy.referenceSetup.rawValue)
                        Text("Consumer display freshness: 24 h from as_of. Read time and file mtime never refresh the cohort clock.")
                        Text("Budget: \(decision.budget.estimatedDraws) estimated draws / \(decision.budget.maxResampledPrDraws); \(decision.budget.maxWallSeconds) s ceiling")
                        Text(shown == nil ? "Input: " + Monitor.decisionFile.path : "Input: VM snapshot · " + (shown?.snapshotId ?? ""))
                    }
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            } else {
                Text("No computed comparison is available. It arrives through the VM snapshot.")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func bestContext(_ shown: BestPresentation) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(shown.family.note).font(.caption.weight(.semibold))
            Text(shown.summary).font(.caption)
            if let refused = shown.refused { Text(refused).font(.caption).foregroundStyle(.secondary) }
            Text("Unknown: " + shown.unknown.joined(separator: " · ")).font(.caption).foregroundStyle(.secondary)
            DisclosureGroup("Sources") {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(shown.sources, id: \.id) { source in Text(shown.sourceLine(source)) }
                }
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
    }

    private func bestTraces(_ shown: BestPresentation) -> some View {
        DisclosureGroup("Traced PRs") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(shown.traces, id: \.pr) { trace in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(trace.pr + " · " + trace.state).font(.subheadline.weight(.semibold))
                        ForEach(trace.stages, id: \.name) { stage in
                            Text(stage.name + " · " + stage.state + ": " + stage.detail).foregroundStyle(stage.state == "unknown" ? HierarchicalShapeStyle.secondary : .primary)
                        }
                        ForEach(trace.reasons.keys.sorted(), id: \.self) { key in
                            Text((shown.measures[key] ?? key) + ": " + BestPresentation.reasonLine(trace.reasons[key] ?? [])).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .font(.caption).textSelection(.enabled)
        }
    }

    private func decisionMeasure(_ id: DecisionIdentity, measure: ModelDecision.Measure, source: DecisionSource, at date: Date, shown: BestPresentation?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().opacity(0.5)
            HStack(alignment: .firstTextBaseline) {
                Text(shown?.measures[id.rawValue] ?? id.rawValue).font(.headline)
                Spacer()
                Text(measure.unit + " · " + measure.direction.rawValue + " is better").font(.caption).foregroundStyle(.secondary)
            }
            Text("Common joint support: \(DecisionSource.number(measure.commonSupportFraction * 100))% · \(measure.supportedMix.count) supported / \(measure.uncoveredStrata.count) uncovered strata")
                .font(.caption).foregroundStyle(.secondary)
            ProgressView(value: measure.commonSupportFraction, total: 1).tint(Theme.cyan)
                .accessibilityLabel("Common joint support \(DecisionSource.number(measure.commonSupportFraction * 100)) percent")
            if !measure.unmetGates.isEmpty {
                Text("Computed blockers: " + measure.unmetGates.sorted().map(DecisionSource.gateLabel).joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.warning)
            }
            ForEach(measure.family, id: \.self) { id in
                decisionGroup(id, group: measure.groups[id]!, population: measure.populations[id]!, measure: measure, source: source, at: date, shown: shown)
            }
            DisclosureGroup("Measure support, calibration and contrasts") {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(measure.gates.keys.sorted(), id: \.self) { gate in
                        Text((measure.gates[gate] == true ? "Met: " : "Unmet: ") + DecisionSource.gateLabel(gate))
                    }
                    Text("Calibration: " + measure.calibration.status + " · " + (measure.calibration.reason ?? (measure.calibration.pass ? "computed false-flag criterion passed" : "computed false-flag criterion failed")))
                    if let scenarios = measure.calibration.scenarios {
                        ForEach(scenarios, id: \.scenario) { scenario in
                            Text("\(scenario.scenario): \(scenario.falseFlags)/\(scenario.simulations) false flags; simultaneous upper \(DecisionSource.number(scenario.upper95Simultaneous))")
                        }
                    }
                    ForEach(Array(measure.supportedMix.enumerated()), id: \.offset) { _, cell in
                        Text(cell.stratum.map(\.rawValue).joined(separator: " · ") + ": normalized weight " + DecisionSource.number(cell.normalizedWeight))
                    }
                    ForEach(Array(measure.uncoveredStrata.enumerated()), id: \.offset) { _, cell in
                        Text("Uncovered: " + cell.map(\.rawValue).joined(separator: " · "))
                    }
                    ForEach(Array(measure.comparisons.enumerated()), id: \.offset) { _, comparison in
                        Text(comparison.a.rawValue + " vs " + comparison.b.rawValue + ": simultaneous 95% " + DecisionSource.interval(comparison.simultaneous95Interval.map(Optional.some)) + " " + comparison.scale)
                    }
                    if !measure.excludedGroups.isEmpty { Text("Outside comparison family: " + measure.excludedGroups.map(\.rawValue).joined(separator: ", ")) }
                }
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
    }

    private func decisionGroup(_ id: DecisionIdentity, group: ModelDecision.Group, population: ModelDecision.Population,
                               measure: ModelDecision.Measure, source: DecisionSource, at date: Date, shown: BestPresentation?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text((shown?.setups[id.rawValue]?.label ?? "Joint setup ID: " + id.rawValue) + (id == measure.referenceSetup ? " · fixed reference" : ""))
                .font(.subheadline.weight(.semibold)).textSelection(.enabled)
            if let line = shown?.setupLine(id.rawValue) { Text(line).font(.caption).foregroundStyle(.secondary) }
            Text(source.label(group, at: date)).font(.subheadline)
                .foregroundStyle(decisionTint(group, source: source, at: date))
            Text("\(measure.independentCounts[id]!) independent PRs on common support · \(group.coverage.included) measured / \(group.coverage.knownDenominator) known · \(group.coverage.possibleExtra) possible unknown · \(population.missingIds.count) known missing" + (population.outsideSupport > 0 ? " · \(population.outsideSupport) outside common support" : ""))
                .font(.caption).foregroundStyle(.secondary)
            if group.coverage.knownDenominator + group.coverage.possibleExtra > 0 {
                ProgressView(value: group.coverage.minimumFraction, total: 1).tint(Theme.gunmetal)
                Text(DecisionSource.coverageLine(group.coverage, everyCell: source.lastGood?.everyCellCoverage == true))
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Coverage unavailable: no known or possible denominator").font(.caption).foregroundStyle(.secondary)
            }
            if let estimate = group.estimate, let interval = group.simultaneous95Interval {
                let scale = measure.kind == .rate ? "rate" : "ln(" + measure.unit + ")"
                Text("Estimate \(DecisionSource.number(estimate)) · simultaneous 95% " + DecisionSource.interval(interval.map(Optional.some)) + " " + scale)
                    .font(.caption).foregroundStyle(.secondary)
                if let domain = DecisionSource.domain(measure), let positions = DecisionSource.positions(group, domain: domain) {
                    GeometryReader { geometry in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Theme.gunmetal.opacity(0.3)).frame(height: 3)
                            Capsule().fill(Theme.cyan).frame(width: max(1, geometry.size.width * CGFloat(positions[2] - positions[0])), height: 4)
                                .offset(x: geometry.size.width * CGFloat(positions[0]))
                            Circle().fill(Theme.cyan).frame(width: 6, height: 6).offset(x: geometry.size.width * CGFloat(positions[1]) - 3)
                        }
                    }
                    .frame(height: 12).accessibilityHidden(true)
                    HStack {
                        Text(DecisionSource.number(domain.lowerBound))
                        Spacer()
                        Text(DecisionSource.number(domain.upperBound) + " " + scale)
                    }.font(.caption2).foregroundStyle(.secondary)
                }
                Text("Worst-case interval: " + DecisionSource.interval(group.worstCaseInterval) + " " + scale)
                    .font(.caption).foregroundStyle(.secondary)
            } else { Text("Intervals not computed").font(.caption).foregroundStyle(.secondary) }
            if !group.unmetGates.isEmpty {
                Text("Blockers: " + group.unmetGates.sorted().map(DecisionSource.gateLabel).joined(separator: " · "))
                    .font(.caption).foregroundStyle(Theme.warning)
            }
            if !population.reasons.isEmpty {
                Text("Missing evidence: " + population.reasons.keys.sorted().map { "\($0): \(population.reasons[$0]!)" }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(8).card()
    }

    private func decisionTint(_ group: ModelDecision.Group, source: DecisionSource, at date: Date) -> Color {
        guard source.isCurrent(at: date) else { return .secondary }
        switch group.status {
        case .best: return Theme.hud
        case .attention: return Theme.warning
        case .overlap: return Theme.cyan
        case .insufficient: return .secondary
        }
    }

    @ViewBuilder private func trailerAttribution(_ f: Analytics.FactsSummary) -> some View {
        if f.trailerLabels.isEmpty {
            Text(m.factsFile?.prs == nil ? "GitHub receipts not read yet" : "No PR in this window has a first-ready commit in the available receipts").font(.subheadline).foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                Text(Analytics.EvidenceField.trailerLabels.label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.warning)
                Text("A Co-Authored-By trailer is the commit's own claim, not proof of which model built the PR. Labels are PR counts in name order and never compare models.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(Analytics.trailerLines(f), id: \.self) { Text($0).font(.subheadline).monospacedDigit() }
                Divider()
                Text(Analytics.EvidenceField.sameHeadCI.label).font(.subheadline.weight(.semibold))
                Text("First try of each PR's first-ready commit, all PRs together, by how that commit was found. Not a model quality rate.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(Analytics.firstTryLines(f), id: \.self) { Text($0).font(.subheadline).monospacedDigit() }
            }
            .help(Analytics.fitnessLegend)
        }
    }





    private func builders(_ rows: [Analytics.ModelStat], by: Analytics.ModelGroup) -> some View {
        let exact = rows.filter { !$0.inferred }
        let inferred = rows.filter(\.inferred)
        let unknown = inferred.filter { $0.key == "unknown" }.reduce(0) { $0 + $1.prs }
        let family = inferred.reduce(0) { $0 + $1.prs } - unknown
        let total = rows.reduce(0) { $0 + $1.prs }
        let parts: [String?] = [family > 0 ? "\(family) of \(total) family only" : nil, unknown > 0 ? "\(unknown) of \(total) unknown" : nil]
        let mt = Metric(rawValue: metric) ?? .ci
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Picker("", selection: $metric) { ForEach(Metric.allCases, id: \.rawValue) { Text($0.rawValue).tag($0.rawValue) } }
                    .pickerStyle(.segmented).labelsHidden().controlSize(.small)
                Picker("", selection: $group) { ForEach(Analytics.ModelGroup.allCases, id: \.rawValue) { Text($0.rawValue).tag($0.rawValue) } }
                    .labelsHidden().controlSize(.small).frame(width: 96).help("Group by")
            }
            if rows.isEmpty {
                Text("No PRs in this window").font(.subheadline).foregroundStyle(.secondary)
            } else if exact.isEmpty {
                Text(m.liveModels == nil ? "No builder seen on its PR's head branch yet" : "Orchestrator records don't name builders").font(.subheadline).foregroundStyle(.secondary)
            } else {
                bars(exact, metric: mt)
            }
            if !inferred.isEmpty {

                Button { withAnimation(.easeInOut(duration: 0.18)) { inferredOpen.toggle() } } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.right").font(.caption2.weight(.semibold)).rotationEffect(.degrees(inferredOpen ? 90 : 0))
                        Text("Not exact · " + parts.compactMap { $0 }.joined(separator: " · ")).font(.caption)
                    }
                    .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Builders known only from a branch prefix or an old report, or not at all: not compared")
                if inferredOpen {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(Analytics.attributionLines(rows), id: \.self) { Text($0).font(.subheadline).monospacedDigit() }
                        Text("A branch prefix or an old report doesn't prove the builder. PR counts in name order, with no outcomes.").font(.caption)
                    }
                    .foregroundStyle(.secondary)
                }
            }
        }
    }



    private func bars(_ rows: [Analytics.ModelStat], metric: Metric) -> some View {
        let values: [Double?] = rows.map { r in
            switch metric {
            case .ci: return r.ciPass
            case .rework: return r.rework
            case .tokens: return r.tokensPerPR
            case .merge: return r.mergeAvg
            }
        }
        let top = max(values.compactMap { $0 }.max() ?? 1, 1e-9)
        let share = metric == .ci || metric == .rework
        let tint: Color = metric == .ci ? Theme.hud : metric == .rework ? Theme.caution : Theme.cyan
        return VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(rows.enumerated()), id: \.element.id) { i, r in
                let v = values[i]
                let count = r.denominator(metric)
                let small = count < 3
                HStack(spacing: 8) {
                    Text(r.name)
                        .font(.subheadline.weight(.medium)).lineLimit(2).fixedSize(horizontal: false, vertical: true).frame(width: 170, alignment: .leading).help(r.name)
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.primary.opacity(0.08))
                            Capsule().fill(tint.opacity(small ? 0.35 : 0.85))
                                .frame(width: max(0, g.size.width * CGFloat((v ?? 0) / (share ? 1 : top))))
                        }
                    }
                    .frame(height: 8)
                    Text(v.map { label($0, metric) } ?? "–").font(.subheadline).monospacedDigit().frame(width: 48, alignment: .trailing)
                    Text("\(count)").font(.caption).foregroundStyle(.secondary).monospacedDigit().frame(width: 22, alignment: .trailing)
                        .help(metric == .tokens ? "Averaged over \(count) of \(r.prs) PRs with fully known usage" : "\(count) of \(r.prs) PRs")
                }
                .foregroundStyle(small ? HierarchicalShapeStyle.secondary : .primary)
                .accessibilityElement(children: .combine)
            }
            HStack {
                Text(metric == .ci ? "Never sent to fixing after CI (conflicts too): sampled, doesn't rank"
                     : metric == .rework ? "Sampled at each refresh: describes, doesn't rank" : metric == .tokens ? "No cache reads" : "Opened to merged")
                Spacer()
                Text("PRs")
            }
            .font(.caption2).foregroundStyle(.secondary)
        }
    }

    private func label(_ v: Double, _ metric: Metric) -> String {
        switch metric {
        case .ci, .rework: return pct(v)
        case .tokens: return compactCount(v)
        case .merge: return duration(Int(v))
        }
    }




    @ViewBuilder private var agents: some View {
        let reworks = role == "Rework"
        let live = m.liveModels != nil
        let linked = live && role == "Linked"
        let rows = Analytics.agentStats(m.timelines, models: m.shownModels, since: since, now: now, reworks: reworks, linked: linked)
        let top = max(rows.compactMap(\.tokensPerPR).max() ?? 1, 1)
        VStack(alignment: .leading, spacing: 8) {
            Text(live ? "Review roles are title hints. Title hints alone allocate no tokens. Settings are as requested. ~ is an estimate from one Orchestrator link; exclusive use is unproven."
                 : "Historical usage only; freshness unknown. Runs without IDs counted as reported; shared usage excluded.").font(.caption).foregroundStyle(.secondary)
            Picker("", selection: $role) {
                Text("Review").tag("Review")
                if live { Text("Linked").tag("Linked") } else { Text("Rework").tag("Rework") }
            }
            .pickerStyle(.segmented).labelsHidden().controlSize(.small).frame(maxWidth: 200)
            if rows.isEmpty {
                Text(live && reworks ? "Rework is unknown from Orchestrator records" : linked ? "No linked runs in this window" : reworks ? "No readable rework runs in this window" : "No readable review runs in this window").font(.subheadline).foregroundStyle(.secondary)
            } else {
                ForEach(rows) { r in
                    HStack(spacing: 8) {
                        Text(r.name).font(.subheadline.weight(.medium)).lineLimit(2).fixedSize(horizontal: false, vertical: true).frame(width: 170, alignment: .leading).help(r.name)
                        GeometryReader { g in
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.primary.opacity(0.08))
                                Capsule().fill(Theme.cyan.opacity(r.legacy ? 0.3 : 0.8))
                                    .frame(width: g.size.width * CGFloat((r.tokensPerPR ?? 0) / top))
                            }
                        }
                        .frame(height: 8)
                        Text(r.tokensText).font(.subheadline).monospacedDigit().frame(width: 48, alignment: .trailing)
                        Text(reworks ? pct(r.mergedShare) : r.runsPerPR.map { String(format: "%.1f×", $0) } ?? "–")
                            .font(.caption).foregroundStyle(.secondary).monospacedDigit().frame(width: 34, alignment: .trailing)
                            .help(reworks ? "Share of these PRs that merged" : "Review runs per PR")
                    }
                    .foregroundStyle(r.legacy ? HierarchicalShapeStyle.secondary : .primary)
                    .help(r.legacy ? "Old report: tokens were counted per folder, not per run" : "\(r.prs) PRs; tokens averaged over \(r.tokenPRs)")
                    .accessibilityElement(children: .combine)
                }
                HStack { Text("Tokens per PR"); Spacer(); Text(reworks ? "merged" : "runs/PR") }.font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func pct(_ x: Double) -> String { "\(Int((x * 100).rounded()))%" }
}
