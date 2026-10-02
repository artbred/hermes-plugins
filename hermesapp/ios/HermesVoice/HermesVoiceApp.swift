import SwiftUI
import UIKit
import UserNotifications

@main
struct HermesVoiceApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            if AppDelegate.isHostingUnitTests {
                // Unit tests must not start the production chat store or microphone.
                Color.clear
            } else {
                RootView(model: .shared)
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    static let isHostingUnitTests = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Restore local conversation state before URL and foreground intent delivery.
        if !Self.isHostingUnitTests {
            _ = AppModel.shared
            UNUserNotificationCenter.current().delegate = self
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        ReplyNotifications.shared.receivedToken(deviceToken, client: AppModel.shared.settings.makeClient())
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        ReplyNotifications.shared.registrationFailed(error)
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping @Sendable (UNNotificationPresentationOptions) -> Void
    ) {
        let reply = ReplyNotification(userInfo: notification.request.content.userInfo)
        // UIKit performs state restoration in these completions, so both the model
        // work and completion must stay on main rather than an async delegate thunk.
        DispatchQueue.main.async {
            guard UIApplication.shared.applicationState == .active else {
                completionHandler([.banner, .list, .sound])
                return
            }
            if let reply { AppModel.shared.receivedReplyNotification(reply, openChat: false) }
            completionHandler([])
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping @Sendable () -> Void
    ) {
        let reply = ReplyNotification(userInfo: response.notification.request.content.userInfo)
        let opensChat = response.actionIdentifier == UNNotificationDefaultActionIdentifier
        DispatchQueue.main.async {
            defer { completionHandler() }
            guard opensChat, let reply else { return }
            AppModel.shared.receivedReplyNotification(reply, openChat: true)
        }
    }

}
