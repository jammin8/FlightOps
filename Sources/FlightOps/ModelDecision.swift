import Foundation

struct ModelDecision: Decodable {
    static let schemaName = "flightops-decision/1"
    static let policyName = "joint-strata-max-bootstrap/2"
    static let historicalPolicyName = "joint-strata-max-bootstrap/1"
    static let supportFloor = 0.7 - 1e-12
    static func supportMass(_ weights: [Double]) -> Double { weights.sorted().reduce(0, +) }
    static let requiredGates: Set<DecisionIdentity> = ["inventory_complete", "two_exact_reported_setups", "independent_prs_at_least_20_each",
        "common_support_at_least_70_percent", "verified_measure_representation", "calibration_false_flags_at_most_10_percent", "precision"]

    let schema: String
    let policyVersion: String
    let decisionHash: String
    let manifestHash: String
    let inputHash: String
    let asOf: String
    let windowDays: Int
    let scope: String
    let qualification: String
    let policy: Policy
    let measures: DecisionMap<Measure>
    let budget: Budget

    struct Policy: Decodable {
        let version: String
        let seed: UInt64
        let bootstrapReplicates: Int
        let calibrationSimulations: Int
        let family: [DecisionIdentity]
        let referenceSetup: DecisionIdentity
        let referenceMix: [Cell]
    }
    struct Cell: Decodable {
        let repo: String
        let task: DecisionIdentity
        let size: String
        let weight: Double
        var stratum: [DecisionIdentity] { [DecisionIdentity(repo), task, DecisionIdentity(size)] }
    }
    struct Budget: Decodable {
        let maxResampledPrDraws: Int
        let estimatedDraws: Int
        let maxWallSeconds: Int
    }
    enum Kind: String, Decodable { case rate, amount }
    enum Direction: String, Decodable { case higher, lower }
    enum Status: String, Decodable {
        case best = "Best", attention = "NeedsAttention", overlap = "NoClearDifference", insufficient = "InsufficientEvidence"
        var label: String {
            switch self {
            case .best: return "Best · supported difference"
            case .attention: return "Needs attention · supported deficit"
            case .overlap: return "Measured overlap · no clear difference"
            case .insufficient: return "Insufficient evidence"
            }
        }
    }
    struct Coverage: Decodable, Equatable {
        let included: Int
        let knownDenominator: Int
        let possibleExtra: Int
        let minimumFraction: Double
        let thresholdPass: Bool
    }
    struct Population: Decodable {
        let eligibilityIds: [DecisionIdentity]
        let excluded: [Exclusion]
        let denominatorIds: [DecisionIdentity]
        let potentialDenominatorIds: [DecisionIdentity]
        let includedIds: [DecisionIdentity]
        let missingIds: [DecisionIdentity]
        let reasons: DecisionMap<Int>
        let verifiedNumerator: Double?
        var outsideSupport: Int { excluded.filter { $0.reason == "outside_common_support" }.count }
    }
    struct Exclusion: Decodable { let prId: DecisionIdentity; let reason: String; let firstReadyAt: String? }
    struct Mix: Decodable { let stratum: [DecisionIdentity]; let normalizedWeight: Double }
    struct Comparison: Decodable { let a: DecisionIdentity; let b: DecisionIdentity; let scale: String; let simultaneous95Interval: [Double] }
    struct Bootstrap: Decodable {
        let maxStatisticCritical: Double
        let bootstrapReplicates: Int
        let bootstrapMaximaHash: String
    }
    struct Effect: Decodable {
        let bestSeparatesEveryOther: Bool
        let worstCaseBestSeparatesEveryOther: Bool
        let deficitLowerBound: Double?
        let worstCaseDeficitLowerBound: Double?
        let deficitThreshold: Double
        let scale: String
    }
    struct Group: Decodable {
        let calls: [String]
        let unmetGates: [DecisionIdentity]
        let status: Status
        let coverage: Coverage
        let gates: DecisionMap<Bool>
        let estimate: Double?
        let scale: String?
        let simultaneous95Interval: [Double]?
        let worstCaseInterval: [Double?]?
        let coverageRoute: String?
        let effectChecks: Effect?
        var hasInference: Bool { estimate != nil || scale != nil || simultaneous95Interval != nil || worstCaseInterval != nil || effectChecks != nil }
    }
    struct Calibration: Decodable {
        let status: String
        let pass: Bool
        let reason: String?
        let seed: UInt64?
        let familySize: Int?
        let referenceSetup: DecisionIdentity?
        let shape: DecisionMap<DecisionMap<Int>>?
        let missingShape: DecisionMap<DecisionMap<Int>>?
        let scenarios: [Scenario]?
        let rule: String?
        let calibrationHash: String?
    }
    struct Scenario: Decodable {
        let scenario: String
        let simulations: Int
        let falseFlags: Int
        let falseFlagFraction: Double
        let upper95Simultaneous: Double
        let pass: Bool
        let failureCases: [FailureCase]
    }
    struct FailureCase: Decodable { let simulation: Int; let flags: [DecisionIdentity] }
    struct Measure: Decodable {
        let kind: Kind
        let unit: String
        let direction: Direction
        let family: [DecisionIdentity]
        let referenceSetup: DecisionIdentity
        let independentCounts: DecisionMap<Int>
        let populations: DecisionMap<Population>
        let gates: DecisionMap<Bool>
        let commonSupportFraction: Double
        let supportedMix: [Mix]
        let uncoveredStrata: [[DecisionIdentity]]
        let groups: DecisionMap<Group>
        let calibration: Calibration
        let excludedGroups: [DecisionIdentity]
        let comparisons: [Comparison]
        let coverage: DecisionMap<Coverage>
        let coverageThresholdPass: Bool
        let bootstrap: Bootstrap?
        let unmetGates: [DecisionIdentity]
    }

