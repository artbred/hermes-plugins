import Foundation
import Observation

@MainActor
@Observable
final class AppSettings {
    private(set) var serverURL: String
    private(set) var token: String
    private(set) var storageError: String?

    static let defaultURL = "https://hermes.sashakuzina.com"
    nonisolated private static let service = "com.artbred.hermesapp"

    private enum Key: String, CaseIterable {
        case nativeServerURL, nativeAPIToken
        // Read only during the one-time credential migration, then delete.
        case nativeSpeechURL, nativeSpeechToken

        var item: KeychainItem { KeychainItem(service: AppSettings.service, account: rawValue) }
    }

    init() {
        let savedServer = Key.nativeServerURL.item.read()
        serverURL = savedServer ?? Self.defaultURL
        token = Key.nativeAPIToken.item.read() ?? ""
        let destination = Self.migratedURL(serverURL)
        if savedServer != nil || Key.nativeSpeechURL.item.read() != nil || Key.nativeSpeechToken.item.read() != nil {
            do {
                try save(serverURL: destination, token: token)
            } catch {
                // Keep the original in-memory values if the atomic migration cannot finish.
                storageError = error.localizedDescription
            }
        }
    }

    var isConfigured: Bool { makeClient() != nil }

    func save(serverURL: String, token: String) throws {
        let url = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard APIClient.baseURL(from: url) != nil else { throw SettingsError.invalidURL }
        let values: [String?] = [url, token.isEmpty ? nil : token, nil, nil]
        let keys = Key.allCases
        let previous = keys.map { $0.item.read() }
        var written: [Int] = []
        for index in keys.indices where values[index] != previous[index] {
            guard keys[index].item.write(values[index]) else {
                var restored = true
                for changed in written.reversed() {
                    if !keys[changed].item.write(previous[changed]) { restored = false }
                }
                throw SettingsError.keychain(restored: restored)
            }
            written.append(index)
        }
        self.serverURL = url
        self.token = token
        storageError = nil
    }

    func makeClient() -> APIClient? {
        Self.client(serverURL: serverURL, token: token)
    }

    static func client(serverURL: String, token: String) -> APIClient? {
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = APIClient.baseURL(from: serverURL), !token.isEmpty else { return nil }
        return APIClient(baseURL: url, token: token)
    }

    static func migratedURL(_ input: String) -> String {
        guard let url = APIClient.baseURL(from: input),
              url.host()?.lowercased() == "notes.sashakuzina.com",
              url.port == nil || url.port == 443,
              var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return input }
        components.scheme = "https"
        components.host = "hermes.sashakuzina.com"
        components.port = nil
        return components.url?.absoluteString ?? input
    }

    private enum SettingsError: LocalizedError {
        case invalidURL, keychain(restored: Bool)

        var errorDescription: String? {
            switch self {
            case .invalidURL: "Enter a valid HTTPS Hermes server URL."
            case .keychain(true): "The Keychain could not save your connection. Your previous settings were kept. Unlock your iPhone and try again."
            case .keychain(false): "The Keychain could not save or fully restore your connection. Unlock your iPhone and save the server URL and token again."
            }
        }
    }
}
