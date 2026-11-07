import Foundation

enum DecisionError: Error, LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self { case .invalid(let reason): return reason }
    }
}

func decisionRequire(_ condition: Bool, _ reason: String) throws {
    if !condition { throw DecisionError.invalid(reason) }
}

indirect enum DecisionJSON {
    case object([DecisionIdentity: DecisionJSON]), array([DecisionJSON]), string(String), number(String), bool(Bool), null

    var object: [DecisionIdentity: DecisionJSON]? {
        if case .object(let value) = self { return value }
        return nil
    }

    func digest(removing key: DecisionIdentity) throws -> String {
        guard var fields = object else { throw DecisionError.invalid("Expected an object for integrity binding") }
        fields.removeValue(forKey: key)
        return DecisionSHA256.hex(DecisionJSON.object(fields).canonical)
    }

    var typedTransport: DecisionJSON {
        switch self {
        case .object(let fields): return .object(Dictionary(uniqueKeysWithValues: fields.map { (DecisionIdentity($0.key.transportKey), $0.value.typedTransport) }))
        case .array(let values): return .array(values.map(\.typedTransport))
        default: return self
        }
    }

    var canonical: [UInt8] {
        switch self {
        case .null: return Array("null".utf8)
        case .bool(let value): return Array((value ? "true" : "false").utf8)
        case .number(let value): return Array(value.utf8)
        case .string(let value):
            var bytes: [UInt8] = [34]
            for unit in value.utf16 {
                switch unit {
                case 34: bytes += [92, 34]
                case 92: bytes += [92, 92]
                case 8: bytes += [92, 98]
                case 9: bytes += [92, 116]
                case 10: bytes += [92, 110]
                case 12: bytes += [92, 102]
                case 13: bytes += [92, 114]
                case 32...126: bytes.append(UInt8(unit))
                default: bytes += Array(String(format: "\\u%04x", unit).utf8)
                }
            }
            bytes.append(34)
            return bytes
        case .array(let values):
            var bytes: [UInt8] = [91]
            for (index, value) in values.enumerated() {
                if index > 0 { bytes.append(44) }
                bytes += value.canonical
            }
            bytes.append(93)
            return bytes
        case .object(let values):
            var bytes: [UInt8] = [123]
            let keys = values.keys.sorted()
            for (index, key) in keys.enumerated() {
                if index > 0 { bytes.append(44) }
                bytes += DecisionJSON.string(key.rawValue).canonical
                bytes.append(58)
                bytes += values[key]!.canonical
            }
            bytes.append(125)
            return bytes
        }
    }

    struct Parser {
        let bytes: [UInt8]
        var index = 0
        var nodes = 0

        init(_ data: Data) throws {
            try decisionRequire(!data.isEmpty && data.count <= 32 * 1024 * 1024, "Decision publication is empty or exceeds 32 MiB")
            bytes = Array(data)
        }

        mutating func parse() throws -> DecisionJSON {
            let result = try value(depth: 0)
            whitespace()
            try decisionRequire(index == bytes.count, "Trailing decision content")
            return result
        }

        mutating func whitespace() {
            while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 }
        }

        mutating func take(_ byte: UInt8) -> Bool {
            whitespace()
            guard index < bytes.count && bytes[index] == byte else { return false }
            index += 1
            return true
        }

        mutating func value(depth: Int) throws -> DecisionJSON {
            nodes += 1
            try decisionRequire(depth <= 64 && nodes <= 1_000_000, "Decision parsing budget exceeded")
            whitespace()
            guard index < bytes.count else { throw DecisionError.invalid("Truncated decision") }
            switch bytes[index] {
            case 123:
                index += 1
                var fields: [DecisionIdentity: DecisionJSON] = [:]
                if take(125) { return .object(fields) }
                repeat {
                    let key = DecisionIdentity(try string())
                    try decisionRequire(fields[key] == nil, "Duplicate decision key")
                    try decisionRequire(take(58), "Missing decision colon")
                    fields[key] = try value(depth: depth + 1)
                    if take(125) { return .object(fields) }
                    try decisionRequire(take(44), "Missing decision comma")
                } while true
            case 91:
                index += 1
                var values: [DecisionJSON] = []
                if take(93) { return .array(values) }
                repeat {
                    values.append(try value(depth: depth + 1))
                    if take(93) { return .array(values) }
                    try decisionRequire(take(44), "Missing decision array comma")
                } while true
            case 34: return .string(try string())
            case 116: try literal("true"); return .bool(true)
            case 102: try literal("false"); return .bool(false)
            case 110: try literal("null"); return .null
            default: return .number(try number())
            }
        }

        mutating func literal(_ text: String) throws {
            let expected = Array(text.utf8)
            try decisionRequire(index + expected.count <= bytes.count && Array(bytes[index..<index + expected.count]) == expected, "Invalid decision literal")
            index += expected.count
        }

        mutating func string() throws -> String {
            whitespace()
            let start = index
            try decisionRequire(index < bytes.count && bytes[index] == 34, "Expected decision string")
            index += 1
            while index < bytes.count {
                let byte = bytes[index]
                index += 1
                if byte == 34 {
                    return try JSONDecoder().decode(String.self, from: Data(bytes[start..<index]))
                }
                try decisionRequire(byte >= 32, "Control character in decision string")
                if byte == 92 {
                    try decisionRequire(index < bytes.count, "Truncated decision escape")
                    index += 1
                }
            }
            throw DecisionError.invalid("Truncated decision string")
        }

        mutating func number() throws -> String {
            let start = index
            if index < bytes.count && bytes[index] == 45 { index += 1 }
            if index < bytes.count && bytes[index] == 48 { index += 1 }
            else {
                try decisionRequire(index < bytes.count && (49...57).contains(bytes[index]), "Invalid decision number")
                while index < bytes.count && (48...57).contains(bytes[index]) { index += 1 }
            }
            var floating = false
            if index < bytes.count && bytes[index] == 46 {
                floating = true
                index += 1
                let digits = index
                while index < bytes.count && (48...57).contains(bytes[index]) { index += 1 }
                try decisionRequire(index > digits, "Invalid decimal")
            }
            if index < bytes.count && [69, 101].contains(bytes[index]) {
                floating = true
                index += 1
                if index < bytes.count && [43, 45].contains(bytes[index]) { index += 1 }
                let digits = index
                while index < bytes.count && (48...57).contains(bytes[index]) { index += 1 }
                try decisionRequire(index > digits, "Invalid exponent")
            }
            let text = String(decoding: bytes[start..<index], as: UTF8.self)
            try decisionRequire(text.count <= 1024, "Decision number budget exceeded")
            guard let value = Double(text), value.isFinite else { throw DecisionError.invalid("Nonfinite decision number") }
            let mantissa = text.lowercased().split(separator: "e", omittingEmptySubsequences: false)[0]
            try decisionRequire(value != 0 || !mantissa.utf8.contains(where: { (49...57).contains($0) }), "Decision number underflows to zero")
            return floating ? String(value) : (text == "-0" ? "0" : text)
        }
    }
}

