import AppKit
import Combine
import SwiftUI

/// Small standard window for the two flows a menu can't host: adding an account and signing in.
@MainActor
final class AccountWindowController: NSObject, NSWindowDelegate {
    static let shared = AccountWindowController()

    private var window: NSWindow?
    private var cancellable: AnyCancellable?
    private var closing = false

    func show() {
        if window == nil {
            let host = NSHostingController(rootView: AccountFlowView().environmentObject(Store.shared))
            host.sizingOptions = [.preferredContentSize]
            let w = NSWindow(contentViewController: host)
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            w.delegate = self
            window = w
            cancellable = Store.shared.objectWillChange.sink { [weak self] _ in
                DispatchQueue.main.async { self?.sync() }
            }
        }
        sync()
        NSApp.activate(ignoringOtherApps: true)
        window?.center()
        window?.makeKeyAndOrderFront(nil)
    }

    private func sync() {
        let store = Store.shared
        if store.addForm == nil && store.login == nil {
            if window?.isVisible == true { window?.close() }
        } else {
            window?.title = store.login != nil ? "Sign In" : "Add Account"
        }
    }

    func windowWillClose(_ notification: Notification) {
        guard !closing else { return }
        closing = true
        let store = Store.shared
        if let login = store.login, !login.finished { store.cancelLogin() } else { store.login = nil }
        store.addForm = nil
        closing = false
    }
}

struct AccountFlowView: View {
    @EnvironmentObject var store: Store

    var body: some View {
        Group {
            if let login = store.login {
                SignInStep(state: login)
            } else {
                AddStep()
            }
        }
        .padding(20)
        .frame(width: 440, alignment: .topLeading)
    }
}

private struct AddStep: View {
    @EnvironmentObject var store: Store

    private static let help: [String: String] = [
        "claude": "Gets its own Claude Code config dir (~/.claude-<name>), so several subscriptions coexist. Sign-in opens your browser.",
        "codex": "Gets its own CODEX_HOME (~/.codex-<name>). Requires the codex CLI. Sign-in opens your browser.",
        "copilot": "Signs in with a GitHub device code, using the account that holds the Copilot seat. For GitHub Enterprise with data residency, enter its host (e.g. mycorp.ghe.com) and sign in there first.",
        "command": "Any command printing its usage as TokenMana JSON (see the README, “Your own provider”).",
    ]

    private func binding<T>(_ key: WritableKeyPath<AddForm, T>, _ fallback: T) -> Binding<T> {
        Binding(get: { store.addForm?[keyPath: key] ?? fallback },
                set: { store.addForm?[keyPath: key] = $0 })
    }

    var body: some View {
        let form = store.addForm ?? AddForm()
        VStack(alignment: .leading, spacing: 14) {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 10, verticalSpacing: 12) {
                GridRow {
                    Text("Type:").gridColumnAlignment(.trailing)
                    Picker("", selection: binding(\.type, "claude")) {
                        Text("Claude").tag("claude")
                        Text("Codex").tag("codex")
                        Text("Copilot").tag("copilot")
                        Text("Command").tag("command")
                    }
                    .pickerStyle(.segmented).labelsHidden()
                }
                GridRow {
                    Text("Label:")
                    TextField("e.g. Claude Max · work", text: binding(\.label, ""))
                        .onSubmit { store.addAccount() }
                }
                GridRow {
                    Text("Group:")
                    HStack(spacing: 6) {
                        TextField("optional — e.g. Work, Personal", text: binding(\.group, ""))
                        if !store.groups.isEmpty {
                            Menu {
                                ForEach(store.groups, id: \.self) { g in
                                    Button(g) { store.addForm?.group = g }
                                }
                            } label: {
                                Image(systemName: "chevron.down")
                            }
                            .menuStyle(.borderlessButton)
                            .menuIndicator(.hidden)
                            .fixedSize()
                        }
                    }
                }
                if form.type == "copilot" {
                    GridRow {
                        Text("GitHub host:")
                        TextField("github.com — or e.g. mycorp.ghe.com", text: binding(\.host, ""))
                    }
                }
                if form.type == "command" {
                    GridRow {
                        Text("Command:")
                        TextField("e.g. my-usage --json", text: binding(\.command, ""))
                            .font(.system(.body, design: .monospaced))
                    }
                }
            }
            Text(Self.help[form.type] ?? "")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let err = form.error {
                Text(err).font(.callout).foregroundStyle(.red)
            }
            HStack {
                if form.busy { ProgressView().controlSize(.small) }
                Spacer()
                Button("Cancel") { store.addForm = nil }.keyboardShortcut(.cancelAction)
                Button(form.type == "command" ? "Add" : "Add & Sign In") { store.addAccount() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(form.busy)
            }
        }
    }
}

private struct SignInStep: View {
    @EnvironmentObject var store: Store
    let state: LoginState

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                if let acc = store.status?.accounts.first(where: { $0.id == state.accountId }) {
                    Image(nsImage: VendorImage.badge(acc.vendor, size: 28))
                }
                Text(state.label).font(.headline)
            }
            if let code = state.userCode {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Enter this code on GitHub:").foregroundStyle(.secondary)
                    HStack(spacing: 10) {
                        Text(code)
                            .font(.system(size: 26, weight: .semibold, design: .monospaced))
                            .textSelection(.enabled)
                        Button("Copy") { store.copy(code) }
                        if let url = state.url {
                            Button("Open GitHub") { store.open(url) }
                        }
                    }
                }
            } else if let url = state.url, !state.finished {
                Button("Open Sign-In Page") { store.open(url) }
            }
            HStack(spacing: 8) {
                if state.finished {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                } else if state.failed {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                } else {
                    ProgressView().controlSize(.small)
                }
                Text(state.message)
                    .foregroundStyle(state.failed ? .red : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                if state.finished || state.failed {
                    Button("Done") { store.login = nil }.keyboardShortcut(.defaultAction)
                } else {
                    Button("Cancel") { store.cancelLogin() }.keyboardShortcut(.cancelAction)
                }
            }
        }
    }
}