    var date: Date { Self.timestamp(asOf)! }
    var everyCellCoverage: Bool { policyVersion == Self.policyName }
    var supersededPolicy: Bool { policyVersion == Self.historicalPolicyName }
    static func timestamp(_ text: String) -> Date? {
        guard text.hasSuffix("Z"), text.count <= 80 else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let value = formatter.date(from: text) { return value }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: text)
    }
    static func isDigest(_ text: String) -> Bool {
        text.utf8.count == 64 && text.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
    static func finite(_ value: Double) -> Bool { value.isFinite && abs(value) <= 1e100 }
    static func fraction(_ value: Double) -> Bool { finite(value) && (0...1).contains(value) }
    static func text(_ value: String) -> Bool { !value.isEmpty && value.unicodeScalars.count <= 200 }
    static func unique(_ values: [String]) -> Bool { Set(values.map { DecisionIdentity($0) }).count == values.count }
    static func unique(_ values: [DecisionIdentity]) -> Bool { Set(values).count == values.count }
    static func text(_ value: DecisionIdentity) -> Bool { text(value.rawValue) }
    static func analyticClose(_ a: Double, _ b: Double) -> Bool { abs(a - b) <= 1e-12 * max(1, abs(a), abs(b)) }
    static func close(_ a: Double, _ b: Double) -> Bool { abs(a - b) <= 1e-9 * max(1, abs(a), abs(b)) }

    static func decode(_ data: Data, now: Date) throws -> ModelDecision {
        var parser = try DecisionJSON.Parser(data)
        let raw = try parser.parse()
        guard let fields = raw.object else { throw DecisionError.invalid("Decision must be an object") }
        try decisionRequire(Set(fields.keys) == ["schema", "policy_version", "decision_hash", "manifest_hash", "input_hash", "as_of", "window_days", "scope", "qualification", "policy", "measures", "budget"], "Unknown or missing decision fields")
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .custom { DecisionCodingKey.decoded($0) }
        let decision = try decoder.decode(ModelDecision.self, from: Data(raw.typedTransport.canonical))
        try decisionRequire(decision.schema == schemaName && [policyName, historicalPolicyName].contains(decision.policyVersion) && decision.policy.version == decision.policyVersion, "Unsupported decision schema or policy")
        try decisionRequire(isDigest(decision.decisionHash) && isDigest(decision.manifestHash) && isDigest(decision.inputHash), "Invalid decision hash reference")
        try decisionRequire(try raw.digest(removing: "decision_hash") == decision.decisionHash, "Decision integrity hash mismatch")
        try decisionRequire(timestamp(decision.asOf) != nil && now.timeIntervalSince1970.isFinite, "Invalid as_of or consumer clock")
        try decisionRequire(decision.date <= now, "Decision as_of is in the future")
        try decisionRequire(decision.windowDays == 90 && decision.scope == "among comparable setups on common joint-stratum support", "Unsupported decision population or scope")
        try decisionRequire(decision.qualification == "normalized assertions only; upstream authenticity not established by this kernel", "Unsupported source qualification representation")
        try decision.validate(raw: fields)
        return decision
    }

    private func validate(raw: [DecisionIdentity: DecisionJSON]) throws {
        try fields(raw["policy"], required: ["version", "seed", "bootstrap_replicates", "calibration_simulations", "family", "reference_setup", "reference_mix"])
        try fields(raw["budget"], required: ["max_resampled_pr_draws", "estimated_draws", "max_wall_seconds"])
        let ids = Set(policy.family)
        try decisionRequire((2...8).contains(ids.count) && Self.unique(policy.family) && policy.family.allSatisfy(Self.text) && ids.contains(policy.referenceSetup), "Invalid frozen setup family/reference")
        try decisionRequire(policy.seed <= UInt64(Int64.max) && (199...999).contains(policy.bootstrapReplicates) && (120...400).contains(policy.calibrationSimulations), "Invalid frozen computation policy")
        try decisionRequire((1...24).contains(policy.referenceMix.count), "Invalid reference mix")
        try decisionRequire(Set(policy.referenceMix.map(\.stratum)).count == policy.referenceMix.count, "Duplicate joint strata")
        for cell in policy.referenceMix {
            try decisionRequire(Self.text(cell.repo) && cell.repo.range(of: "^[a-z0-9_.-]+/[a-z0-9_.-]+$", options: .regularExpression) != nil && Self.text(cell.task) && ["xs", "s", "m", "l", "xl"].contains(cell.size) && Self.finite(cell.weight) && cell.weight > 0, "Invalid joint stratum")
        }
        try decisionRequire(Self.close(policy.referenceMix.reduce(0) { $0 + $1.weight }, 1), "Reference weights do not sum to one")
        try decisionRequire(budget.maxResampledPrDraws == 45_000_000 && budget.maxWallSeconds == 45 && budget.estimatedDraws >= 0, "Invalid computation budget")
        try decisionRequire((1...8).contains(measures.count), "Empty or excessive measure inventory")
        guard let rawMeasures = raw["measures"]?.object else { throw DecisionError.invalid("Missing measures") }
        var expectedDraws = 0
        for (id, measure) in measures {
            try decisionRequire(Self.text(id) && Self.text(measure.unit) && measure.family == policy.family && measure.referenceSetup == policy.referenceSetup, "Measure family/reference mismatch")
            try decisionRequire(Set(measure.groups.keys) == ids && Set(measure.populations.keys) == ids && Set(measure.independentCounts.keys) == ids && Set(measure.coverage.keys) == ids, "Incomplete setup inventory")
            try decisionRequire(Set(measure.gates.keys) == Self.requiredGates && Set(measure.unmetGates) == Set(measure.gates.filter { !$0.value }.map(\.key)) && Self.unique(measure.unmetGates), "Missing or inconsistent computed gates")
            try decisionRequire(Self.fraction(measure.commonSupportFraction) && Self.unique(measure.excludedGroups) && Set(measure.excludedGroups).isDisjoint(with: ids), "Invalid support or excluded setup list")
            let supported = Set(measure.supportedMix.map(\.stratum))
            let reference = Set(policy.referenceMix.map(\.stratum))
            let uncovered = Set(measure.uncoveredStrata)
            try decisionRequire(supported.count == measure.supportedMix.count && uncovered.count == measure.uncoveredStrata.count && supported.isDisjoint(with: uncovered) && supported.union(uncovered) == reference, "Joint support does not match frozen mix")
            let weights = policy.referenceMix.filter { supported.contains($0.stratum) }.map(\.weight)
            let mass = everyCellCoverage ? Self.supportMass(weights) : weights.reduce(0, +)
            try decisionRequire(Self.close(mass, measure.commonSupportFraction), "Support fraction mismatch")
            for mix in measure.supportedMix {
                let weight = policy.referenceMix.first { $0.stratum == mix.stratum }!.weight
                try decisionRequire(mass > 0 && Self.fraction(mix.normalizedWeight) && Self.close(mix.normalizedWeight, weight / mass), "Invalid supported weight")
            }
            try decisionRequire(measure.gates["common_support_at_least_70_percent"] == (mass >= (everyCellCoverage ? Self.supportFloor : 0.7)) && measure.gates["independent_prs_at_least_20_each"] == measure.independentCounts.values.allSatisfy { $0 >= 20 }, "Support/sample gate contradiction")
            let coveragePass = measure.coverage.values.allSatisfy(\.thresholdPass)
            try decisionRequire(measure.coverageThresholdPass == coveragePass, "Coverage gate contradiction")
            guard let rawMeasure = rawMeasures[id]?.object, let rawGroups = rawMeasure["groups"]?.object else { throw DecisionError.invalid("Missing measure/group evidence") }
            try fields(rawMeasures[id], required: ["kind", "unit", "direction", "family", "reference_setup", "independent_counts", "populations", "gates", "common_support_fraction", "supported_mix", "uncovered_strata", "groups", "calibration", "excluded_groups", "comparisons", "coverage", "coverage_threshold_pass", "unmet_gates"], optional: ["bootstrap"])
            let prerequisites = measure.gates.filter { !["precision", "calibration_false_flags_at_most_10_percent"].contains($0.key) }.values.allSatisfy { $0 }
            try validateCalibration(measure, raw: rawMeasure)
            let hasInference = measure.groups.values.contains(where: \.hasInference)
            try decisionRequire(measure.groups.values.allSatisfy { $0.hasInference == hasInference } && (measure.bootstrap != nil) == hasInference, "Partial/skipped inference representation")
            if !hasInference { try decisionRequire(measure.gates["precision"] == false && measure.calibration.status == "not_computed", "Skipped inference cannot pass precision/calibration") }
            if hasInference { try decisionRequire(measure.gates.filter { !["precision", "calibration_false_flags_at_most_10_percent"].contains($0.key) }.values.allSatisfy { $0 }, "Inference despite failed prerequisites") }
            if let bootstrap = measure.bootstrap {
                try fields(rawMeasure["bootstrap"], required: ["max_statistic_critical", "bootstrap_replicates", "bootstrap_maxima_hash"])
                try decisionRequire(bootstrap.bootstrapReplicates == policy.bootstrapReplicates && Self.finite(bootstrap.maxStatisticCritical) && bootstrap.maxStatisticCritical > 0 && Self.isDigest(bootstrap.bootstrapMaximaHash), "Invalid bootstrap representation")
            }
            var knownIDs: Set<DecisionIdentity> = []
            let possibleIDs = Set(measure.populations.values.flatMap(\.potentialDenominatorIds))
            for g in policy.family {
                let pop = measure.populations[g]!, group = measure.groups[g]!, coverage = measure.coverage[g]!
                try fields(rawMeasure["populations"]?.object?[g], required: ["eligibility_ids", "excluded", "denominator_ids", "potential_denominator_ids", "included_ids", "missing_ids", "reasons", "verified_numerator"])
                try decisionRequire(knownIDs.isDisjoint(with: pop.denominatorIds) && possibleIDs.isDisjoint(with: pop.denominatorIds), "Conflicting known/possible PR allocation")
                knownIDs.formUnion(pop.denominatorIds)
                try validatePopulation(pop, coverage: coverage, kind: measure.kind)
                try decisionRequire(group.coverage == coverage && (0...coverage.included).contains(measure.independentCounts[g]!), "Inconsistent independent count/coverage")
                try validateGroup(group, id: g, measure: measure, raw: rawGroups[g]!)
            }
            try decisionRequire(knownIDs.count <= 5000 && knownIDs.union(possibleIDs).count <= 5000, "Population exceeds frozen row budget")
            if prerequisites {
                let draws = measure.independentCounts.values.reduce(0, +) * policy.bootstrapReplicates * (1 + (measure.kind == .rate ? 4 : 3) * policy.calibrationSimulations)
                let (sum, overflow) = expectedDraws.addingReportingOverflow(draws)
                try decisionRequire(!overflow, "Computation accounting overflow")
                expectedDraws = sum
            }
            try decisionRequire(measure.comparisons.count == (hasInference ? ids.count * (ids.count - 1) / 2 : 0), "Missing or skipped pairwise comparisons")
            var pairs: Set<Set<DecisionIdentity>> = []
            for comparison in measure.comparisons {
                let pair: Set<DecisionIdentity> = [comparison.a, comparison.b]
                try decisionRequire(pair.count == 2 && pair.isSubset(of: ids) && pairs.insert(pair).inserted && comparison.scale == (measure.kind == .rate ? "rate_difference" : "log_ratio"), "Invalid pairwise comparison")
                try validateInterval(comparison.simultaneous95Interval)
                let a = measure.groups[comparison.a]!, b = measure.groups[comparison.b]!
                let point = a.estimate! - b.estimate!
                let low = comparison.simultaneous95Interval[0], high = comparison.simultaneous95Interval[1]
                try decisionRequire((low <= point || Self.analyticClose(low, point)) && (point <= high || Self.analyticClose(point, high)), "Contrast excludes its published point difference")
                if measure.kind == .rate {
                    let envelopeLow = a.simultaneous95Interval![0] - b.simultaneous95Interval![1]
                    let envelopeHigh = a.simultaneous95Interval![1] - b.simultaneous95Interval![0]
                    try decisionRequire((low <= envelopeLow || Self.analyticClose(low, envelopeLow)) && (high >= envelopeHigh || Self.analyticClose(high, envelopeHigh)), "Rate contrast excludes published group interval envelope")
                }
            }
            if hasInference { try validatePublishedEffects(measure) }
        }
        try decisionRequire(budget.estimatedDraws == expectedDraws, "Computation accounting mismatch")
    }

    private func fields(_ value: DecisionJSON?, required: Set<DecisionIdentity>, optional: Set<DecisionIdentity> = []) throws {
        guard let keys = value?.object?.keys else { throw DecisionError.invalid("Missing required decision object") }
        let actual = Set(keys)
        try decisionRequire(required.isSubset(of: actual) && actual.isSubset(of: required.union(optional)), "Missing or unknown decision evidence fields")
    }

    private func validatePublishedEffects(_ measure: Measure) throws {
        let plain = measure.groups.mapValues { $0.simultaneous95Interval!.map(Optional.some) }
        let worst = measure.groups.mapValues { $0.worstCaseInterval! }
        func better(_ a: DecisionIdentity, _ b: DecisionIdentity, bounds: [DecisionIdentity: [Double?]]) -> Bool {
            if measure.direction == .higher {
                guard let low = bounds[a]![0], let high = bounds[b]![1] else { return false }
                return low > high
            }
            guard let high = bounds[a]![1], let low = bounds[b]![0] else { return false }
            return high < low
        }
        var precise = true
        for g in measure.family {
            let info = measure.groups[g]!, effect = info.effectChecks!, bounds = info.simultaneous95Interval!
            precise = precise && (measure.kind == .rate ? (bounds[1] - bounds[0]) / 2 <= 0.2 : max(info.estimate! - bounds[0], bounds[1] - info.estimate!) <= log(2))
            try decisionRequire(effect.bestSeparatesEveryOther == measure.family.filter { $0 != g }.allSatisfy { better(g, $0, bounds: plain) } && effect.worstCaseBestSeparatesEveryOther == measure.family.filter { $0 != g }.allSatisfy { better(g, $0, bounds: worst) }, "Separation flags contradict published intervals")
            if g != policy.referenceSetup {
                let comparison = measure.comparisons.first { Set([$0.a, $0.b]) == Set([g, policy.referenceSetup]) }!
                let delta = comparison.a == policy.referenceSetup ? comparison.simultaneous95Interval : [-comparison.simultaneous95Interval[1], -comparison.simultaneous95Interval[0]]
                let deficit = measure.direction == .higher ? delta[0] : -delta[1]
                try decisionRequire(Self.close(effect.deficitLowerBound!, deficit), "Deficit contradicts published contrast")
                let low = measure.direction == .higher ? worst[policy.referenceSetup]![0] : worst[g]![0]
                let high = measure.direction == .higher ? worst[g]![1] : worst[policy.referenceSetup]![1]
                if let low, let high {
                    try decisionRequire(effect.worstCaseDeficitLowerBound.map { Self.close($0, low - high) } == true, "Deficit contradicts worst-case intervals")
                } else { try decisionRequire(effect.worstCaseDeficitLowerBound == nil, "Unbounded deficit must stay unknown") }
            }
        }
        if measure.kind == .amount {
            for comparison in measure.comparisons {
                let point = measure.groups[comparison.a]!.estimate! - measure.groups[comparison.b]!.estimate!
                precise = precise && max(point - comparison.simultaneous95Interval[0], comparison.simultaneous95Interval[1] - point) <= log(2)
            }
        }
        try decisionRequire(measure.gates["precision"] == precise, "Precision flag contradicts published intervals")
    }

    private func validatePopulation(_ pop: Population, coverage: Coverage, kind: Kind) throws {
        let lists = [pop.eligibilityIds, pop.denominatorIds, pop.potentialDenominatorIds, pop.includedIds, pop.missingIds]
        for list in lists {
            try decisionRequire(list.count <= 5000 && Self.unique(list) && list.allSatisfy { $0.rawValue.range(of: "^[a-z0-9_.-]+/[a-z0-9_.-]+#[1-9][0-9]*$", options: .regularExpression) != nil }, "Invalid/duplicate PR population identity")
        }
        let known = Set(pop.denominatorIds), possible = Set(pop.potentialDenominatorIds), included = Set(pop.includedIds)
        try decisionRequire(known.isDisjoint(with: possible) && Set(pop.eligibilityIds) == known.union(possible) && included.isSubset(of: known) && Set(pop.missingIds) == known.subtracting(included), "Inconsistent missing/possible denominators")
        try decisionRequire(coverage.included == included.count && coverage.knownDenominator == known.count && coverage.possibleExtra == possible.count && Self.fraction(coverage.minimumFraction), "Invalid coverage counts")
        let denominator = known.count + possible.count
        let fraction = denominator == 0 ? 0 : Double(included.count) / Double(denominator)
        let rawPass = denominator > 0 && fraction >= 0.9
        try decisionRequire(Self.close(coverage.minimumFraction, fraction) && (everyCellCoverage ? rawPass || !coverage.thresholdPass : coverage.thresholdPass == rawPass), "Inconsistent coverage fraction")
        try decisionRequire(pop.reasons.allSatisfy { !$0.key.isEmpty && $0.key.rawValue.unicodeScalars.count <= 512 && (1...5000).contains($0.value) } && pop.excluded.count <= 5000, "Invalid missingness reasons")
        try decisionRequire(Set(pop.excluded.map(\.prId)).count == pop.excluded.count && Set(pop.excluded.map(\.prId)).isDisjoint(with: pop.eligibilityIds) && pop.excluded.allSatisfy { Self.text($0.reason) && !$0.prId.isEmpty && ($0.firstReadyAt == nil || Self.timestamp($0.firstReadyAt!) != nil) }, "Invalid exclusion evidence")
        if kind == .rate {
            guard let numerator = pop.verifiedNumerator else { throw DecisionError.invalid("Missing verified rate numerator") }
            try decisionRequire(Self.finite(numerator) && numerator >= 0 && numerator <= Double(included.count) && numerator.rounded() == numerator, "Invalid rate numerator")
        } else { try decisionRequire(pop.verifiedNumerator == nil, "Amount has rate numerator") }
    }

    private func validateInterval(_ bounds: [Double]) throws {
        try decisionRequire(bounds.count == 2 && bounds.allSatisfy(Self.finite) && bounds[0] <= bounds[1], "Invalid uncertainty interval")
    }

    private func validateGroup(_ group: Group, id: DecisionIdentity, measure: Measure, raw: DecisionJSON) throws {
        guard let fields = raw.object else { throw DecisionError.invalid("Invalid group") }
        let basic: Set<DecisionIdentity> = ["calls", "unmet_gates", "status", "coverage", "gates"]
        let inference: Set<DecisionIdentity> = ["estimate", "scale", "simultaneous_95_interval", "worst_case_interval", "coverage_route", "effect_checks"]
        try decisionRequire(Set(fields.keys) == basic.union(group.hasInference ? inference : []), "Missing or unknown group inference fields")
        try decisionRequire(Set(group.gates.keys) == Self.requiredGates.union(["coverage_or_directional_worst_case"]), "Missing group gates")
        try decisionRequire(measure.gates.allSatisfy { group.gates[$0.key] == $0.value }, "Group/measure gate mismatch")
        var unmet = Set(measure.unmetGates)
        if group.gates["coverage_or_directional_worst_case"] == false { unmet.insert(group.hasInference ? "directional_worst_case_not_separated" : "coverage_or_directional_worst_case") }
        try decisionRequire(Self.unique(group.unmetGates) && Set(group.unmetGates) == unmet, "Unmet gate list contradicts computed blockers")
        try decisionRequire(Self.unique(group.calls) && group.calls.count <= 1 && group.calls.allSatisfy { ["Best", "NeedsAttention"].contains($0) }, "Invalid computed calls")
        let ready = group.gates.values.allSatisfy { $0 }
        if !group.hasInference { try decisionRequire(group.gates["coverage_or_directional_worst_case"] == measure.coverageThresholdPass, "Unevaluated directional coverage claim") }
        switch group.status {
        case .best: try decisionRequire(ready && group.hasInference && group.calls == ["Best"], "Best contradicts gates or intervals")
        case .attention: try decisionRequire(ready && group.hasInference && group.calls == ["NeedsAttention"] && id != policy.referenceSetup, "Needs attention contradicts gates/reference")
        case .overlap: try decisionRequire(ready && group.hasInference && group.calls.isEmpty && measure.coverageThresholdPass, "No clear difference without evaluated evidence")
        case .insufficient: try decisionRequire(!ready && group.calls.isEmpty, "Insufficient status contradicts gates/calls")
        }
        if group.hasInference {
            guard let estimate = group.estimate, let scale = group.scale, let bounds = group.simultaneous95Interval,
                  let worst = group.worstCaseInterval, let effect = group.effectChecks else { throw DecisionError.invalid("Incomplete inference") }
            try validateInterval(bounds)
            try decisionRequire(Self.finite(estimate) && bounds[0] <= estimate && estimate <= bounds[1] && scale == (measure.kind == .rate ? "rate" : "log_amount"), "Invalid estimate/scale")
            try self.fields(fields["effect_checks"], required: ["best_separates_every_other", "worst_case_best_separates_every_other", "deficit_lower_bound", "worst_case_deficit_lower_bound", "deficit_threshold", "scale"])
            try decisionRequire(worst.count == 2 && worst.compactMap { $0 }.allSatisfy(Self.finite) && worst[0] != nil, "Invalid worst-case interval")
            if let low = worst[0], let high = worst[1] { try decisionRequire(low <= high, "Reversed worst-case interval") }
            if measure.kind == .rate { try decisionRequire(Self.fraction(estimate) && bounds.allSatisfy(Self.fraction) && worst.allSatisfy { $0.map(Self.fraction) ?? false }, "Rate outside range") }
            try decisionRequire(group.coverageRoute == nil || ["at_least_90_percent_each", "directional_worst_case"].contains(group.coverageRoute!), "Unknown coverage route")
            try decisionRequire(effect.scale == (measure.kind == .rate ? "rate_difference" : "log_ratio") && Self.close(effect.deficitThreshold, measure.kind == .rate ? 0.1 : log(1.5)), "Effect scale/threshold mismatch")
            try decisionRequire([effect.deficitLowerBound, effect.worstCaseDeficitLowerBound].compactMap { $0 }.allSatisfy(Self.finite), "Invalid deficit bound")
            if id == policy.referenceSetup {
                try decisionRequire(effect.deficitLowerBound == nil && effect.worstCaseDeficitLowerBound == nil, "Reference setup has a deficit against itself")
            } else {
                try decisionRequire(effect.deficitLowerBound != nil && (measure.kind == .amount || effect.worstCaseDeficitLowerBound != nil), "Unknown deficit cannot become measured overlap")
            }
            let precise = measure.gates["precision"] == true
            let best = precise && effect.bestSeparatesEveryOther && (measure.coverageThresholdPass || effect.worstCaseBestSeparatesEveryOther)
            let attention = precise && id != policy.referenceSetup && (effect.deficitLowerBound ?? -.infinity) > effect.deficitThreshold && (measure.coverageThresholdPass || (effect.worstCaseDeficitLowerBound ?? -.infinity) > effect.deficitThreshold)
            try decisionRequire(group.gates["coverage_or_directional_worst_case"] == (measure.coverageThresholdPass || best || attention), "Directional coverage gate contradiction")
            try decisionRequire(group.coverageRoute == (precise ? (measure.coverageThresholdPass ? "at_least_90_percent_each" : "directional_worst_case") : nil), "Coverage route contradicts precision/coverage")
            let expectedCalls = measure.gates.values.allSatisfy { $0 } ? [("Best", best), ("NeedsAttention", attention)].filter(\.1).map(\.0) : []
            try decisionRequire(group.calls == expectedCalls, "Calls contradict computed effect checks")
            if group.status == .best { try decisionRequire(effect.bestSeparatesEveryOther && (measure.coverageThresholdPass || effect.worstCaseBestSeparatesEveryOther), "Best lacks computed separation") }
            if group.status == .attention { try decisionRequire((effect.deficitLowerBound ?? -.infinity) > effect.deficitThreshold && (measure.coverageThresholdPass || (effect.worstCaseDeficitLowerBound ?? -.infinity) > effect.deficitThreshold), "Attention lacks computed deficit") }
        }
    }

    private func validateCalibration(_ measure: Measure, raw: [DecisionIdentity: DecisionJSON]) throws {
        let c = measure.calibration
        try decisionRequire(measure.gates["calibration_false_flags_at_most_10_percent"] == c.pass, "Calibration gate contradiction")
        if c.status == "not_computed" {
            try decisionRequire(!c.pass && c.reason.map(Self.text) == true && Set(raw["calibration"]?.object?.keys.map { $0 } ?? []) == ["status", "pass", "reason"], "Invalid unavailable calibration")
            return
        }
        guard c.status == "computed", let hash = c.calibrationHash, let scenarios = c.scenarios, let shape = c.shape,
              let missing = c.missingShape, c.seed != nil else { throw DecisionError.invalid("Incomplete calibration") }
        try fields(raw["calibration"], required: ["status", "pass", "seed", "family_size", "reference_setup", "shape", "missing_shape", "scenarios", "rule", "calibration_hash"])
        try decisionRequire(c.familySize == policy.family.count && c.referenceSetup == policy.referenceSetup && Set(shape.keys) == Set(policy.family) && Set(missing.keys) == Set(policy.family), "Calibration family mismatch")
        try decisionRequire(c.rule == "any flag in frozen family; simultaneous Wilson upper <= 0.10" && Self.isDigest(hash), "Unsupported calibration rule/hash")
        try decisionRequire(try raw["calibration"]!.digest(removing: "calibration_hash") == hash, "Calibration integrity mismatch")
        let expected: Set<String> = measure.kind == .rate ? ["pooled_joint_strata", "0.01", "0.5", "0.99"] : ["pooled_joint_strata", "0.5", "1.5"]
        try decisionRequire(Set(scenarios.map(\.scenario)) == expected && scenarios.count == expected.count && c.pass == scenarios.allSatisfy(\.pass), "Calibration scenarios/pass mismatch")
        let z = measure.kind == .rate ? 2.2414027276049464 : 2.128045234184984
        for scenario in scenarios {
            try decisionRequire(scenario.simulations == policy.calibrationSimulations && (0...scenario.simulations).contains(scenario.falseFlags) && scenario.failureCases.count == scenario.falseFlags && Self.fraction(scenario.falseFlagFraction) && Self.analyticClose(scenario.falseFlagFraction, Double(scenario.falseFlags) / Double(scenario.simulations)) && Self.fraction(scenario.upper95Simultaneous) && scenario.upper95Simultaneous >= scenario.falseFlagFraction && scenario.pass == (scenario.upper95Simultaneous <= 0.1), "Invalid calibration scenario")
            let n = Double(scenario.simulations), p = Double(scenario.falseFlags) / n
            let denominator = 1 + z * z / n
            let center = (p + z * z / (2 * n)) / denominator
            let halfWidth = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
            let expectedUpper = min(1, center + halfWidth)
            try decisionRequire(Self.analyticClose(scenario.upper95Simultaneous, expectedUpper) && scenario.pass == (expectedUpper <= 0.1), "Calibration upper/pass contradicts frozen simultaneous Wilson rule")
            try decisionRequire(Set(scenario.failureCases.map(\.simulation)).count == scenario.failureCases.count && scenario.failureCases.allSatisfy { (0..<scenario.simulations).contains($0.simulation) && !$0.flags.isEmpty && Self.unique($0.flags) && $0.flags.allSatisfy { flag in policy.family.contains { flag == $0.appending(":Best") || flag == $0.appending(":NeedsAttention") } } }, "Invalid calibration failure cases")
        }
        try decisionRequire(shape.values.allSatisfy { $0.values.allSatisfy { (0...5000).contains($0) } } && missing.values.allSatisfy { $0.values.allSatisfy { (0...5000).contains($0) } }, "Invalid calibration shape")
        let cells = Set(measure.supportedMix.map { DecisionIdentity($0.stratum.map(\.rawValue).joined(separator: "/")) })
        for g in policy.family {
            try decisionRequire(Set(shape[g]!.keys) == cells && Set(missing[g]!.keys) == cells && shape[g]!.values.allSatisfy { $0 >= 2 } && shape[g]!.values.reduce(0, +) == measure.independentCounts[g], "Calibration shape/sample mismatch")
            if everyCellCoverage {
                let covered = !cells.isEmpty && cells.allSatisfy { 10 * shape[g]![$0]! >= 9 * (shape[g]![$0]! + missing[g]![$0]!) }
                try decisionRequire(measure.coverage[g]!.thresholdPass == covered, "Coverage gate contradicts its supported cells")
            }
        }
    }
}

