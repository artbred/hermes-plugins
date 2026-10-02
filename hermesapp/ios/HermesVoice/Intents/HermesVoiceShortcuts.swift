import AppIntents

/// Makes the intents available in Shortcuts, Spotlight, Siri and the Action Button picker without setup.
struct HermesVoiceShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartVoiceChatIntent(),
            phrases: [
                "Talk to \(.applicationName)",
                "Start a voice chat in \(.applicationName)",
                "New voice chat in \(.applicationName)",
            ],
            shortTitle: "New Voice Chat",
            systemImageName: "mic.fill"
        )
        AppShortcut(
            intent: SendMessageIntent(),
            phrases: [
                "Send a message to \(.applicationName)",
                "Ask \(.applicationName)",
            ],
            shortTitle: "Send Message",
            systemImageName: "square.and.pencil"
        )
    }

    static let shortcutTileColor: ShortcutTileColor = .purple
}
