import Foundation

struct BestPresentation: Decodable {
    static let schemaName = "flightops-best-presentation/1"

    struct Family: Decodable {
        let status: String
        let setups: [String]
        let note: String
    }
    struct Setting: Decodable {
        let value: String?
        let basis: String
    }
    struct Setup: Decodable {
        let label: String
        let exact: Bool
        let settings: [String: Setting]
    }
    struct Counts: Decodable {
        let prs: Int
        let merged: Int
        let open: Int
        let withOrchLink: Int
        let withReviewRecord: Int
        let headExact: Int
        let ciObserved: Int
        let reviewsUnread: Int
        let reviewsRefused: [String: Int]
        let linksRefused: Int
        let firstReadySeen: Int
        let headAtOpenExact: Int
        enum CodingKeys: String, CodingKey {
            case prs, merged, open
            case withOrchLink = "with_orch_link", withReviewRecord = "with_review_record", headExact = "head_exact", ciObserved = "ci_observed"
            case reviewsUnread = "reviews_unread", reviewsRefused = "reviews_refused", linksRefused = "links_refused"
            case firstReadySeen = "first_ready_seen", headAtOpenExact = "head_at_open_exact"
        }
    }
    struct Source: Decodable, Equatable {
        let id: String
        let schema: String
        let snapshotAt: String
        let historyStart: String
        let complete: Bool
        let gaps: [String]
        enum CodingKeys: String, CodingKey {
            case id, schema, complete, gaps
            case snapshotAt = "snapshot_at", historyStart = "history_start"
        }
    }
    struct Stage: Decodable {
        let name: String
        let state: String
        let detail: String
    }
    struct Trace: Decodable {
        let pr: String
        let state: String
        let mergedAt: String?
        let openedAt: String?
        let head: String?
        let reasons: [String: [String]]
        let stages: [Stage]
        enum CodingKeys: String, CodingKey {
            case pr, state, head, reasons, stages
            case mergedAt = "merged_at", openedAt = "opened_at"
        }
    }

    let schema: String
    let decisionHash: String
    let manifestHash: String
    let inputHash: String
    let presentationHash: String
    let asOf: String
    let snapshotId: String
    let family: Family
    let setups: [String: Setup]
    let measures: [String: String]
    let counts: Counts
    let sources: [Source]
    let unknown: [String]
    let traces: [Trace]
    enum CodingKeys: String, CodingKey {
        case schema, family, setups, measures, counts, sources, unknown, traces
        case decisionHash = "decision_hash", manifestHash = "manifest_hash", inputHash = "input_hash", presentationHash = "presentation_hash"
        case asOf = "as_of", snapshotId = "snapshot_id"
    }

    var summary: String {
        "\(counts.prs) PRs · \(counts.merged) merged, \(counts.open) open · \(counts.withOrchLink) on Orchestrator links · \(counts.withReviewRecord) with a bound review · \(counts.headExact) exact heads · \(counts.ciObserved) with CI seen · \(counts.firstReadySeen) with first-ready · \(counts.headAtOpenExact) exact heads at open"
    }
    var refused: String? {
        let total = counts.reviewsRefused.values.reduce(0, +) + counts.linksRefused
        return total + counts.reviewsUnread == 0 ? nil : "\(counts.reviewsRefused.values.reduce(0, +)) review threads refused · \(counts.reviewsUnread) unread · \(counts.linksRefused) links refused"
    }
    func sourceLine(_ source: Source) -> String {
        source.id + (source.complete ? ": covers the window" : ": partial · " + source.gaps.joined(separator: " · "))
    }
    func setupLine(_ id: String) -> String? {
        guard let setup = setups[id] else { return nil }
        let parts = ["provider", "model", "effort", "tier"].map { key in key + " " + (setup.settings[key].map { ($0.value ?? "unknown") + " (" + $0.basis + ")" } ?? "unknown") }
        return parts.joined(separator: " · ")
    }
    static func reasonLine(_ reasons: [String]) -> String { reasons.joined(separator: " · ") }
}

