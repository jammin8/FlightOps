import Foundation
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

enum Supervised {
    static let outputLimit = 32 * 1024 * 1024
    static let nodeLimit = 400_000
    static let objectCost = 5
    static let grace: TimeInterval = 1
    static let linger: TimeInterval = 0.2
    private static let slice: TimeInterval = 0.05
    private static let chunk = 65536

    enum Outcome: Equatable {
        case finished(Data), failed, timedOut, tooLarge, notStarted

        var data: Data? {
            if case .finished(let data) = self { return data }
            return nil
        }

        var problem: String? {
            switch self {
            case .finished: return nil
            case .failed: return "collector failed"
            case .timedOut: return "collector timed out"
            case .tooLarge: return "collector output too large"
            case .notStarted: return "collector could not start"
            }
        }
    }

    static func nodes(in data: Data) -> Int {
        var count = 0, quoted = false, escaped = false
        for byte in data {
            if quoted {
                if escaped { escaped = false } else if byte == 92 { escaped = true } else if byte == 34 { quoted = false }
            } else if byte == 34 {
                quoted = true
            } else if byte == 123 {
                count += objectCost
            } else if byte == 44 || byte == 58 || byte == 91 {
                count += 1
            }
        }
        return count
    }

    static func limited(_ text: String?, to limit: TimeInterval) -> TimeInterval {
        guard let value = text.flatMap(TimeInterval.init), value.isFinite, value > 0 else { return limit }
        return min(value, limit)
    }

    static func run(_ executable: String, _ arguments: [String], environment: [String: String]? = nil, timeout: TimeInterval,
                    grace: TimeInterval = Supervised.grace, limit: Int = Supervised.outputLimit) -> Outcome {
        var ends: [Int32] = [0, 0]
        guard pipe(&ends) == 0 else { return .notStarted }
        let reader = ends[0], writer = ends[1]
        defer { close(reader) }
        for fd in ends { _ = fcntl(fd, F_SETFD, FD_CLOEXEC) }
        _ = fcntl(reader, F_SETFL, fcntl(reader, F_GETFL) | O_NONBLOCK)
        let pid = spawn(executable, arguments, environment ?? ProcessInfo.processInfo.environment, stdout: writer)
        close(writer)
        guard let pid else { return .notStarted }
        let deadline = clock() + timeout
        var output = Data(), ended = false, status: Int32?, exitedAt: TimeInterval?
        while true {
            if status == nil, let code = reap(pid) {
                status = code
                exitedAt = clock()
            }
            if ended && status != nil {
                stop(pid, grace: grace)
                break
            }
            let now = clock()
            if status == nil && now >= deadline {
                stop(pid, grace: grace)
                return .timedOut
            }
            if let exitedAt, now >= exitedAt + linger {
                stop(pid, grace: grace)
                _ = read(reader, into: &output, limit: limit, wait: 0)
                break
            }
            if ended {
                usleep(5000)
                continue
            }
            switch read(reader, into: &output, limit: limit, wait: min(slice, max(0, deadline - now))) {
            case .ended: ended = true
            case .overflow:
                stop(pid, grace: grace)
                return .tooLarge
            case .more: break
            }
        }
        guard let exitedAt, exitedAt <= deadline else { return .timedOut }
        guard output.count <= limit else { return .tooLarge }
        return status == 0 ? .finished(output) : .failed
    }

    private enum Reading { case more, ended, overflow }

    private static func read(_ fd: Int32, into output: inout Data, limit: Int, wait: TimeInterval) -> Reading {
        var waiting = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
        guard poll(&waiting, 1, Int32((wait * 1000).rounded(.up))) > 0 else { return .more }
        var buffer = [UInt8](repeating: 0, count: chunk)
        while true {
            let count = buffer.withUnsafeMutableBytes { readBytes(fd, $0.baseAddress, chunk) }
            if count == 0 { return .ended }
            guard count > 0 else { return .more }
            guard output.count + count <= limit else { return .overflow }
            output.append(contentsOf: buffer[0..<count])
        }
    }

    private static func readBytes(_ fd: Int32, _ buffer: UnsafeMutableRawPointer?, _ count: Int) -> Int {
        #if canImport(Glibc)
        return Glibc.read(fd, buffer, count)
        #else
        return Darwin.read(fd, buffer, count)
        #endif
    }

    private static func clock() -> TimeInterval { TimeInterval(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000 }

    private static func reap(_ pid: pid_t) -> Int32? {
        var status: Int32 = 0
        let answer = waitpid(pid, &status, WNOHANG)
        if answer == pid { return status }
        return answer < 0 && errno != EINTR ? -1 : nil
    }

    private static func stop(_ pid: pid_t, grace: TimeInterval) {
        kill(-pid, SIGTERM)
        var reaped = false
        let end = clock() + grace
        while clock() < end {
            if !reaped { reaped = reap(pid) != nil }
            if reaped && kill(-pid, 0) != 0 { return }
            usleep(10000)
        }
        kill(-pid, SIGKILL)
        let last = clock() + grace
        while !reaped && clock() < last {
            reaped = reap(pid) != nil
            usleep(10000)
        }
        if !reaped {
            DispatchQueue.global(qos: .utility).async {
                var status: Int32 = 0
                _ = waitpid(pid, &status, 0)
            }
        }
    }

    private static func spawn(_ executable: String, _ arguments: [String], _ environment: [String: String], stdout: Int32) -> pid_t? {
        #if canImport(Darwin)
        var actions: posix_spawn_file_actions_t? = nil
        var attributes: posix_spawnattr_t? = nil
        let flags = POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT
        #else
        var actions = posix_spawn_file_actions_t()
        var attributes = posix_spawnattr_t()
        let flags = POSIX_SPAWN_SETPGROUP
        #endif
        guard posix_spawn_file_actions_init(&actions) == 0 else { return nil }
        defer { posix_spawn_file_actions_destroy(&actions) }
        guard posix_spawnattr_init(&attributes) == 0 else { return nil }
        defer { posix_spawnattr_destroy(&attributes) }
        guard posix_spawnattr_setflags(&attributes, Int16(flags)) == 0, posix_spawnattr_setpgroup(&attributes, 0) == 0,
              posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0) == 0,
              posix_spawn_file_actions_adddup2(&actions, stdout, 1) == 0,
              posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0) == 0 else { return nil }
        let argv: [UnsafeMutablePointer<CChar>?] = ([executable] + arguments).map { strdup($0) } + [nil]
        let envp: [UnsafeMutablePointer<CChar>?] = environment.map { strdup($0.key + "=" + $0.value) } + [nil]
        defer {
            for pointer in argv + envp { free(pointer) }
        }
        var pid: pid_t = 0
        return posix_spawn(&pid, executable, &actions, &attributes, argv, envp) == 0 ? pid : nil
    }
}
