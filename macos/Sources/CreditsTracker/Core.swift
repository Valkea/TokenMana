import Foundation

enum CoreError: LocalizedError {
    case missing(String)
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .missing(let s), .failed(let s): return s
        }
    }
}

/// Runs the shared Python core (`ctrack.py`, bundled in the app's Resources).
enum Core {
    static let script: String? = {
        if let p = ProcessInfo.processInfo.environment["CTRACK_PATH"], FileManager.default.fileExists(atPath: p) {
            return p
        }
        return Bundle.main.url(forResource: "ctrack", withExtension: "py")?.path
    }()

    static let python: String? = {
        let candidates = [ProcessInfo.processInfo.environment["CTRACK_PYTHON"],
                          "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        return candidates.compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }()

    static func makeProcess(_ args: [String]) throws -> Process {
        guard let python else { throw CoreError.missing("python3 not found (install Xcode CLT or Homebrew python)") }
        guard let script else { throw CoreError.missing("ctrack.py not found in the app bundle") }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-I", script] + args
        p.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        return p
    }

    /// Runs `ctrack <args>` and returns stdout. Fails only if the process wrote nothing to stdout.
    static func run(_ args: [String]) async throws -> Data {
        let p = try makeProcess(args)
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        try p.run()
        return try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global().async {
                let errBox = DataBox()
                let reader = DispatchQueue.global()
                let group = DispatchGroup()
                group.enter()
                reader.async {
                    errBox.data = err.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                let outData = out.fileHandleForReading.readDataToEndOfFile()
                group.wait()
                p.waitUntilExit()
                if !outData.isEmpty {
                    cont.resume(returning: outData)
                } else {
                    let msg = String(decoding: errBox.data, as: UTF8.self)
                        .split(separator: "\n").last.map(String.init) ?? "ctrack exited with \(p.terminationStatus)"
                    cont.resume(throwing: CoreError.failed(msg))
                }
            }
        }
    }
}

final class DataBox: @unchecked Sendable {
    var data = Data()
}

/// A running `ctrack login <id> --json`, streaming one JSON event per line.
final class LoginSession: @unchecked Sendable {
    private let process: Process
    private let buffer = DataBox()

    init(accountId: String,
         onEvent: @escaping @Sendable ([String: Any]) -> Void,
         onExit: @escaping @Sendable (Int32) -> Void) throws {
        process = try Core.makeProcess(["login", accountId, "--json"])
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let buffer = self.buffer
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            buffer.data.append(chunk)
            while let nl = buffer.data.firstIndex(of: 0x0A) {
                let line = buffer.data[buffer.data.startIndex..<nl]
                buffer.data.removeSubrange(buffer.data.startIndex...nl)
                if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                    onEvent(obj)
                }
            }
        }
        process.terminationHandler = { p in onExit(p.terminationStatus) }
        try process.run()
    }

    func cancel() {
        if process.isRunning { process.terminate() }
    }
}
