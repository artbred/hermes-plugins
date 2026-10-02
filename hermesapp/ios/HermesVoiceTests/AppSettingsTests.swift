import Foundation
import Testing
@testable import HermesVoice

@MainActor
@Suite("Mobile speech preferences")
struct AppSettingsTests {
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
        #expect(KeychainItem(service: service, account: "nativeServerURL").write("https://notes.sashakuzina.com"))
        #expect(KeychainItem(service: service, account: "nativeAPIToken").write("synthetic-migration-token"))
        #expect(KeychainItem(service: service, account: "nativeVoiceID").write(custom))
        #expect(KeychainItem(service: service, account: "nativeSpeechURL").write("https://old.example.com"))
        let migrated = AppSettings(service: service)
        #expect(migrated.serverURL == "https://hermes.sashakuzina.com")
        #expect(migrated.voiceID == custom)
        #expect(migrated.token == "synthetic-migration-token")
        #expect(KeychainItem(service: service, account: "nativeSpeechURL").read() == nil)
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
        for account in ["nativeServerURL", "nativeAPIToken", "nativeVoiceID", "nativeSpeechURL", "nativeSpeechToken"] {
            _ = KeychainItem(service: service, account: account).write(nil)
        }
    }
}
