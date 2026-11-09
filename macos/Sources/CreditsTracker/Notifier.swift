import AppKit
import UserNotifications

/// Asks about a scheduled ping this Mac slept through: one notification with Ping Now / Wait,
/// removed once the question is answered (here or in the menu).
@MainActor
final class MissedPingNotifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = MissedPingNotifier()

    private static let category = "MISSED_PING"
    private static let pingAction = "PING_NOW"
    private static let waitAction = "WAIT"

    private var shown: Set<String> = []  // identifiers of the notifications on screen
    private var asked: Set<String> = []  // every question already notified (closing one doesn't bring it back)

    /// UNUserNotificationCenter needs a real app bundle (not a bare `swift run` binary).
    private var center: UNUserNotificationCenter? {
        Bundle.main.bundleIdentifier == nil ? nil : UNUserNotificationCenter.current()
    }

    func start() {
        guard let center else { return }
        center.delegate = self
        center.setNotificationCategories([UNNotificationCategory(
            identifier: Self.category,
            actions: [UNNotificationAction(identifier: Self.pingAction, title: "Ping Now", options: []),
                      UNNotificationAction(identifier: Self.waitAction, title: "Wait for Next", options: [])],
            intentIdentifiers: [], options: [])])
    }

    func sync(_ accounts: [Account]) {
        guard let center else { return }
        var pending: [String: Account] = [:]
        for acc in accounts {
            if let slot = acc.autoPing?.missed { pending["missed-\(acc.id)-\(slot)"] = acc }
        }
        let answered = shown.subtracting(pending.keys)
        if !answered.isEmpty {
            center.removeDeliveredNotifications(withIdentifiers: Array(answered))
            shown.subtract(answered)
        }
        for (ident, acc) in pending where !asked.contains(ident) {
            guard let ap = acc.autoPing else { continue }
            asked.insert(ident)
            shown.insert(ident)
            let content = UNMutableNotificationContent()
            content.title = acc.label
            content.body = Fmt.missedPing(ap, now: Date()).question
            content.categoryIdentifier = Self.category
            content.userInfo = ["account": acc.id]
            content.sound = .default
            let request = UNNotificationRequest(identifier: ident, content: content, trigger: nil)
            Task {
                // Asks for permission the first time; without it the menu (and ⏰ in the menu bar) still ask.
                guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
                try? await center.add(request)
            }
        }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let id = response.notification.request.content.userInfo["account"] as? String
        let action = response.actionIdentifier
        Task { @MainActor in
            guard let id else { return }
            if action == Self.pingAction {
                Store.shared.ping([id])
            } else if action == Self.waitAction {
                Store.shared.dismissMissed(id)
            }
        }
        completionHandler()
    }

    /// Show it even though the app counts as active (it is a menu-bar app).
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }
}