struct DecisionSource {
    static let maximumAge: TimeInterval = 86400
    var lastGood: ModelDecision?
    var attemptedAt: Date?
    var acceptedAt: Date?
    var error: String?
    var failureKind: String?

    mutating func read(_ url: URL, now: Date) {
        attemptedAt = now
        do {
            try decisionRequire(url.isFileURL, "Decision input must be a local file")
            let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
            try decisionRequire(attributes[.type] as? FileAttributeType == .typeRegular, "Decision input is not a regular file")
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            let data = try handle.read(upToCount: 32 * 1024 * 1024 + 1) ?? Data()
            let next = try ModelDecision.decode(data, now: now)
            lastGood = next
            acceptedAt = now
            error = nil
            failureKind = nil
        } catch {
            let cocoa = error as NSError
            if cocoa.domain == NSCocoaErrorDomain && [NSFileReadNoSuchFileError, NSFileNoSuchFileError].contains(cocoa.code) { failureKind = "Missing input" }
            else if error is DecisionError || error is DecodingError { failureKind = "Invalid input" }
            else { failureKind = "Unavailable input" }
            self.error = (error as? DecisionError)?.errorDescription ?? (error is DecodingError ? "Required decision fields are absent or malformed" : "Decision publication cannot be read")
        }
    }

