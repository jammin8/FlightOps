import Foundation

@MainActor final class SummaryStore {
    private var cache: SummaryCache = Monitor.load("summary-cache.json") ?? SummaryCache()
    private var memory: ForecastMemory = Monitor.load("forecast-memory.json") ?? ForecastMemory()
    private let writer: SummaryWriting?
    private let publish: (String?) -> Void
    private var wanted: String?
    private var running: String?
    private var retryAfter: [String: Date] = [:]

    static let retryDelay: TimeInterval = 600

    init(writer: SummaryWriting?, publish: @escaping (String?) -> Void) {
        self.writer = writer
        self.publish = publish
    }

    func update(_ w: World, flow: [FlowSample]) {
        let remembered = memory.recording(ForecastMemory.current(w), at: w.now.timeIntervalSince1970)
        if remembered != memory {
            memory = remembered
            Monitor.save(memory, "forecast-memory.json")
        }
        guard let writer, writer.available else { wanted = nil; publish(nil); return }
        let facts = SummaryFacts.build(w, flow: flow, memory: memory)
        switch SummaryPlan.decide(facts, cache: cache) {
        case .none:
            wanted = nil
            publish(nil)
        case .cached(let text):
            wanted = nil
            publish(text)
        case .generate(let key, let instructions, let prompt):
            wanted = key
            publish(nil)
            guard let facts, running == nil, (retryAfter[key] ?? .distantPast) <= w.now else { return }
            running = key
            Task { [weak self] in
                let picks = await writer.choose(instructions: instructions, prompt: prompt)
                self?.finish(picks, facts: facts, key: key)
            }
        }
    }

    private func finish(_ picks: [String]?, facts: SummaryFacts, key: String) {
        running = nil
        guard let picks else {
            retryAfter[key] = Date().addingTimeInterval(Self.retryDelay)
            return
        }
        let settled = SummaryPlan.settle(picks, facts: facts, key: key, cache: cache)
        cache = settled.cache
        Monitor.save(cache, "summary-cache.json")
        if wanted == key { publish(settled.text) }
    }
}
