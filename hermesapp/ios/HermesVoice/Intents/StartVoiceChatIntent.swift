import AppIntents

/// Starts a new voice chat, or sends the active recording on the next invocation.
struct StartVoiceChatIntent: AppIntent {
    static let title: LocalizedStringResource = "New Voice Chat"
    static let description = IntentDescription(
        "Opens a new Hermes voice chat and starts recording. Run it again while recording to send.",
        categoryName: "Chats"
    )

    /// iOS 18–25. iOS 26+ reads `supportedModes` instead.
    static let openAppWhenRun = true

    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { .foreground(.immediate) }

    @MainActor
    func perform() async throws -> some IntentResult {
        await AppModel.shared.newVoiceChat()
        return .result()
    }
}
