import Foundation

/// Mirrors the JSON printed by `ctrack status --json`.
struct StatusResult: Decodable {
    let generatedAt: String
    let refreshMinutes: Double?
    let configPath: String?
    let summary: Summary?
    let groups: [String]?
    let groupColors: [String: String]?
    let accounts: [Account]

    static func decode(_ data: Data) throws -> StatusResult {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(StatusResult.self, from: data)
    }
}

struct Summary: Decodable {
    let percent: Double
    let account: String
    let metric: String
    let label: String
}

struct Account: Decodable, Identifiable {
    let id: String
    let label: String
    let type: String
    let vendor: String
    let plan: String?
    let identity: String?
    let status: String          // ok | stale | error | setup | pending
    let error: String?
    let hint: String?
    let fetchedAt: String?
    let okAt: String?
    let canPing: Bool
    let canLogin: Bool
    let cli: String?
    let host: String?
    let group: String?
    let dashboardUrl: String?
    let metrics: [Metric]
    let notes: [String]

    var isSetup: Bool { status == "setup" }
    var hasIdleSession: Bool { metrics.contains { $0.key == "session" && $0.idle } }
}

struct Metric: Decodable, Identifiable {
    var id: String { key }
    let key: String
    let label: String
    let percent: Double?
    let text: String
    let resetsAt: String?
    let windowSeconds: Double?
    let idle: Bool

    var resetDate: Date? { resetsAt.flatMap(parseISO) }
}

struct PingResults: Decodable {
    struct Item: Decodable {
        let id: String
        let ok: Bool
        let seconds: Double?
        let output: String?
        let error: String?
    }
    let results: [Item]
}

func parseISO(_ s: String) -> Date? {
    let f = ISO8601DateFormatter()
    if let d = f.date(from: s) { return d }
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.date(from: s)
}
