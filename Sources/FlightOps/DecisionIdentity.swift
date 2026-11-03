import Foundation

struct DecisionIdentity: Hashable, Comparable, Decodable, ExpressibleByStringLiteral, CustomStringConvertible {
    let rawValue: String
    private let bytes: [UInt8]

    init(_ rawValue: String) {
        self.rawValue = rawValue
        bytes = Array(rawValue.utf8)
    }
    init(stringLiteral value: String) { self.init(value) }
    init(from decoder: Decoder) throws { self.init(try decoder.singleValueContainer().decode(String.self)) }
    var description: String { rawValue }
    var isEmpty: Bool { bytes.isEmpty }
    static func == (lhs: Self, rhs: Self) -> Bool { lhs.bytes == rhs.bytes }
    static func < (lhs: Self, rhs: Self) -> Bool { lhs.bytes.lexicographicallyPrecedes(rhs.bytes) }
    func hash(into hasher: inout Hasher) { hasher.combine(bytes) }
    func appending(_ suffix: String) -> Self { Self(rawValue + suffix) }
    var transportKey: String { "u8:" + bytes.map { String(format: "%02x", $0) }.joined() }

    init(transportKey: String) throws {
        let bytes = Array(transportKey.utf8)
        try decisionRequire(bytes.starts(with: [117, 56, 58]) && bytes.count % 2 == 1, "Invalid internal decision key")
        func digit(_ value: UInt8) throws -> UInt8 {
            switch value {
            case 48...57: return value - 48
            case 97...102: return value - 87
            default: throw DecisionError.invalid("Invalid internal decision key digit")
            }
        }
        var decoded: [UInt8] = []
        for index in stride(from: 3, to: bytes.count, by: 2) {
            decoded.append(try digit(bytes[index]) * 16 + digit(bytes[index + 1]))
        }
        guard let raw = String(bytes: decoded, encoding: .utf8) else { throw DecisionError.invalid("Invalid internal UTF8 identity") }
        self.init(raw)
    }
}

struct DecisionMap<Value: Decodable>: Decodable, Sequence {
    private let storage: [DecisionIdentity: Value]
    init(from decoder: Decoder) throws {
        let encoded = try decoder.singleValueContainer().decode([String: Value].self)
        var decoded: [DecisionIdentity: Value] = [:]
        for (key, value) in encoded {
            let identity = try DecisionIdentity(transportKey: key)
            try decisionRequire(decoded.updateValue(value, forKey: identity) == nil, "Duplicate typed decision identity")
        }
        storage = decoded
    }
    var count: Int { storage.count }
    var isEmpty: Bool { storage.isEmpty }
    var keys: Dictionary<DecisionIdentity, Value>.Keys { storage.keys }
    var values: Dictionary<DecisionIdentity, Value>.Values { storage.values }
    subscript(_ key: DecisionIdentity) -> Value? { storage[key] }
    func makeIterator() -> Dictionary<DecisionIdentity, Value>.Iterator { storage.makeIterator() }
    func filter(_ predicate: ((key: DecisionIdentity, value: Value)) throws -> Bool) rethrows -> [DecisionIdentity: Value] {
        try storage.filter(predicate)
    }
    func mapValues<T>(_ transform: (Value) throws -> T) rethrows -> [DecisionIdentity: T] { try storage.mapValues(transform) }
}

struct DecisionCodingKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
    static func decoded(_ path: [CodingKey]) -> CodingKey {
        guard let key = path.last, let raw = try? DecisionIdentity(transportKey: key.stringValue).rawValue else { return DecisionCodingKey(stringValue: "")! }
        let pieces = raw.split(separator: "_", omittingEmptySubsequences: false)
        return DecisionCodingKey(stringValue: String(pieces[0]) + pieces.dropFirst().map { $0.prefix(1).uppercased() + $0.dropFirst() }.joined())!
    }
}
