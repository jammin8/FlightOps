import AppKit
import Foundation
import UserNotifications





@MainActor final class Alerts: NSObject, UNUserNotificationCenterDelegate {
    private let center: UNUserNotificationCenter?
    private var authorized = false
    private var lastAuthCheck = Date.distantPast
    private var engine: AlertEngine
    private let store = UserDefaults.standard
    private static let stateKey = "alertState.v3"

    private var mutedUntil: Date {
        get { Date(timeIntervalSince1970: store.double(forKey: "alertsMutedUntil")) }
        set { store.set(newValue.timeIntervalSince1970, forKey: "alertsMutedUntil") }
    }

    override init() {

        center = Bundle.main.bundleIdentifier == nil ? nil : UNUserNotificationCenter.current()
        engine = AlertEngine.restored(UserDefaults.standard.data(forKey: Alerts.stateKey), now: Date())
        super.init()

        for old in ["alertEpisode", "alertAgents", "alertState.v2", "snoozedNeeds"] { store.removeObject(forKey: old) }
        guard let center else { return }
        center.delegate = self
        let open = UNNotificationAction(identifier: "open", title: "Open", options: [.foreground])
        let snooze = UNNotificationAction(identifier: "snooze", title: "Snooze 1 Hour", options: [])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: "incident", actions: [open, snooze], intentIdentifiers: []),
            UNNotificationCategory(identifier: "agent", actions: [open, snooze], intentIdentifiers: []),
        ])
        center.requestAuthorization(options: [.alert, .sound, .timeSensitive]) { ok, _ in
            Task { @MainActor in self.authorized = ok }
        }
        lastAuthCheck = Date()
    }


    func rebaseline() {
        engine.rebaseline(Date())
        save()
    }

    func evaluate(_ w: World, checks: [Check]) {
        refreshAuthorization()

        let observations = Rules.alertObservations(checks)
        var waiting: [WaitingAgent]? = nil
        if w.agentsKnown {
            waiting = (w.agents ?? []).filter { $0.waiting }.map {
                WaitingAgent(id: $0.id, title: $0.name, url: threadURL($0.threadId))
            }
        }
        let canDeliver = center != nil && authorized && Date() >= mutedUntil
        let input = AlertInput(now: w.now, observations: observations, waiting: waiting, canDeliver: canDeliver)
        let notices = engine.step(input)
        save()
        for n in notices { post(n) }
    }

    private func save() { store.set(engine.encoded(), forKey: Alerts.stateKey) }


    private func refreshAuthorization() {
        guard let center, Date().timeIntervalSince(lastAuthCheck) > 300 else { return }
        lastAuthCheck = Date()
        center.getNotificationSettings { settings in
            let ok = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
            Task { @MainActor in self.authorized = ok }
        }
    }

    private func post(_ n: Notice) {
        guard let center else { return }
        let content = UNMutableNotificationContent()
        content.title = n.title
        content.body = n.body
        content.categoryIdentifier = n.kind == .agent ? "agent" : "incident"
        content.threadIdentifier = n.kind == .agent ? "agents" : "incident"
        content.interruptionLevel = n.timeSensitive ? .timeSensitive : .active
        var info: [String: Any] = [:]
        if let url = n.url { info["url"] = url.absoluteString }
        if let tab = n.tab { info["tab"] = tab.rawValue }
        content.userInfo = info

        let identifier: String
        switch n.kind {
        case .incident, .reminder: identifier = "incident"
        case .agent: identifier = "agent-" + n.ids.joined(separator: ",")
        case .allClear, .missed: identifier = UUID().uuidString
        }
        let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
        center.add(request) { error in
            guard error != nil else { return }
            Task { @MainActor in
                self.engine.deliveryFailed(n)
                self.save()
            }
        }
    }



    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .list]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let action = response.actionIdentifier
        let info = response.notification.request.content.userInfo
        let url = (info["url"] as? String).flatMap { URL(string: $0) }
        let tab = (info["tab"] as? Int).flatMap { Tab(rawValue: $0) }
        await MainActor.run {
            switch action {
            case "snooze":
                self.mutedUntil = Date().addingTimeInterval(3600)
            case "open", UNNotificationDefaultActionIdentifier:
                if let url { NSWorkspace.shared.open(url) } else { PanelOpener.open(tab: tab) }
            default:
                break
            }
        }
    }
}



@MainActor enum PanelOpener {
    static func open(tab: Tab?) {
        if let tab { UserDefaults.standard.set(tab.rawValue, forKey: "tab") }
        NSApp.activate()
        for window in NSApp.windows where window.className.contains("StatusBarWindow") {
            if let button = statusButton(in: window.contentView) {
                button.performClick(nil)
                return
            }
        }
    }

    private static func statusButton(in view: NSView?) -> NSStatusBarButton? {
        guard let view else { return nil }
        if let button = view as? NSStatusBarButton { return button }
        for sub in view.subviews {
            if let button = statusButton(in: sub) { return button }
        }
        return nil
    }
}
