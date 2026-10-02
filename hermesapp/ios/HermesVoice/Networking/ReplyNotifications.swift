import Foundation
import Observation
import UIKit
import UserNotifications

struct ReplyNotification: Equatable, Sendable {
    let chatID: String
    let runID: String

    init?(userInfo: [AnyHashable: Any]) {
        guard userInfo["kind"] as? String == "hermes_reply",
              let chatID = userInfo["chat_id"] as? String, UUID(uuidString: chatID) != nil,
              let runID = userInfo["run_id"] as? String, !runID.isEmpty else { return nil }
        self.chatID = chatID
        self.runID = runID
    }
}

struct PushDestination: Sendable {
    let deviceID: String
    let chatID: String
}

@MainActor
@Observable
final class ReplyNotifications {
    static let shared = ReplyNotifications()
    private(set) var status = "Notifications not enabled"
    private(set) var isAuthorized = false
    private var deviceToken: String?
    private var registeredConnection: String?
    private var registrationTask: Task<Void, Never>?
    private let defaults: UserDefaults
    let deviceID: String

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if let saved = defaults.string(forKey: "pushDeviceID"), UUID(uuidString: saved) != nil {
            deviceID = saved
        } else {
            deviceID = UUID().uuidString
            defaults.set(deviceID, forKey: "pushDeviceID")
        }
    }

    func refresh(client: APIClient?) async {
        guard Self.environment != nil else {
            isAuthorized = false
            status = "Push notifications require a push-enabled signed build"
            return
        }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        if settings.authorizationStatus == .notDetermined {
            await requestAuthorization(client: client)
            return
        }
        isAuthorized = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
        guard isAuthorized else {
            status = settings.authorizationStatus == .denied ? "Disabled in iPhone Settings" : "Notifications not enabled"
            return
        }
        if deviceToken == nil { UIApplication.shared.registerForRemoteNotifications() }
        guard let client, deviceToken != nil else { status = "Registering with Apple…"; return }
        do { try await register(client: client) }
        catch { status = "Notifications unavailable: \(error.localizedDescription)" }
    }

    private func requestAuthorization(client: APIClient?) async {
        do {
            _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
            await refresh(client: client)
        } catch { status = "Notifications unavailable: \(error.localizedDescription)" }
    }

    func receivedToken(_ token: Data, client: APIClient?) {
        deviceToken = token.map { String(format: "%02x", $0) }.joined()
        registeredConnection = nil
        registrationTask?.cancel()
        registrationTask = Task { await refresh(client: client) }
    }

    func registrationFailed(_ error: any Error) {
        deviceToken = nil
        registeredConnection = nil
        status = "Apple push registration failed: \(error.localizedDescription)"
    }

    func destination(chatID: String, client: APIClient) async throws -> PushDestination? {
        guard Self.environment != nil else { return nil }
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        isAuthorized = settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
        guard isAuthorized else { return nil }
        guard deviceToken != nil else {
            throw APIError.cannotPrepare("Apple push registration is not ready. Check Notifications in Settings and try again.")
        }
        do { try await register(client: client) }
        catch {
            status = "Notifications unavailable: \(error.localizedDescription)"
            throw error
        }
        return PushDestination(deviceID: deviceID, chatID: chatID)
    }

    private func register(client: APIClient) async throws {
        let connection = client.baseURL.absoluteString + "\n" + client.token
        guard registeredConnection != connection, let deviceToken, let environment = Self.environment else { return }
        try await client.registerPushDevice(id: deviceID, token: deviceToken, environment: environment)
        try Task.checkCancellation()
        registeredConnection = connection
        status = "Reply notifications enabled"
    }

    // Signing/export determines the environment, not the Swift Debug/Release configuration.
    private static let environment: String? = {
        #if targetEnvironment(simulator)
        return nil
        #else
        guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision") else {
            return "production"
        }
        guard let data = try? Data(contentsOf: url),
              let start = data.range(of: Data("<?xml".utf8)),
              let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
              let profile = try? PropertyListSerialization.propertyList(from: data[start.lowerBound..<end.upperBound], format: nil) as? [String: Any],
              let entitlements = profile["Entitlements"] as? [String: Any],
              let value = entitlements["aps-environment"] as? String,
              ["development", "production"].contains(value) else { return nil }
        return value
        #endif
    }()

    func acknowledge(runID: String, client: APIClient) async throws {
        try await client.acknowledgeReply(runID: runID, deviceID: deviceID)
        let center = UNUserNotificationCenter.current()
        let delivered = await center.deliveredNotifications()
        let identifiers = delivered.filter {
            ReplyNotification(userInfo: $0.request.content.userInfo)?.runID == runID
        }.map(\.request.identifier)
        center.removeDeliveredNotifications(withIdentifiers: identifiers)
    }
}