enum DecisionSHA256 {
    static func hex(_ bytes: [UInt8]) -> String {
        let constants: [UInt32] = [
            0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
            0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
            0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
            0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
            0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
            0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
            0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
            0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]
        func rotate(_ value: UInt32, _ bits: UInt32) -> UInt32 { (value >> bits) | (value << (32 - bits)) }
        var message = bytes
        let length = UInt64(message.count) * 8
        message.append(128)
        while message.count % 64 != 56 { message.append(0) }
        for shift in stride(from: 56, through: 0, by: -8) { message.append(UInt8(truncatingIfNeeded: length >> shift)) }
        var hash: [UInt32] = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
        for offset in stride(from: 0, to: message.count, by: 64) {
            var words = [UInt32](repeating: 0, count: 64)
            for i in 0..<16 {
                let j = offset + i * 4
                words[i] = UInt32(message[j]) << 24 | UInt32(message[j + 1]) << 16 | UInt32(message[j + 2]) << 8 | UInt32(message[j + 3])
            }
            for i in 16..<64 {
                let x = words[i - 15], y = words[i - 2]
                let a = rotate(x, 7) ^ rotate(x, 18) ^ (x >> 3)
                let b = rotate(y, 17) ^ rotate(y, 19) ^ (y >> 10)
                words[i] = words[i - 16] &+ a &+ words[i - 7] &+ b
            }
            var a = hash[0], b = hash[1], c = hash[2], d = hash[3], e = hash[4], f = hash[5], g = hash[6], h = hash[7]
            for i in 0..<64 {
                let first = h &+ (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) &+ ((e & f) ^ (~e & g)) &+ constants[i] &+ words[i]
                let second = (rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) &+ ((a & b) ^ (a & c) ^ (b & c))
                h = g; g = f; f = e; e = d &+ first; d = c; c = b; b = a; a = first &+ second
            }
            for (i, value) in [a,b,c,d,e,f,g,h].enumerated() { hash[i] = hash[i] &+ value }
        }
        return hash.map { String(format: "%08x", $0) }.joined()
    }
}