enum BestPass {
    static let future = "Best attempt time is in the future"
    static let unreadable = "Best attempt file is unreadable"
    static let unwritable = "Best attempt could not be recorded"

    static func gate(_ text: String?, now: TimeInterval) -> Attempt.Gate {
        Attempt.gate(text, now: now, every: ModelsPass.every, future: future, unreadable: unreadable)
    }
}

struct BestManifest: Decodable {
    let manifestHash: String
    let inputHash: String
    let sources: [BestPresentation.Source]
    enum CodingKeys: String, CodingKey {
        case sources
        case manifestHash = "manifest_hash", inputHash = "input_hash"
    }
}

struct LiveBest {
    static let schema = "flightops-best/1"
    static let limit: TimeInterval = 3600
    static let malformed = "unreadable collector output"
    static let orchSchema = "orch-orchestration/2"
    static let standIn = "Stand-in evidence cannot be published"

    var decision: ModelDecision
    var presentation: BestPresentation
    var at: Date

    static func parse(_ value: [String: Any], now: Date) throws -> LiveBest {
        try decisionRequire(value["schema"] as? String == schema, "Unsupported Best publication")
        guard let generated = PublishedStatus.instant(value["generated"]), let decisionText = value["decision"] as? String, let manifestText = value["manifest"] as? String, let shownText = value["presentation"] as? String else {
            throw DecisionError.invalid("Incomplete Best publication")
        }
        let decision = try ModelDecision.decode(Data(decisionText.utf8), now: now.addingTimeInterval(clockSkew))
        try decisionRequire(!decision.supersededPolicy, "Superseded policy cannot be published")
        var parser = try DecisionJSON.Parser(Data(shownText.utf8))
        let raw = try parser.parse()
        let shown: BestPresentation
        do {
            shown = try JSONDecoder().decode(BestPresentation.self, from: Data(shownText.utf8))
        } catch {
            throw DecisionError.invalid("Presentation fields are absent or malformed")
        }
        try decisionRequire(shown.schema == BestPresentation.schemaName, "Unsupported presentation version")
        try decisionRequire(try raw.digest(removing: "presentation_hash") == shown.presentationHash, "Presentation integrity hash mismatch")
        try decisionRequire(shown.decisionHash == decision.decisionHash && shown.manifestHash == decision.manifestHash && shown.inputHash == decision.inputHash && shown.asOf == decision.asOf, "Presentation belongs to another generation")
        try decisionRequire(decision.policy.family.allSatisfy { shown.setups[$0.rawValue] != nil } && Set(shown.measures.keys.map { DecisionIdentity($0) }) == Set(decision.measures.keys), "Presentation does not cover its decision")
        try decisionRequire(shown.family.status == "diagnostic" && Set(shown.family.setups) == Set(decision.policy.family.map(\.rawValue)), "Comparison family is not labelled diagnostic")
        let manifest = try verified(manifestText, for: decision)
        try decisionRequire(manifest.sources.filter { $0.id == "orch" }.map(\.schema) == [orchSchema], standIn)
        try decisionRequire(shown.sources == manifest.sources, "Presentation sources disagree with the manifest")
        guard let asOf = ModelDecision.timestamp(decision.asOf), abs(asOf.timeIntervalSince1970 - generated) < 2 else { throw DecisionError.invalid("Publication clock contradicts its decision") }
        return LiveBest(decision: decision, presentation: shown, at: Date(timeIntervalSince1970: generated))
    }

    static func verified(_ text: String, for decision: ModelDecision) throws -> BestManifest {
        var parser = try DecisionJSON.Parser(Data(text.utf8))
        try decisionRequire(try parser.parse().digest(removing: "manifest_hash") == decision.manifestHash, "Manifest integrity hash mismatch")
        let manifest: BestManifest
        do {
            manifest = try JSONDecoder().decode(BestManifest.self, from: Data(text.utf8))
        } catch {
            throw DecisionError.invalid("Manifest fields are absent or malformed")
        }
        try decisionRequire(manifest.manifestHash == decision.manifestHash && manifest.inputHash == decision.inputHash, "Manifest belongs to another generation")
        return manifest
    }
}
