import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

enum SummaryWriters {
    static func system() -> SummaryWriting? {
#if canImport(FoundationModels)
        if #available(macOS 26.0, *) { return AppleSummaryWriter() }
#endif
        return nil
    }
}

#if canImport(FoundationModels)
@available(macOS 26.0, *)
struct AppleSummaryWriter: SummaryWriting {
    var available: Bool {
        if case .available = SystemLanguageModel.default.availability { return true }
        return false
    }

    func choose(instructions: String, prompt: String) async -> [String]? {
        guard available else { return nil }
        let session = LanguageModelSession(instructions: instructions)
        let options = GenerationOptions(temperature: 0)
        return try? await session.respond(to: prompt, generating: SummaryPicks.self, options: options).content.ids
    }
}

@available(macOS 26.0, *)
@Generable
struct SummaryPicks {
    @Guide(description: "Fact ids in order of importance, most important first, written exactly as listed", .maximumCount(3))
    var ids: [String]
}
#endif
