import AppIntents

/// Opens a new Hermes conversation and submits a text message through a full agent run.
struct SendMessageIntent: AppIntent {
    static let title: LocalizedStringResource = "Send Message"
    static let description = IntentDescription(
        "Opens Hermes Voice and starts a new chat with your message.",
        categoryName: "Chats"
    )
    static let openAppWhenRun = true

    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { .foreground(.immediate) }

    @Parameter(
        title: "Text",
        description: "What to ask Hermes.",
        inputOptions: String.IntentInputOptions(multiline: true),
        requestValueDialog: "What would you like to tell Hermes?"
    )
    var text: String

    static var parameterSummary: some ParameterSummary {
        Summary("Send \(\.$text) to Hermes")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        AppModel.shared.newChat()
        AppModel.shared.sendText(text)
        return .result(dialog: "Your message is in Hermes Voice.")
    }
}
