import Foundation
import Testing
@testable import HermesVoice

@MainActor
@Suite("Mobile connection, speech and model preferences")
struct AppSettingsTests {
    @Test("Model preferences persist separately from connection and speech settings")
    func independentModelPreference() throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        let settings = AppSettings(service: service)
        #expect(settings.preferredChatModel == nil)
        let first = HermesModelChoice(provider: "first", modelID: "Exact-Model:variant", displayName: "Same name")
        let second = HermesModelChoice(provider: "second", modelID: first.modelID, displayName: first.displayName)
        try settings.savePreferredChatModel(first)
        try settings.save(serverURL: "https://example.com", token: "synthetic-model-token", voiceID: SpeechVoice.defaultReferenceID)
        #expect(AppSettings(service: service).preferredChatModel == first)
        try settings.savePreferredChatModel(second)
        let restored = AppSettings(service: service)
        #expect(restored.preferredChatModel == second)
        #expect(restored.preferredChatModel?.id != first.id)
        #expect(restored.serverURL == "https://example.com")
        #expect(restored.token == "synthetic-model-token")
        #expect(restored.speechVoice == .defaultVoice)
        try restored.savePreferredChatModel(nil)
        #expect(AppSettings(service: service).preferredChatModel == nil)
        #expect(restored.serverURL == "https://example.com")
        #expect(restored.token == "synthetic-model-token")
    }

    @Test("Failed model writes and deletes retain both persisted and in-memory preferences")
    func failedModelPreferenceWrite() throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        var failModelWrites = false
        let settings = AppSettings(service: service, writeKeychainItem: { item, value in
            if failModelWrites, item.account == "nativePreferredChatModel" { return false }
            return item.write(value)
        })
        let original = HermesModelChoice(provider: "first", modelID: "original-model", displayName: "Original")
        let replacement = HermesModelChoice(provider: "second", modelID: "new-model", displayName: "Replacement")
        try settings.savePreferredChatModel(original)
        failModelWrites = true
        #expect(throws: (any Error).self) { try settings.savePreferredChatModel(replacement) }
        #expect(settings.preferredChatModel == original)
        #expect(settings.storageError != nil)
        #expect(AppSettings(service: service).preferredChatModel == original)
        #expect(throws: (any Error).self) { try settings.savePreferredChatModel(nil) }
        #expect(settings.preferredChatModel == original)
        #expect(AppSettings(service: service).preferredChatModel == original)
        failModelWrites = false
        try settings.savePreferredChatModel(replacement)
        #expect(settings.storageError == nil)
        #expect(AppSettings(service: service).preferredChatModel == replacement)
    }

    @Test("A partially failed connection save rolls back without touching the model preference")
    func failedConnectionRetainsModelPreference() throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        var failTokenWrites = false
        let settings = AppSettings(service: service, writeKeychainItem: { item, value in
            if failTokenWrites, item.account == "nativeAPIToken" { return false }
            return item.write(value)
        })
        let choice = HermesModelChoice(provider: "first", modelID: "chosen-model", displayName: "Chosen")
        try settings.savePreferredChatModel(choice)
        try settings.save(serverURL: "https://original.example.com", token: "original-token", voiceID: SpeechVoice.defaultReferenceID)
        failTokenWrites = true
        #expect(throws: (any Error).self) {
            try settings.save(serverURL: "https://changed.example.com", token: "changed-token", voiceID: SpeechVoice.defaultReferenceID)
        }
        let restored = AppSettings(service: service)
        #expect(settings.serverURL == "https://original.example.com")
        #expect(settings.preferredChatModel == choice)
        #expect(restored.serverURL == "https://original.example.com")
        #expect(restored.token == "original-token")
        #expect(restored.preferredChatModel == choice)
    }

    @Test("Invalid model choices cannot replace the existing preference", arguments: [
        ("", "model"), ("provider\n", "model"), ("provider", " model "),
        ("provider", "hermes-agent"), ("provider", "default")
    ])
    func invalidModelPreference(provider: String, model: String) throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        let settings = AppSettings(service: service)
        let original = HermesModelChoice(provider: "first", modelID: "original-model", displayName: "Original")
        try settings.savePreferredChatModel(original)
        #expect(throws: (any Error).self) {
            try settings.savePreferredChatModel(HermesModelChoice(provider: provider, modelID: model, displayName: "Invalid"))
        }
        #expect(settings.preferredChatModel == original)
        #expect(AppSettings(service: service).preferredChatModel == original)
    }

    @Test("Corrupt and virtual persisted preferences restore as no choice rather than a default", arguments: [
        "not-json", #"{"provider":"first","modelID":"model"}"#,
        #"{"provider":"first","modelID":"hermes-agent","displayName":"Virtual"}"#,
        #"{"provider":"","modelID":"model","displayName":"Missing provider"}"#
    ])
    func corruptModelPreference(payload: String) {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        #expect(KeychainItem(service: service, account: "nativePreferredChatModel").write(payload))
        #expect(AppSettings(service: service).preferredChatModel == nil)
    }

    @Test("The supplied default voice is paid-model speech until the user selects another voice")
    func defaultAndCustomVoicePersistence() throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        let settings = AppSettings(service: service)
        #expect(settings.voiceID == "933563129e564b19a115bedd57b7406a")
        #expect(settings.speechVoice.modelID == "s2.1-pro")
        let custom = "9a9cf47702da476aa4629e2506d4a857"
        try settings.save(serverURL: "https://example.com", token: "synthetic-settings-token", voiceID: "  \(custom.uppercased())  ")
        let restored = AppSettings(service: service)
        #expect(restored.voiceID == custom)
        #expect(restored.speechVoice == SpeechVoice(referenceID: custom))
        #expect(restored.serverURL == "https://example.com")
        #expect(restored.token == "synthetic-settings-token")
        try restored.save(serverURL: restored.serverURL, token: restored.token, voiceID: SpeechVoice.defaultReferenceID)
        #expect(AppSettings(service: service).speechVoice == .defaultVoice)
    }

    @Test("Connection migration preserves the user's selected voice")
    func voiceSurvivesConnectionMigration() throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        let custom = "9a9cf47702da476aa4629e2506d4a857"
        let model = HermesModelChoice(provider: "custom:configured", modelID: "Exact-Model", displayName: "Chosen model")
        #expect(KeychainItem(service: service, account: "nativePreferredChatModel").write(
            String(decoding: try JSONEncoder().encode(model), as: UTF8.self)))
        #expect(KeychainItem(service: service, account: "nativeServerURL").write("https://notes.sashakuzina.com"))
        #expect(KeychainItem(service: service, account: "nativeAPIToken").write("synthetic-migration-token"))
        #expect(KeychainItem(service: service, account: "nativeVoiceID").write(custom))
        #expect(KeychainItem(service: service, account: "nativeSpeechURL").write("https://old.example.com"))
        let migrated = AppSettings(service: service)
        #expect(migrated.serverURL == "https://hermes.sashakuzina.com")
        #expect(migrated.voiceID == custom)
        #expect(migrated.token == "synthetic-migration-token")
        #expect(KeychainItem(service: service, account: "nativeSpeechURL").read() == nil)
        #expect(migrated.preferredChatModel == model)
        #expect(AppSettings(service: service).preferredChatModel == model)
    }

    @Test("A malformed voice cannot partially overwrite saved connection or speech preferences", arguments: ["", "not-a-voice", String(repeating: "g", count: 32), String(repeating: "a", count: 31)])
    func invalidVoiceKeepsPreviousSettings(voice: String) throws {
        let service = "com.artbred.hermesapp.tests.\(UUID().uuidString)"
        defer { clear(service) }
        let settings = AppSettings(service: service)
        try settings.save(serverURL: "https://example.com", token: "original-synthetic-token", voiceID: SpeechVoice.defaultReferenceID)
        #expect(throws: (any Error).self) {
            try settings.save(serverURL: "https://changed.example.com", token: "changed-synthetic-token", voiceID: voice)
        }
        let restored = AppSettings(service: service)
        #expect(restored.serverURL == "https://example.com")
        #expect(restored.token == "original-synthetic-token")
        #expect(restored.speechVoice == .defaultVoice)
    }

    private func clear(_ service: String) {
        for account in ["nativeServerURL", "nativeAPIToken", "nativeVoiceID", "nativePreferredChatModel", "nativeSpeechURL", "nativeSpeechToken"] {
            _ = KeychainItem(service: service, account: account).write(nil)
        }
    }
}
