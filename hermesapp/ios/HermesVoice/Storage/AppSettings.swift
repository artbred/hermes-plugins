import Foundation
import Observation

@MainActor
@Observable
final class AppSettings {
    private(set) var serverURL: String
    private(set) var token: String
    private(set) var voiceID: String
    private(set) var storageError: String?

    static let defaultURL = "https://hermes.sashakuzina.com"
    nonisolated static let service = "com.artbred.hermesapp"
    @ObservationIgnored private let keychainService: String

    private enum Key: String, CaseIterable {
        case nativeServerURL, nativeAPIToken, nativeVoiceID
        // Read only during the one-time credential migration, then delete.
        case nativeSpeechURL, nativeSpeechToken

        func item(service: String) -> KeychainItem { KeychainItem(service: service, account: rawValue) }
    }

    init(service: String = AppSettings.service) {
        keychainService = service
        let savedServer = Key.nativeServerURL.item(service: service).read()
        serverURL = savedServer ?? Self.defaultURL
        token = Key.nativeAPIToken.item(service: service).read() ?? ""
        voiceID = Key.nativeVoiceID.item(service: service).read() ?? SpeechVoice.defaultReferenceID
        let destination = Self.migratedURL(serverURL)
        if savedServer != nil || Key.nativeSpeechURL.item(service: service).read() != nil || Key.nativeSpeechToken.item(service: service).read() != nil {
            do {
                try save(serverURL: destination, token: token, voiceID: voiceID)
            } catch {
                // Keep the original in-memory values if the atomic migration cannot finish.
                storageError = error.localizedDescription
            }
        }
    }

    var isConfigured: Bool { makeClient() != nil }
    var speechVoice: SpeechVoice { SpeechVoice(referenceID: voiceID) }

    func save(serverURL: String, token: String, voiceID: String) throws {
        let url = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        let voice = voiceID.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard APIClient.baseURL(from: url) != nil else { throw SettingsError.invalidURL }
        guard SpeechVoice.isValidReferenceID(voice) else { throw SettingsError.invalidVoice }
        let values: [String?] = [url, token.isEmpty ? nil : token, voice, nil, nil]
        let keys = Key.allCases
        let previous = keys.map { $0.item(service: keychainService).read() }
        var written: [Int] = []
        for index in keys.indices where values[index] != previous[index] {
            guard keys[index].item(service: keychainService).write(values[index]) else {
                var restored = true
                for changed in written.reversed() {
                    if !keys[changed].item(service: keychainService).write(previous[changed]) { restored = false }
                }
                throw SettingsError.keychain(restored: restored)
            }
            written.append(index)
        }
        self.serverURL = url
        self.token = token
        self.voiceID = voice
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
        case invalidURL, invalidVoice, keychain(restored: Bool)

        var errorDescription: String? {
            switch self {
            case .invalidURL: "Enter a valid HTTPS Hermes server URL."
            case .invalidVoice: "Enter a Fish voice ID containing exactly 32 hexadecimal characters."
            case .keychain(true): "The Keychain could not save your connection. Your previous settings were kept. Unlock your iPhone and try again."
            case .keychain(false): "The Keychain could not save or fully restore your connection. Unlock your iPhone and save the server URL and token again."
            }
        }
    }
}