    func isCurrent(at now: Date) -> Bool {
        guard let snapshot = lastGood, error == nil, !snapshot.supersededPolicy else { return false }
        let age = now.timeIntervalSince(snapshot.date)
        return age.isFinite && (0...Self.maximumAge).contains(age)
    }

    func status(at now: Date) -> String {
        if let error {
            var message = (failureKind ?? "Failed refresh") + ": " + error
            if let snapshot = lastGood {
                message += "; retaining last good snapshot"
                if snapshot.supersededPolicy { message += "; superseded policy, historical only" }
                let age = now.timeIntervalSince(snapshot.date)
                if !age.isFinite { message += "; consumer clock unavailable" }
                else if age < 0 { message += "; retained as_of is ahead of consumer clock" }
                else if age > Self.maximumAge { message += "; stale as_of older than 24 h" }
            }
            return message
        }
        guard let snapshot = lastGood else { return "Missing input: model-decision.json has not been read" }
        if snapshot.supersededPolicy { return "Historical only: policy \(snapshot.policyVersion) is superseded; its calls are not current conclusions" }
        let age = now.timeIntervalSince(snapshot.date)
        if !age.isFinite { return "Consumer clock unavailable; historical computed results only" }
        if age < 0 { return "Historical only: as_of is ahead of the consumer clock" }
        if age > Self.maximumAge { return "Stale snapshot: as_of is older than 24 h; historical computed results only" }
        return "Decision snapshot loaded · as_of " + snapshot.asOf
    }

