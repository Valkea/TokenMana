import AppKit
import Combine
import ServiceManagement

/// Native status item + NSMenu (same look, placement and behaviour as the system's own menus).
@MainActor
final class StatusMenuController: NSObject, NSMenuDelegate {
    private let store: Store
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private var cancellables: Set<AnyCancellable> = []
    private var structure = ""
    private var metricItems: [String: NSMenuItem] = [:]
    private var stripe: NSColor?  // colour of the group being built
    private var layout = MetricLayout(valueRight: 0, barLeft: 0, resetLeft: 0)
    private var updateQueued = false

    private var showPercent: Bool {
        get { UserDefaults.standard.object(forKey: "showPercent") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "showPercent") }
    }

    /// Id of the account shown in the menu bar; empty = the one with the highest usage.
    private var panelAccount: String {
        get { UserDefaults.standard.string(forKey: "panelAccount") ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: "panelAccount") }
    }

    init(store: Store) {
        self.store = store
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.font = .monospacedDigitSystemFont(ofSize: NSFont.menuBarFont(ofSize: 0).pointSize, weight: .regular)
        store.objectWillChange.sink { [weak self] _ in self?.queueUpdate() }.store(in: &cancellables)
        update()
    }

    func menuWillOpen(_ menu: NSMenu) {
        store.now = Date()
        Task { await store.load([]) }  // fetch whatever is due; the menu updates live
    }

    // MARK: Update

    private func queueUpdate() {
        guard !updateQueued else { return }
        updateQueued = true
        // objectWillChange fires before the change lands; read the new state on the next turn.
        DispatchQueue.main.async { [weak self] in
            self?.updateQueued = false
            self?.update()
        }
    }

    private func update() {
        updateButton()
        let newLayout = MetricLayout.measure(store.activeAccounts.flatMap(\.metrics))
        let signature = structureSignature()
        if signature != structure || newLayout != layout {
            layout = newLayout
            structure = signature
            rebuild()
        } else {
            // Same layout: refresh bars/countdowns in place (keeps open submenus open).
            for acc in store.activeAccounts {
                stripe = groupColor(acc.group)
                for m in acc.metrics {
                    if let item = metricItems["\(acc.id)/\(m.key)"] { configure(item, metric: m) }
                }
            }
            if let header = menu.item(withTag: Tag.header) { setHeaderSubtitle(header) }
        }
    }

    private func updateButton() {
        guard let button = statusItem.button else { return }
        let (pct, text, tip) = menuBarUsage()
        let image = NSImage(systemSymbolName: Fmt.gaugeSymbol(pct), accessibilityDescription: "Coding assistants usage")?
            .withSymbolConfiguration(.init(pointSize: 14, weight: .regular))
        image?.isTemplate = true
        button.image = image
        button.title = showPercent && !text.isEmpty ? " \(text)" : ""
        button.toolTip = tip
    }

    /// The chosen account (or, on Auto, the one with the highest usage): "5h% · week%".
    /// Returns the gauge percentage, the menu-bar text and the tooltip.
    private func menuBarUsage() -> (Double?, String, String?) {
        let summary = store.status?.summary
        let accounts = store.status?.accounts ?? []
        let chosen = panelAccount.isEmpty ? summary?.account : panelAccount
        guard let acc = accounts.first(where: { $0.id == chosen }) ?? accounts.first(where: { $0.id == summary?.account }) else {
            return (summary?.percent, summary.map { "\(Int($0.percent.rounded()))%" } ?? "", summary?.label)
        }
        func pct(_ m: Metric?) -> Double? { m.flatMap { $0.idle ? 0 : $0.percent } }
        let session = acc.metrics.first { $0.key == "session" }
        let weekly = acc.metrics.first { $0.key == "weekly_all" || $0.key == "weekly" }
        var shown = [pct(session), pct(weekly)].compactMap { $0 }
        var labels = [session, weekly].compactMap { m in m.flatMap { pct($0) != nil ? $0.label : nil } }
        if shown.isEmpty, let worst = acc.metrics.filter({ $0.percent != nil }).max(by: { $0.percent! < $1.percent! }) {
            // Accounts without 5h/weekly windows (Copilot, command accounts): their highest metric.
            shown = [worst.percent!]
            labels = [worst.label]
        }
        guard !shown.isEmpty else { return (nil, "", acc.label) }
        let text = shown.map { "\(Int($0.rounded()))%" }.joined(separator: " · ")
        return (shown.max(), text, "\(acc.label) — \(labels.joined(separator: " · "))")
    }

    /// Everything that changes the list of items (not just the numbers in them).
    private func structureSignature() -> String {
        var parts = [store.groups.joined(separator: ","), "\(store.status?.groupColors ?? [:])", store.loadError ?? "", "\(store.refreshing)", "\(showPercent)", panelAccount, "\(store.launchAtLogin)",
                     "\(store.status == nil)"]
        for a in store.status?.accounts ?? [] {
            parts.append([a.id, a.label, a.group ?? "", a.status, a.plan ?? "", a.identity ?? "", a.error ?? "", a.hint ?? "",
                          a.metrics.map { "\($0.key)\($0.idle)\(Fmt.resetLine($0, now: store.now) != nil)" }.joined(),
                          a.notes.joined(), "\(store.pinging.contains(a.id))", store.pingMessages[a.id]?.text ?? ""]
                .joined(separator: "|"))
        }
        return parts.joined(separator: "\n")
    }

    // MARK: Build

    private enum Tag { static let header = 1 }

    private func rebuild() {
        menu.removeAllItems()
        metricItems.removeAll()

        let header = NSMenuItem(title: "Coding Assistants", action: nil, keyEquivalent: "")
        header.tag = Tag.header
        header.isEnabled = false
        setHeaderSubtitle(header)
        menu.addItem(header)

        if store.status == nil {
            menu.addItem(disabled("Loading…"))
        }
        // One section per group (accounts already sorted by group, then name, by the core).
        let accounts = store.status?.accounts ?? []
        let grouped = !store.groups.isEmpty
        var currentGroup: String?? = .none
        for acc in accounts {
            if currentGroup != .some(acc.group) {
                currentGroup = .some(acc.group)
                stripe = nil
                menu.addItem(.separator())
                if grouped {
                    if menu.numberOfItems > 2 { menu.addItem(spacer()) }
                    stripe = groupColor(acc.group)
                    menu.addItem(groupHeader(acc.group, accounts: accounts.filter { $0.group == acc.group }))
                }
            } else {
                menu.addItem(.separator())  // between subscriptions of the same group
            }
            if acc.isSetup {
                let item = NSMenuItem(title: acc.label, action: nil, keyEquivalent: "")
                item.image = img(VendorImage.badge(acc.vendor), tall: true)
                if #available(macOS 14.4, *) { item.subtitle = acc.host.map { "Not signed in · \($0)" } ?? "Not signed in" }
                item.toolTip = acc.error
                item.submenu = accountMenu(acc)
                menu.addItem(item)
            } else {
                addAccountSection(acc)
            }
        }

        stripe = nil
        menu.addItem(.separator())
        let idle = store.activeAccounts.filter { $0.canPing && $0.hasIdleSession }
        if !idle.isEmpty {
            let ping = action(idle.count == 1 ? "Ping Idle Session" : "Ping \(idle.count) Idle Sessions",
                              #selector(pingIdle), symbol: "bolt.fill")
            if #available(macOS 14.4, *) { ping.subtitle = "Starts their 5h timers (smallest model, low effort)" }
            ping.image = img(ping.image)
            menu.addItem(ping)
        }
        let refresh = action(store.refreshing ? "Refreshing…" : "Refresh", #selector(refresh), key: "r",
                             symbol: "arrow.clockwise")
        refresh.isEnabled = !store.refreshing
        let add = action("Add Account…", #selector(showAddAccount), key: "n", symbol: "plus")
        let settings = settingsItem()
        for item in [refresh, add, settings] {
            item.image = img(item.image)
            menu.addItem(item)
        }
        menu.addItem(.separator())
        menu.addItem(action("Quit CreditsTracker", #selector(quit), key: "q"))
    }

    private func setHeaderSubtitle(_ item: NSMenuItem) {
        guard #available(macOS 14.4, *) else { return }
        if let err = store.loadError {
            item.subtitle = "⚠ \(err)"
        } else if let t = store.lastLoaded {
            let highest = store.status?.summary.map { "Highest \(Int($0.percent.rounded()))% · " } ?? ""
            item.subtitle = "\(highest)Updated \(Fmt.clock(t, now: store.now))"
        }
    }

    private func addAccountSection(_ acc: Account) {
        let head = NSMenuItem(title: acc.label, action: nil, keyEquivalent: "")
        head.attributedTitle = NSAttributedString(string: acc.label, attributes: [
            .font: NSFont.systemFont(ofSize: NSFont.menuFont(ofSize: 0).pointSize, weight: .semibold)])
        head.image = img(VendorImage.badge(acc.vendor), tall: true)
        if #available(macOS 14.4, *) {
            head.subtitle = [acc.plan, acc.identity].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        }
        head.submenu = accountMenu(acc)
        menu.addItem(head)

        for m in acc.metrics {
            let item = info("", action: acc.dashboardUrl != nil ? #selector(openURL(_:)) : nil,
                            represented: acc.dashboardUrl)
            item.indentationLevel = 0
            configure(item, metric: m)
            metricItems["\(acc.id)/\(m.key)"] = item
            menu.addItem(item)
        }
        for note in acc.notes {
            let n = info(note, image: NSImage(systemSymbolName: "info.circle", accessibilityDescription: nil))
            n.indentationLevel = 0
            menu.addItem(n)
        }
        if acc.status == "error" || acc.status == "stale", let err = acc.error {
            let stale = acc.status == "stale"
            let item = info(stale ? "Data from \(Fmt.time(acc.okAt) ?? "earlier") — \(err)" : err,
                            image: MenuIcons.symbol("exclamationmark.triangle.fill", stale ? .systemOrange : .systemRed))
            if #available(macOS 14.4, *) { item.subtitle = acc.hint }
            item.toolTip = [err, acc.hint].compactMap { $0 }.joined(separator: "\n")
            menu.addItem(item)
        }
        if store.pinging.contains(acc.id) {
            menu.addItem(info("Pinging…", image: MenuIcons.symbol("bolt.fill", .systemYellow)))
        } else if let msg = store.pingMessages[acc.id] {
            menu.addItem(info(msg.text, image: MenuIcons.symbol(msg.ok ? "checkmark.circle.fill" : "xmark.octagon.fill",
                                                                 msg.ok ? .systemGreen : .systemRed)))
        }
    }

    /// One aligned row per metric:  ◷ Session · 5h   79%  ▬▬▬▬▭▭  3h 11m · 05:00
    private func configure(_ item: NSMenuItem, metric m: Metric) {
        item.attributedTitle = MetricRow.title(m, layout: layout, now: store.now)
        item.image = MenuIcons.striped(MetricRow.icon(m), color: nil)  // bar only on headers
        if #available(macOS 14.4, *) { item.subtitle = nil }
        item.toolTip = [m.label, m.text, Fmt.resetLine(m, now: store.now)].compactMap { $0 }.joined(separator: " — ")
    }

    /// A plain, indented informational row under an account header.
    private func info(_ title: String, action: Selector? = nil, represented: Any? = nil, image: NSImage? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = action == nil ? nil : self
        item.representedObject = represented
        item.indentationLevel = 0
        item.image = MenuIcons.striped(image, color: nil)
        return item
    }

    private func accountMenu(_ acc: Account) -> NSMenu {
        let sub = NSMenu()
        sub.autoenablesItems = false
        if acc.metrics.contains(where: { $0.percent != nil }) {
            // Checked = this account's usage is the one next to the menu-bar icon; unchecking returns to Auto.
            let shown = panelAccount == acc.id
            let pin = action("Show in Menu Bar", #selector(choosePanelAccount(_:)),
                             represented: shown ? "" : acc.id, symbol: "menubar.rectangle")
            pin.state = shown ? .on : .off
            if #available(macOS 14.4, *) { pin.subtitle = shown ? "Uncheck to go back to Auto (highest usage)" : nil }
            sub.addItem(pin)
            sub.addItem(.separator())
        }
        if acc.canPing {
            let ping = action("Ping Now", #selector(pingAccount(_:)), represented: acc.id, symbol: "bolt.fill")
            if #available(macOS 14.4, *) { ping.subtitle = "Smallest model, low effort — starts the 5h timer" }
            ping.isEnabled = !store.pinging.contains(acc.id)
            sub.addItem(ping)
        }
        if let url = acc.dashboardUrl {
            sub.addItem(action("Open Usage Page", #selector(openURL(_:)), represented: url, symbol: "arrow.up.right.square"))
        }
        if let cli = acc.cli {
            let copy = action("Copy Terminal Command", #selector(copyText(_:)), represented: cli, symbol: "terminal")
            if #available(macOS 14.4, *) { copy.subtitle = cli }
            sub.addItem(copy)
        }
        if acc.canLogin {
            sub.addItem(action(acc.isSetup ? "Sign In…" : "Sign In Again…", #selector(signIn(_:)),
                               represented: acc.id, symbol: "person.crop.circle"))
        }
        if !acc.isSetup {
            sub.addItem(action("Refresh This Account", #selector(refreshAccount(_:)), represented: acc.id))
        }
        sub.addItem(.separator())
        sub.addItem(groupMenuItem(acc))
        let edit = action("Edit Account…", #selector(editAccount(_:)), represented: acc.id, symbol: "pencil")
        if #available(macOS 14.4, *), let host = acc.host { edit.subtitle = "Name, GitHub host (\(host))" }
        sub.addItem(edit)
        sub.addItem(action("Remove Account…", #selector(removeAccount(_:)), represented: acc.id))
        return sub
    }

    private func groupColor(_ group: String?) -> NSColor? {
        guard let group else { return nil }
        return MenuIcons.color(named: store.status?.groupColors?[group])
    }

    private func img(_ icon: NSImage?, tall: Bool = false) -> NSImage {
        MenuIcons.striped(icon, color: stripe, tall: tall)
    }

    /// Blank standard item with a tiny font: a small vertical gap (separators can't be doubled).
    private func spacer() -> NSMenuItem {
        let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        item.attributedTitle = NSAttributedString(string: " ", attributes: [.font: NSFont.systemFont(ofSize: 3)])
        item.isEnabled = false
        return item
    }

    /// Group header: a real (full-colour) item whose submenu holds group-wide actions.
    private func groupHeader(_ name: String?, accounts: [Account]) -> NSMenuItem {
        let title = name ?? "Other"
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.attributedTitle = NSAttributedString(string: title.uppercased(), attributes: [
            .font: NSFont.systemFont(ofSize: NSFont.menuFont(ofSize: 0).pointSize, weight: .heavy),
            .foregroundColor: NSColor.labelColor, .kern: 1.5])
        let lower = title.lowercased()
        let symbol = ["work", "pro", "client", "company", "job", "office", "boulot"].contains { lower.contains($0) } ? "briefcase.fill"
            : ["perso", "home", "private", "privé", "me"].contains { lower.contains($0) } ? "person.fill"
            : name == nil ? "tray.fill" : "folder.fill"
        item.image = img(NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: 13, weight: .semibold)))

        let sub = NSMenu()
        sub.autoenablesItems = false
        let idle = accounts.filter { !$0.isSetup && $0.canPing && $0.hasIdleSession }.map(\.id)
        let ping = action(idle.isEmpty ? "No Idle Sessions" : "Ping \(idle.count) Idle Session\(idle.count > 1 ? "s" : "")",
                          #selector(pingIDs(_:)), represented: idle, symbol: "bolt.fill")
        ping.isEnabled = !idle.isEmpty
        sub.addItem(ping)
        sub.addItem(action("Refresh Group", #selector(refreshIDs(_:)), represented: accounts.map(\.id),
                           symbol: "arrow.clockwise"))
        if let name {
            sub.addItem(.separator())
            let colorItem = NSMenuItem(title: "Color", action: nil, keyEquivalent: "")
            colorItem.image = NSImage(systemSymbolName: "paintpalette", accessibilityDescription: nil)
            let colors = NSMenu()
            for (cname, color) in MenuIcons.palette {
                let c = action(cname.capitalized, #selector(setGroupColor(_:)), represented: [name, cname])
                c.image = MenuIcons.dot(color)
                c.state = store.status?.groupColors?[name] == cname ? .on : .off
                colors.addItem(c)
            }
            colorItem.submenu = colors
            sub.addItem(colorItem)
            sub.addItem(action("Rename Group…", #selector(renameGroup(_:)), represented: name, symbol: "pencil"))
        }
        item.submenu = sub
        return item
    }

    private func groupMenuItem(_ acc: Account) -> NSMenuItem {
        let item = NSMenuItem(title: "Move to Group", action: nil, keyEquivalent: "")
        item.image = NSImage(systemSymbolName: "folder", accessibilityDescription: nil)
        let sub = NSMenu()
        sub.autoenablesItems = false
        for g in store.groups {
            let gi = action(g, #selector(moveToGroup(_:)), represented: [acc.id, g])
            gi.state = acc.group == g ? .on : .off
            sub.addItem(gi)
        }
        if !store.groups.isEmpty { sub.addItem(.separator()) }
        let none = action("No Group", #selector(moveToGroup(_:)), represented: [acc.id, ""])
        none.state = acc.group == nil ? .on : .off
        sub.addItem(none)
        sub.addItem(action("New Group…", #selector(newGroup(_:)), represented: acc.id))
        item.submenu = sub
        return item
    }

    /// Which account's 5h/weekly usage the menu bar shows.
    private func menuBarAccountItem() -> NSMenuItem {
        let item = NSMenuItem(title: "Menu Bar Account", action: nil, keyEquivalent: "")
        let sub = NSMenu()
        sub.autoenablesItems = false
        let accounts = (store.status?.accounts ?? []).filter { $0.metrics.contains { $0.percent != nil } }
        let current = accounts.contains { $0.id == panelAccount } ? panelAccount : ""
        let auto = action("Auto (Highest Usage)", #selector(choosePanelAccount(_:)), represented: "")
        auto.state = current.isEmpty ? .on : .off
        sub.addItem(auto)
        if !accounts.isEmpty { sub.addItem(.separator()) }
        for acc in accounts {
            let a = action(acc.label, #selector(choosePanelAccount(_:)), represented: acc.id)
            a.state = acc.id == current ? .on : .off
            sub.addItem(a)
        }
        item.submenu = sub
        return item
    }

    private func settingsItem() -> NSMenuItem {
        let item = NSMenuItem(title: "Settings", action: nil, keyEquivalent: "")
        item.image = NSImage(systemSymbolName: "gearshape", accessibilityDescription: nil)
        let sub = NSMenu()
        sub.autoenablesItems = false
        let pct = action("Show Percentage in Menu Bar", #selector(togglePercent))
        pct.state = showPercent ? .on : .off
        sub.addItem(pct)
        sub.addItem(menuBarAccountItem())
        let login = action("Launch at Login", #selector(toggleLaunchAtLogin))
        login.state = store.launchAtLogin ? .on : .off
        sub.addItem(login)
        sub.addItem(.separator())
        sub.addItem(action(configEditorTitle(), #selector(openConfig), key: ","))
        item.submenu = sub
        return item
    }

    private func configEditorTitle() -> String {
        if let path = store.status?.configPath,
           let app = NSWorkspace.shared.urlForApplication(toOpen: URL(fileURLWithPath: path)) {
            return "Open Config in ‘\(app.deletingPathExtension().lastPathComponent)’"
        }
        return "Open Config File"
    }

    // MARK: Item helpers

    private func action(_ title: String, _ selector: Selector, key: String = "", represented: Any? = nil,
                        symbol: String? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
        item.target = self
        item.representedObject = represented
        if let symbol { item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
        return item
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func account(_ sender: NSMenuItem) -> Account? {
        guard let id = sender.representedObject as? String else { return nil }
        return store.status?.accounts.first { $0.id == id }
    }

    // MARK: Actions

    @objc private func pingAccount(_ sender: NSMenuItem) {
        if let acc = account(sender) { store.ping([acc.id]) }
    }

    @objc private func pingIdle() { store.pingIdle() }

    @objc private func refresh() { store.refresh() }

    @objc private func refreshAccount(_ sender: NSMenuItem) {
        if let acc = account(sender) { Task { await store.refreshAccount(acc.id) } }
    }

    @objc private func openURL(_ sender: NSMenuItem) {
        if let url = sender.representedObject as? String { store.open(url) }
    }

    @objc private func copyText(_ sender: NSMenuItem) {
        if let text = sender.representedObject as? String { store.copy(text) }
    }

    @objc private func signIn(_ sender: NSMenuItem) {
        guard let acc = account(sender) else { return }
        store.addForm = nil
        store.startLogin(acc)
        AccountWindowController.shared.show()
    }

    @objc private func showAddAccount() {
        store.cancelLogin()
        store.addForm = AddForm()
        AccountWindowController.shared.show()
    }

    @objc private func editAccount(_ sender: NSMenuItem) {
        guard let acc = account(sender) else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Edit “\(acc.label)”"
        alert.informativeText = acc.type == "copilot"
            ? "Name, group, and GitHub host (github.com, or your GitHub Enterprise host such as mycorp.ghe.com — changing it signs this account out)."
            : "Name and group shown in the menu."
        let labelField = NSTextField(string: acc.label)
        labelField.placeholderString = "Name"
        let groupField = NSComboBox()
        groupField.addItems(withObjectValues: store.groups)
        groupField.stringValue = acc.group ?? ""
        groupField.placeholderString = "Group (e.g. Work, Personal) — optional"
        groupField.completes = true
        var fields: [NSView] = [labelField, groupField]
        var hostField: NSTextField?
        if acc.type == "copilot" {
            let f = NSTextField(string: acc.host ?? "github.com")
            f.placeholderString = "github.com"
            hostField = f
            fields.append(f)
        }
        let stack = NSStackView(views: fields)
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        for f in fields { f.widthAnchor.constraint(equalToConstant: 280).isActive = true }
        stack.frame = NSRect(x: 0, y: 0, width: 280, height: CGFloat(fields.count) * 30)
        stack.layoutSubtreeIfNeeded()
        alert.accessoryView = stack
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = labelField
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let label = labelField.stringValue.trimmingCharacters(in: .whitespaces)
        let host = hostField?.stringValue.trimmingCharacters(in: .whitespaces)
        let hostChanged = host != nil && host != (acc.host ?? "github.com")
        let group = groupField.stringValue.trimmingCharacters(in: .whitespaces)
        store.editAccount(acc.id, label: label.isEmpty ? acc.label : label,
                          group: group == (acc.group ?? "") ? nil : group, host: hostChanged ? host : nil)
        if hostChanged, acc.canLogin {
            // New host → its token is gone; go straight to sign-in on the new host.
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                if let fresh = store.status?.accounts.first(where: { $0.id == acc.id }) {
                    store.startLogin(fresh)
                    AccountWindowController.shared.show()
                }
            }
        }
    }

    @objc private func pingIDs(_ sender: NSMenuItem) {
        if let ids = sender.representedObject as? [String] { store.ping(ids) }
    }

    @objc private func refreshIDs(_ sender: NSMenuItem) {
        guard let ids = sender.representedObject as? [String] else { return }
        Task { for id in ids { _ = try? await Core.run(["status", "--json", "--compact", "--force", id]) }
               await store.load(["--cached"]) }
    }

    @objc private func setGroupColor(_ sender: NSMenuItem) {
        guard let pair = sender.representedObject as? [String], pair.count == 2 else { return }
        Task {
            _ = try? await Core.run(["group-color", pair[0], pair[1], "--json"])
            await store.load(["--cached"])
        }
    }

    @objc private func renameGroup(_ sender: NSMenuItem) {
        guard let old = sender.representedObject as? String else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Rename Group “\(old)”"
        alert.informativeText = "Leave empty to ungroup its accounts."
        let field = NSTextField(string: old)
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task {
            _ = try? await Core.run(["rename-group", old, field.stringValue.trimmingCharacters(in: .whitespaces), "--json"])
            await store.load(["--cached"])
        }
    }

    @objc private func moveToGroup(_ sender: NSMenuItem) {
        guard let pair = sender.representedObject as? [String], pair.count == 2 else { return }
        store.editAccount(pair[0], group: pair[1])
    }

    @objc private func newGroup(_ sender: NSMenuItem) {
        guard let acc = account(sender) else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "New Group"
        alert.informativeText = "“\(acc.label)” will be moved into it."
        let field = NSTextField(string: "")
        field.placeholderString = "e.g. Work, Personal"
        field.frame = NSRect(x: 0, y: 0, width: 260, height: 24)
        alert.accessoryView = field
        alert.addButton(withTitle: "Create")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let name = field.stringValue.trimmingCharacters(in: .whitespaces)
        if !name.isEmpty { store.editAccount(acc.id, group: name) }
    }

    @objc private func removeAccount(_ sender: NSMenuItem) {
        guard let acc = account(sender) else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Remove “\(acc.label)”?"
        alert.informativeText = acc.type == "copilot"
            ? "Its stored GitHub token is deleted. You can add it again later."
            : "It is removed from CreditsTracker only. Its sign-in and config dir stay on this Mac."
        alert.addButton(withTitle: "Remove").hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        if alert.runModal() == .alertFirstButtonReturn { store.removeAccount(acc.id) }
    }

    @objc private func togglePercent() {
        showPercent.toggle()
        update()
    }

    @objc private func choosePanelAccount(_ sender: NSMenuItem) {
        panelAccount = sender.representedObject as? String ?? ""
        update()
    }

    @objc private func toggleLaunchAtLogin() {
        store.launchAtLogin.toggle()
    }

    @objc private func openConfig() { store.openConfig() }

    @objc private func quit() { NSApp.terminate(nil) }
}
