import Foundation

var checks = 0
func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    checks += 1
    if !condition() { FileHandle.standardError.write(Data(("FAIL: " + message + "\n").utf8)); exit(1) }
}
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let fixture = try Data(contentsOf: directory.appendingPathComponent("valid.json"))
let now = ModelDecision.timestamp("2026-10-29T12:01:00Z")!
let decision = try ModelDecision.decode(fixture, now: now)
expect(decision.measures["metric"]?.groups["a"]?.status == .best, "Frozen kernel Best must decode")
expect(decision.measures["metric"]?.groups["b"]?.status == .attention, "Frozen kernel NeedsAttention must decode")
expect(decision.decisionHash == "c4a6c3dcf98260ae0eace9e310a4ecaaec99e72f4561e06fd395a7b481eef764", "Frozen golden identity")
expect(decision.measures["metric"]!.groups["b"]!.estimate == 0, "Known zero estimate remains zero")
expect(decision.measures["metric"]!.populations["b"]!.verifiedNumerator == 0, "Known zero numerator remains zero")

struct ExpectedStatus: Decodable { let id: DecisionIdentity; let status: String }
struct Case: Decodable { let name: String; let valid: Bool; let statuses: [ExpectedStatus] }
var producerToConsumer: Set<String> = ["historical_outside_support", "outside_support", "weighted_boundary", "support_mass_tolerance", "support_gate_boundary", "support_gate_reversed", "support_gate_above", "support_gate_below", "support_gate_just_below", "standardized_rate", "standardized_amount", "historical_standardized_rate", "historical_standardized_amount"]
let cases = try JSONDecoder().decode([Case].self, from: Data(contentsOf: directory.appendingPathComponent("cases.json")))
for item in cases {
    let data = try Data(contentsOf: directory.appendingPathComponent(item.name + ".json"))
    do {
        let parsed = try ModelDecision.decode(data, now: now)
        expect(item.valid, "Invalid case accepted: " + item.name)
        producerToConsumer.remove(item.name)
        for expected in item.statuses { expect(parsed.measures["metric"]!.groups[expected.id]!.status.rawValue == expected.status, item.name + " status " + expected.id.rawValue) }
        if item.name == "unbounded_amount" {
            expect(parsed.measures["metric"]!.groups["a"]!.worstCaseInterval![1] == nil, "Unbounded missing amount stays unknown")
            expect(DecisionSource.interval(parsed.measures["metric"]!.groups["a"]!.worstCaseInterval).contains("unbounded"), "Unbounded label visible")
        }
        if item.name == "insufficient_kernel" {
            expect(parsed.measures.values.allSatisfy { $0.groups.values.allSatisfy { $0.estimate == nil && $0.status == .insufficient } }, "Actual insufficient kernel never becomes zero/overlap")
        }
        if item.name == "standardized_rate" || item.name == "standardized_amount" {
            let coverage = parsed.measures["metric"]!.groups["a"]!.coverage
            expect(coverage.minimumFraction == 0.9 && !coverage.thresholdPass && !parsed.measures["metric"]!.coverageThresholdPass && parsed.measures["metric"]!.calibration.pass, item.name + " keeps raw coverage but earns no waiver")
            expect(DecisionSource.coverageLine(coverage, everyCell: parsed.everyCellCoverage) == "PR coverage 90% · every supported cell at least 90%: no", "The reader says which coverage failed")
        }
        if item.name == "historical_standardized_rate" || item.name == "historical_standardized_amount" {
            let coverage = parsed.measures["metric"]!.groups["a"]!.coverage
            expect(coverage.thresholdPass && DecisionSource.coverageLine(coverage, everyCell: parsed.everyCellCoverage) == "Minimum coverage: 90%", item.name + " keeps its original meaning")
        }
        if item.name == "historical_outside_support" {
            expect(parsed.policyVersion == ModelDecision.historicalPolicyName && parsed.measures["metric"]!.groups["a"]!.coverage.included == 360, "A historical decision keeps its old identity and counts")
        }
        if item.name == "outside_support" {
            expect(parsed.policyVersion == ModelDecision.policyName, "New decisions carry the current policy")
            let measure = parsed.measures["metric"]!
            expect(measure.groups["a"]!.coverage.included == 40 && measure.groups["a"]!.coverage.knownDenominator == 80 && measure.coverageThresholdPass == false, "Coverage is counted on common support")
            expect(measure.populations["a"]!.outsideSupport == 320 && measure.populations["b"]!.outsideSupport == 320, "PRs outside common support stay listed")
        }
        if item.name == "weighted_boundary" {
            expect(parsed.measures["metric"]!.groups["a"]!.estimate == 1 && parsed.measures["metric"]!.groups["b"]!.estimate == 0, "Weighted boundary rates stay at their exact ends")
        }
        if item.name == "multiple_measures" {
            expect(parsed.measures.count == 2 && parsed.measures["second"]!.groups["a"]!.status == .overlap, "Metrics keep separate computed outcomes")
        }
    } catch {
        expect(!item.valid, "Valid case rejected: \(item.name): \(error)")
    }
}