    func label(_ group: ModelDecision.Group, at now: Date) -> String {
        if lastGood?.supersededPolicy == true { return "Superseded policy, not a current conclusion: " + group.status.rawValue }
        return (isCurrent(at: now) ? "Computed: " : "Historical computed: ") + group.status.label
    }

    static func number(_ value: Double) -> String { String(format: "%.3g", value) }
    static func coverageLine(_ coverage: ModelDecision.Coverage, everyCell: Bool) -> String {
        let fraction = number(coverage.minimumFraction * 100) + "%"
        return everyCell ? "PR coverage \(fraction) · every supported cell at least 90%: " + (coverage.thresholdPass ? "yes" : "no") : "Minimum coverage: " + fraction
    }
    static func interval(_ values: [Double?]?) -> String {
        guard let values, values.count == 2 else { return "not computed" }
        return "[" + values.map { $0.map(number) ?? "unbounded" }.joined(separator: ", ") + "]"
    }

    static func domain(_ measure: ModelDecision.Measure) -> ClosedRange<Double>? {
        if measure.kind == .rate { return 0...1 }
        let values = measure.groups.values.flatMap { $0.simultaneous95Interval ?? [] }
        guard let low = values.min(), let high = values.max() else { return nil }
        return low == high ? (low - 1)...(high + 1) : low...high
    }

    static func positions(_ group: ModelDecision.Group, domain: ClosedRange<Double>) -> [Double]? {
        guard let value = group.estimate, let bounds = group.simultaneous95Interval, bounds.count == 2 else { return nil }
        let width = domain.upperBound - domain.lowerBound
        guard width.isFinite, width > 0 else { return nil }
        let positions = [bounds[0], value, bounds[1]].map { ($0 - domain.lowerBound) / width }
        guard positions.allSatisfy({ $0.isFinite && (0...1).contains($0) }) else { return nil }
        return positions
    }

    static func gateLabel(_ key: DecisionIdentity) -> String {
        ["inventory_complete": "Complete inventory", "two_exact_reported_setups": "Exact reported setups", "independent_prs_at_least_20_each": "20 independent PRs per setup", "common_support_at_least_70_percent": "70% common joint support", "verified_measure_representation": "Verified measure representation", "calibration_false_flags_at_most_10_percent": "Null calibration ≤10% false flags", "precision": "Interval precision", "coverage_or_directional_worst_case": "Coverage or directional bound", "directional_worst_case_not_separated": "Worst-case bound does not separate" ][key.rawValue] ?? key.rawValue
    }
}
