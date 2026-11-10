import AppKit
import Foundation
import ServiceManagement

struct LoginState {
    let accountId: String
    let label: String
    var userCode: String?
    var url: String?
    var message: String
    var finished = false
    var failed = false
}

struct AddForm {
    var type = "claude"
    var label = ""
    var command = ""
    var host = ""
    var group = ""
    var busy = false
    var error: String?
}

@MainActor
final class Store: ObservableObject {
    static let shared = Store()

    @Published var status: StatusResult?
    @Published var loadError: String?
    @Published var refreshing = false
    @Published var pinging: Set<String> = []
    @Published var pingMessages: [String: (ok: Bool, text: String)] = [:]
    @Published var login: LoginState?
    @Published var now = Date()
    @Published var lastLoaded: Date?
    @Published var addForm: AddForm?

    private var loading = false
    private var autoPinging = false
    private var loginSession: LoginSession?
    private var timers: [Timer] = []

    init() {
        Task {
            await load(["--cached"])   // instant paint from cache
            await load([])             // then fetch whatever is due
            await autoPing()
        }
        // The core only hits the network when an account's cache is older than its
        // refresh interval, so polling every minute is cheap.
        // Timers run in .common modes so they also fire while the menu is open.
        let poll = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in
                await self?.load([], poll: true)
                await self?.autoPing()
            }
        }
        let tick = Timer(timeInterval: 20, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = Date() }
        }
        for t in [poll, tick] { RunLoop.main.add(t, forMode: .common) }
        timers = [poll, tick]
    }

    var activeAccounts: [Account] { status?.accounts.filter { !$0.isSetup } ?? [] }
    var setupAccounts: [Account] { status?.accounts.filter { $0.isSetup } ?? [] }
    var anyIdle: Bool { activeAccounts.contains { $0.canPing && $0.hasIdleSession } }
    /// "id@slot" of each missed scheduled ping waiting for an answer.
    var missedPings: Set<String> {
        Set((status?.accounts ?? []).compactMap { a in a.autoPing?.missed.map { "\(a.id)@\($0)" } })
    }

    /// `mana status` → `status`. `poll` loads are skipped while another load is in flight.
    func load(_ extra: [String], poll: Bool = false) async {
        if poll && loading { return }
        loading = true
        defer { loading = false }
        do {
            let data = try await Core.run(["status", "--json", "--compact"] + extra)
            status = try StatusResult.decode(data)
            loadError = nil
            lastLoaded = Date()
            MissedPingNotifier.shared.sync(status?.accounts ?? [])
        } catch {
            loadError = error.localizedDescription
        }
        now = Date()
    }

    /// Scheduled pings: the core decides what is due (every 5h, or at set times) and pings it.
    func autoPing() async {
        guard !autoPinging, status?.accounts.contains(where: { $0.autoPing != nil }) == true else { return }
        autoPinging = true
        defer { autoPinging = false }
        let before = missedPings
        guard let data = try? await Core.run(["auto-ping", "--json"]),
              let res = try? JSONDecoder().decode(AutoPingResult.self, from: data) else { return }
        if !res.pinged.isEmpty || Set(res.missed.map { "\($0.id)@\($0.slot)" }) != before {
            await load(["--cached"])
        }
    }

    /// "Wait for the next scheduled ping": drop the missed-ping question.
    func dismissMissed(_ id: String) {
        Task {
            _ = try? await Core.run(["auto-ping", "--dismiss", id, "--json"])
            await load(["--cached"])
        }
    }

    func refreshAccount(_ id: String) async {
        _ = try? await Core.run(["status", "--json", "--compact", "--force", id])
        await load(["--cached"])
    }

    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        Task {
            await load(["--force"])
            refreshing = false
        }
    }

    func ping(_ ids: [String]) {
        let ids = ids.filter { !pinging.contains($0) }
        guard !ids.isEmpty else { return }
        ids.forEach { pinging.insert($0); pingMessages[$0] = nil }
        Task {
            do {
                let data = try await Core.run(["ping", "--json"] + ids)
                let res = try JSONDecoder().decode(PingResults.self, from: data)
                for r in res.results {
                    pingMessages[r.id] = r.ok
                        ? (true, "Pinged at \(Fmt.clock(Date(), now: Date())) — session timer started")
                        : (false, "Ping failed: \(r.error ?? "unknown error")")
                }
            } catch {
                ids.forEach { pingMessages[$0] = (false, "Ping failed: \(error.localizedDescription)") }
            }
            ids.forEach { pinging.remove($0) }
            await load(["--cached"])
            try? await Task.sleep(nanoseconds: 90_000_000_000)
            ids.forEach { pingMessages[$0] = nil }
        }
    }

    func pingIdle() {
        ping(activeAccounts.filter { $0.canPing && $0.hasIdleSession }.map(\.id))
    }

    func startLogin(_ account: Account) {
        loginSession?.cancel()
        login = LoginState(accountId: account.id, label: account.label, message: "Starting sign-in…")
        do {
            loginSession = try LoginSession(accountId: account.id, onEvent: { [weak self] event in
                Task { @MainActor in self?.handleLoginEvent(event) }
            }, onExit: { [weak self] code in
                Task { @MainActor in self?.loginExited(account.id, code: code) }
            })
        } catch {
            login?.message = error.localizedDescription
            login?.failed = true
        }
    }

    private func handleLoginEvent(_ e: [String: Any]) {
        guard login != nil else { return }
        switch e["event"] as? String {
        case "code":
            login?.userCode = e["user_code"] as? String
            login?.url = e["verification_uri"] as? String
            login?.message = (e["copied"] as? Bool == true)
                ? "Code copied — paste it in the browser page that just opened."
                : "Enter this code in the browser page that just opened."
        case "url":
            login?.url = e["url"] as? String
            login?.message = "Finish signing in in your browser…"
        case "log":
            if login?.userCode == nil, let line = e["line"] as? String { login?.message = line }
        case "done":
            login?.message = (e["message"] as? String) ?? "Signed in"
            login?.finished = true
        case "error":
            login?.message = (e["message"] as? String) ?? "Sign-in failed"
            login?.failed = true
        default:
            break
        }
    }

    private func loginExited(_ id: String, code: Int32) {
        loginSession = nil
        if code == 0 {
            login?.finished = true
            Task {
                await refreshAccount(id)
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                if login?.accountId == id, login?.failed == false { login = nil }
            }
        } else if login?.failed == false && login?.finished == false {
            login?.failed = true
            login?.message = "Sign-in was cancelled or failed (exit \(code))"
        }
    }

    func addAccount() {
        guard let form = addForm, !form.busy else { return }
        addForm?.busy = true
        addForm?.error = nil
        Task {
            var args = ["add", form.type, "--json"]
            let label = form.label.trimmingCharacters(in: .whitespaces)
            if !label.isEmpty { args += ["--label", label] }
            if form.type == "command" { args += ["--command", form.command] }
            let group = form.group.trimmingCharacters(in: .whitespaces)
            if !group.isEmpty { args += ["--group", group] }
            if form.type == "copilot", !form.host.trimmingCharacters(in: .whitespaces).isEmpty {
                args += ["--host", form.host.trimmingCharacters(in: .whitespaces)]
            }
            do {
                let data = try await Core.run(args)
                guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let id = obj["id"] as? String else { throw CoreError.failed("unexpected output from mana add") }
                addForm = nil
                await refreshAccount(id)
                if let acc = status?.accounts.first(where: { $0.id == id }), acc.canLogin {
                    startLogin(acc)
                }
            } catch {
                addForm?.busy = false
                addForm?.error = error.localizedDescription
            }
        }
    }

    var groups: [String] { status?.groups ?? [] }

    /// `autoPing`: "off", "5h" or "08:00, 13:01".
    func editAccount(_ id: String, label: String? = nil, group: String? = nil, host: String? = nil, autoPing: String? = nil) {
        Task {
            var args = ["edit", id, "--json"]
            if let label { args += ["--label", label] }
            if let group { args += ["--group", group] }
            if let host { args += ["--host", host] }
            if let autoPing { args += ["--auto-ping", autoPing] }
            var failure: String?
            do {
                _ = try await Core.run(args)
            } catch {
                failure = error.localizedDescription  // e.g. an invalid auto-ping time
            }
            await refreshAccount(id)
            if let failure { loadError = failure }  // after the reload, which would clear it
        }
    }

    func removeAccount(_ id: String) {
        Task {
            _ = try? await Core.run(["remove", id, "--json"])
            await load(["--cached"])
        }
    }

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func cancelLogin() {
        loginSession?.cancel()
        loginSession = nil
        login = nil
    }

    func open(_ url: String) {
        if let u = URL(string: url) { NSWorkspace.shared.open(u) }
    }

    func openConfig() {
        guard let path = status?.configPath else { return }
        let url = URL(fileURLWithPath: path)
        // Prefer a text editor over whatever owns .json on this Mac.
        let textEdit = URL(fileURLWithPath: "/System/Applications/TextEdit.app")
        if NSWorkspace.shared.urlForApplication(toOpen: url) != nil {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.open([url], withApplicationAt: textEdit, configuration: NSWorkspace.OpenConfiguration())
        }
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                loadError = "Launch at login: \(error.localizedDescription)"
            }
            objectWillChange.send()
        }
    }
}