struct Vector: Decodable { let input: String; let canonical: String; let digest: String }
let vectors = try JSONDecoder().decode([Vector].self, from: Data(contentsOf: directory.appendingPathComponent("canonical-vectors.json")))
for vector in vectors {
    var parser = try DecisionJSON.Parser(Data(vector.input.utf8))
    let parsed = try parser.parse()
    expect(Data(parsed.canonical) == Data(vector.canonical.utf8), "Python canonical encoding mismatch: " + vector.input)
    expect(DecisionSHA256.hex(parsed.canonical) == vector.digest, "Python digest mismatch")
}
struct HashVector: Decodable { let bytes: [UInt8]; let digest: String }
let hashVectors = try JSONDecoder().decode([HashVector].self, from: Data(contentsOf: directory.appendingPathComponent("sha-vectors.json")))
for vector in hashVectors { expect(DecisionSHA256.hex(vector.bytes) == vector.digest, "SHA-256 padding boundary mismatch") }
expect(DecisionSHA256.hex(Array("abc".utf8)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "SHA-256 independent abc vector")
do {
    var parser = try DecisionJSON.Parser(Data((String(repeating: "[", count: 70) + "0" + String(repeating: "]", count: 70)).utf8))
    _ = try parser.parse()
    expect(false, "Deep JSON accepted")
} catch { expect(true, "Depth budget rejects") }
do {
    _ = try ModelDecision.decode(Data(repeating: 32, count: 32 * 1024 * 1024 + 1), now: now)
    expect(false, "Oversized publication accepted")
} catch { expect(true, "Size budget rejects") }

let currentFixture = try Data(contentsOf: directory.appendingPathComponent("current.json"))
let current = try ModelDecision.decode(currentFixture, now: now)
expect(current.policyVersion == ModelDecision.policyName, "The lifecycle uses a current-policy decision")
let publication = directory.appendingPathComponent("lifecycle-publication.json")
try currentFixture.write(to: publication, options: .atomic)
var source = DecisionSource()
source.read(publication, now: now)
expect(source.isCurrent(at: now), "Fresh real kernel publication current")
expect(source.error == nil && source.lastGood?.decisionHash == current.decisionHash, "Fresh source accepted")
let accepted = source.acceptedAt
expect(source.label(current.measures["metric"]!.groups["a"]!, at: now).hasPrefix("Computed: Best"), "UI helper uses real computed status")
let aged = now.addingTimeInterval(86401)
try FileManager.default.setAttributes([.modificationDate: aged], ofItemAtPath: publication.path)
source.read(publication, now: aged)
expect(!source.isCurrent(at: aged) && source.status(at: aged).contains("Stale"), "Touch/read clock cannot freshen old as_of")
expect(source.lastGood?.asOf == current.asOf, "Historical source clock retained")
expect(source.label(current.measures["metric"]!.groups["a"]!, at: aged).hasPrefix("Historical computed"), "Stale result cannot be a current UI call")
source.read(publication, now: now)
expect(source.isCurrent(at: now), "Explicit source clock controls freshness")
try Data("{}".utf8).write(to: publication, options: .atomic)
source.read(publication, now: now.addingTimeInterval(1))
expect(source.failureKind == "Invalid input" && !source.isCurrent(at: now), "Malformed refresh observable")
expect(source.lastGood?.decisionHash == current.decisionHash && source.acceptedAt == accepted, "Malformed refresh keeps last good and original accepted clock")
expect(source.label(current.measures["metric"]!.groups["a"]!, at: now).hasPrefix("Historical"), "Failed refresh suppresses current styling")
expect(source.status(at: aged).contains("stale") && source.status(at: aged).contains("Invalid input"), "Failed refresh and stale retained data are both visible")
try FileManager.default.removeItem(at: publication)
source.read(publication, now: now.addingTimeInterval(2))
expect(source.failureKind == "Missing input" && source.status(at: now).contains("retaining last good"), "Removed file visible, history retained")
expect(source.lastGood?.asOf == current.asOf && source.acceptedAt == accepted, "Removed file does not change original clock")
try Data(contentsOf: directory.appendingPathComponent("future.json")).write(to: publication, options: .atomic)
source.read(publication, now: now)
expect(source.failureKind == "Invalid input" && source.error!.contains("future"), "Future timestamp explicitly rejected")
expect(source.lastGood?.decisionHash == current.decisionHash, "Future replacement does not erase good snapshot")
try currentFixture.write(to: publication, options: .atomic)
source.read(publication, now: now.addingTimeInterval(3))
expect(source.error == nil && source.isCurrent(at: now.addingTimeInterval(3)), "Recovered file replaces failure state")
expect(source.lastGood?.asOf == current.asOf, "Recovery retains original as_of")
expect(!source.isCurrent(at: current.date.addingTimeInterval(-1)), "Clock rewind cannot mark future-as-of healthy")
source.read(directory, now: now)
expect(source.failureKind == "Invalid input" && source.lastGood != nil, "Nonregular input rejected, good history retained")
source.read(URL(string: "https://example.invalid/decision")!, now: now)
expect(source.failureKind == "Invalid input", "Nonlocal input rejected before I/O")
var missing = DecisionSource()
missing.read(directory.appendingPathComponent("absent.json"), now: now)
expect(missing.lastGood == nil && missing.failureKind == "Missing input", "No file is missing, never an empty healthy snapshot")

let rate = decision.measures["metric"]!
let positions = DecisionSource.positions(rate.groups["b"]!, domain: DecisionSource.domain(rate)!)!
expect(positions[0] <= positions[1] && positions[1] <= positions[2], "Interval graphic preserves zero/bounds")
expect(DecisionSource.interval(nil) == "not computed", "Missing interval text does not fabricate zero")
var clock = DecisionSource()
clock.read(publication, now: now)
let invalidClock = Date(timeIntervalSince1970: .nan)
expect(!clock.isCurrent(at: invalidClock) && clock.status(at: invalidClock).contains("clock unavailable"), "Unknown clock cannot mark input current")
clock.read(publication, now: invalidClock)
expect(clock.error != nil && clock.lastGood?.asOf == decision.asOf, "Invalid read clock preserves good source clock")

let composed = DecisionIdentity("é"), decomposed = DecisionIdentity("e\u{301}")
expect(composed != decomposed && Set([composed,decomposed]).count == 2, "Exact UTF8 identity distinguishes canonical equivalents for ForEach IDs")
for id in [composed,decomposed,DecisionIdentity("u8:c3a9"),DecisionIdentity(""),DecisionIdentity("😀/x\u{0}")] {
    let restored = try DecisionIdentity(transportKey: id.transportKey)
    expect(Array(restored.rawValue.utf8) == Array(id.rawValue.utf8), "Internal transport reverses original bytes")
}
var unicodeParser = try DecisionJSON.Parser(Data("{\"é\":1,\"e\\u0301\":2}".utf8))
let unicodeRaw = try unicodeParser.parse()
expect(unicodeRaw.object?.count == 2 && unicodeRaw.object?[composed] != nil && unicodeRaw.object?[decomposed] != nil, "Raw JSON object preserves both decoded identities")
for data in ["{\"é\":1,\"\\u00e9\":2}","{\"x\":1,\"\\u0078\":2}"] {
    do {
        var parser = try DecisionJSON.Parser(Data(data.utf8))
        _ = try parser.parse()
        expect(false, "Identical decoded key escaped differently must reject")
    } catch { expect(true, "True decoded duplicate rejected") }
}
func readCase(_ name: String) throws -> ModelDecision {
    try ModelDecision.decode(Data(contentsOf: directory.appendingPathComponent(name + ".json")), now: now)
}
let unicodeKernel = try readCase("unicode_kernel")
expect(unicodeKernel.decisionHash == "6886798d018f8dcd9fd03e346eae4e7d3e1d8c42dc0ac6f1a5cab03db6471132", "Actual zero-draw Unicode kernel identity")
expect(unicodeKernel.policy.family == [composed,decomposed] && unicodeKernel.policy.referenceSetup == composed, "Frozen Unicode family and reference remain exact")
expect(unicodeKernel.measures["metric"]!.groups.count == 2 && unicodeKernel.measures["metric"]!.groups[composed]!.status == .insufficient && unicodeKernel.measures["metric"]!.groups[decomposed]!.status == .insufficient, "Actual Unicode kernel groups survive typed decoding")
let unicodeComputed = try readCase("unicode_computed")
let unicodeMeasure = unicodeComputed.measures["metric"]!
expect(unicodeMeasure.groups[composed]!.status == .best && unicodeMeasure.groups[decomposed]!.status == .attention, "Unicode current status lookup distinguishes setup identities")
expect(unicodeMeasure.referenceSetup == composed && unicodeMeasure.comparisons[0].a == composed && unicodeMeasure.comparisons[0].b == decomposed, "Unicode pair and reference matching retain orientation")
expect(unicodeMeasure.calibration.shape![composed] != nil && unicodeMeasure.calibration.shape![decomposed] != nil, "Unicode nested dynamic calibration map preserves both groups")
expect(Set(unicodeMeasure.family).count == 2 && Set(unicodeComputed.measures.keys).count == 1, "Group/measure ForEach IDs preserve raw equality")
expect(unicodeMeasure.family.map(\.rawValue).map { Array($0.utf8) } == [[195,169],[101,204,129]], "UI labels display original Unicode bytes")
let unicodeMetrics = try readCase("unicode_measures")
expect(unicodeMetrics.measures.count == 2 && Set(unicodeMetrics.measures.keys).count == 2, "Dynamic measure maps and UI IDs preserve both Unicode keys")
expect(unicodeMetrics.measures[composed]!.groups["a"]!.status == .best && unicodeMetrics.measures[decomposed]!.groups["a"]!.status == .overlap, "Per-measure outcomes remain separate across equivalent-looking IDs")
let unicodeReasons = try readCase("unicode_reasons_exclusions").measures["metric"]!
expect(unicodeReasons.populations["a"]!.reasons.count == 2 && unicodeReasons.populations["a"]!.reasons[composed] == 5 && unicodeReasons.populations["a"]!.reasons[decomposed] == 5, "Reason keys retain raw identity")
expect(Set(unicodeReasons.excludedGroups).count == 2, "Excluded setup set keeps exact Unicode IDs")
let unicodeStrata = try readCase("unicode_strata").measures["metric"]!
expect(unicodeStrata.supportedMix.count == 2 && Set(unicodeStrata.supportedMix.map(\.stratum)).count == 2, "Joint stratum set preserves distinct Unicode tasks")
expect(unicodeStrata.calibration.shape!["a"]!.count == 2, "Joined shape keys preserve both Unicode strata")
let ratePassing = try readCase("rate_last_passing").measures["metric"]!
let rateFailing = try readCase("rate_first_failing").measures["metric"]!
expect(abs(ratePassing.calibration.scenarios![0].upper95Simultaneous - 0.0926620133421014) < 1e-15 && ratePassing.groups["b"]!.status == .attention, "Frozen four-scenario last passing supplied count enables current deficit")
expect(abs(rateFailing.calibration.scenarios![0].upper95Simultaneous - 0.10417200293092196) < 1e-15 && rateFailing.groups.values.allSatisfy { $0.status == .insufficient && $0.calls.isEmpty }, "First failing rate count withholds all calls")
let twelve = try readCase("rate_twelve_failures").measures["metric"]!
expect(abs(twelve.calibration.scenarios![0].upper95Simultaneous - 0.17832173798031625) < 1e-15 && !twelve.calibration.pass, "Reported twelve failures use actual frozen simultaneous bound")
let amountPassing = try readCase("amount_last_passing").measures["metric"]!
let amountFailing = try readCase("amount_first_failing").measures["metric"]!
expect(abs(amountPassing.calibration.scenarios![0].upper95Simultaneous - 0.09992655795235886) < 1e-15 && amountPassing.calibration.pass, "Amount uses three-scenario family adjustment")
expect(abs(amountFailing.calibration.scenarios![0].upper95Simultaneous - 0.11103195849034778) < 1e-15 && !amountFailing.calibration.pass, "Amount first failing count withholds calls")
for name in ["D1_rate_point","D1_rate_envelope","D1_amount_point","D2_false_passing_upper","D2_boundary_upper"] {
    var invalidSource = DecisionSource()
    invalidSource.read(directory.appendingPathComponent(name + ".json"), now: now)
    expect(invalidSource.lastGood == nil && invalidSource.failureKind == "Invalid input" && !invalidSource.isCurrent(at: now), name + " cannot reach current-label rendering")
    invalidSource.read(directory.appendingPathComponent("current.json"), now: now)
    let good = invalidSource.lastGood!
    invalidSource.read(directory.appendingPathComponent(name + ".json"), now: now.addingTimeInterval(1))
    expect(invalidSource.lastGood?.decisionHash == good.decisionHash && invalidSource.lastGood?.asOf == good.asOf && !invalidSource.isCurrent(at: now.addingTimeInterval(1)), name + " failed refresh keeps original good history")
    expect(invalidSource.label(good.measures["metric"]!.groups["b"]!, at: now.addingTimeInterval(1)).hasPrefix("Historical computed:"), name + " rejected refresh cannot leave a current NeedsAttention label")
}

expect(producerToConsumer.isEmpty, "Every real producer case reached the consumer")
for name in ["valid", "historical_outside_support", "historical_standardized_rate", "historical_standardized_amount"] {
    var superseded = DecisionSource()
    superseded.read(directory.appendingPathComponent(name + ".json"), now: now)
    let best = superseded.lastGood!.measures["metric"]!.groups["a"]!
    expect(superseded.error == nil && best.status == .best && !superseded.isCurrent(at: now), name + " is readable but never current")
    expect(superseded.status(at: now) == "Historical only: policy joint-strata-max-bootstrap/1 is superseded; its calls are not current conclusions", name + " says why: " + superseded.status(at: now))
    expect(superseded.label(best, at: now) == "Superseded policy, not a current conclusion: Best", name + " keeps no supported wording: " + superseded.label(best, at: now))
    superseded.read(directory.appendingPathComponent("no-such-file.json"), now: now)
    expect(superseded.status(at: now).contains("retaining last good snapshot; superseded policy, historical only"), name + " stays historical after a failed refresh: " + superseded.status(at: now))
}
var supportOrders: Set<Double> = []
func permute(_ rest: [Double], _ chosen: [Double] = []) {
    if rest.isEmpty { supportOrders.insert(ModelDecision.supportMass(chosen)) }
    for i in rest.indices {
        var left = rest
        let next = left.remove(at: i)
        permute(left, chosen + [next])
    }
}
permute([0.438, 0.052, 0.065, 0.074, 0.071])
expect(supportOrders.count == 1 && supportOrders.first! >= ModelDecision.supportFloor, "Support mass is one number in every order: \(supportOrders)")
var forgedSupport = DecisionSource()
forgedSupport.read(directory.appendingPathComponent("forged_support_gate.json"), now: now)
expect(forgedSupport.error == "Support/sample gate contradiction", "A forged gate fails on its mass: \(forgedSupport.error ?? "accepted")")
for name in ["forged_cell_coverage", "relabelled_historical"] {
    var source = DecisionSource()
    source.read(directory.appendingPathComponent(name + ".json"), now: now)
    expect(source.error == "Coverage gate contradicts its supported cells", name + " fails on its cells: \(source.error ?? "accepted")")
}
print("PASS decision consumer: \(checks) checks")
