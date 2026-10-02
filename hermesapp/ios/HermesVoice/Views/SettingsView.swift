import AppIntents
import SwiftUI

struct SettingsView: View {
    @Bindable var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var serverURL = ""
    @State private var token = ""
    @State private var voiceID = SpeechVoice.defaultReferenceID
    @State private var useDefaultVoice = true
    @State private var test: TestState = .idle
    @State private var testTask: Task<Void, Never>?
    @State private var saveError: String?
    @FocusState private var focusedField: Field?

    private enum Field { case serverURL, token, voiceID }
    private enum TestState {
        case idle, running
        case success(String), failure(String)
    }

    var body: some View {
        Form {
            Section {
                TextField("https://hermes.sashakuzina.com", text: $serverURL)
                    .keyboardType(.URL)
                    .textContentType(.URL)
                    .focused($focusedField, equals: .serverURL)
                    .accessibilityLabel("Hermes server URL")
                    .accessibilityIdentifier("serverURLField")
                SecureField("Hermes API token", text: $token)
                    .focused($focusedField, equals: .token)
                    .accessibilityLabel("Hermes API token")
                    .accessibilityIdentifier("tokenField")
                if let storageError = model.settings.storageError {
                    Label(storageError, systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                        .accessibilityIdentifier("settingsStorageError")
                }
            } header: {
                Label("Hermes connection", systemImage: "server.rack")
            } footer: {
                Text("One server URL and API token connect chats, voice, files, and memory. Provider keys stay on your Hermes server.")
            }
            Section {
                Button {
                    focusedField = nil
                    testTask?.cancel()
                    testTask = Task { await testConnection() }
                } label: {
                    HStack {
                        Text("Test connection")
                        Spacer()
                        if isTesting { ProgressView() }
                    }
                }
                .disabled(isTesting)
                .accessibilityIdentifier("testConnectionButton")
                testResult
            } footer: {
                Text("Checks the connected services without sending a chat or changing server data. Your URL and token are saved securely in your iPhone’s Keychain.")
            }
            Section {
                Picker("Voice", selection: $useDefaultVoice) {
                    Text("Sarah (default)").tag(true)
                    Text("Custom Fish voice").tag(false)
                }
                .accessibilityIdentifier("speechVoicePicker")
                if useDefaultVoice {
                    LabeledContent("Voice ID") {
                        Text(SpeechVoice.defaultReferenceID)
                            .font(.footnote.monospaced())
                            .textSelection(.enabled)
                    }
                    .accessibilityIdentifier("defaultSpeechVoice")
                } else {
                    TextField("32-character Fish voice ID", text: $voiceID)
                        .focused($focusedField, equals: .voiceID)
                        .accessibilityLabel("Fish voice ID")
                        .accessibilityIdentifier("speechVoiceIDField")
                }
                LabeledContent("Speech model", value: "Fish 2.1 Pro")
                    .accessibilityIdentifier("speechModelSetting")
            } header: {
                Label("Reply voice", systemImage: "speaker.wave.2")
            } footer: {
                Text("Your selected general voice is used for English replies and whenever language selection is uncertain or unavailable. Russian replies automatically use the Russian voice without changing this setting. Choose Custom to paste another Fish voice ID. Spoken replies and Listen always use the paid 2.1 Pro model. Changing the general voice regenerates reply audio when needed; original recordings are kept.")
            }
            Section {
                Text(model.notifications?.status ?? "Notifications unavailable")
                    .font(.footnote)
                    .accessibilityIdentifier("replyNotificationStatus")
            } header: {
                Label("Notifications", systemImage: "bell")
            } footer: {
                Text("Reply alerts are automatic when iOS notification permission is allowed. Manage or disable them in iPhone Settings. Alerts contain no reply text.")
            }
            Section {
                ShortcutsLink()
                    .shortcutsLinkStyle(.automaticOutline)
                    .frame(maxWidth: .infinity)
                    .listRowBackground(Color.clear)
            } header: {
                Text("Shortcuts & Action Button")
            } footer: {
                Text("Add a Hermes shortcut to your Action Button to start a voice chat quickly. Manage available actions in the Shortcuts app.")
            }
            Section {
                LabeledContent("Version", value: Self.version)
            }
        }
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .submitLabel(.done)
        .onSubmit { focusedField = nil }
        .scrollDismissesKeyboard(.interactively)
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Save", action: save)
                    .fontWeight(.semibold)
                    .accessibilityIdentifier("saveSettingsButton")
            }
        }
        .onAppear {
            serverURL = model.settings.serverURL
            token = model.settings.token
            voiceID = model.settings.voiceID
            useDefaultVoice = voiceID == SpeechVoice.defaultReferenceID
        }
        .onChange(of: [serverURL, token]) { _, _ in
            testTask?.cancel()
            test = .idle
        }
        .onDisappear { testTask?.cancel() }
        .alert("Settings not saved", isPresented: Binding(get: { saveError != nil }, set: { if !$0 { saveError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: { Text(saveError ?? "") }
        .accessibilityIdentifier("settingsForm")
    }

    @ViewBuilder
    private var testResult: some View {
        switch test {
        case .idle:
            EmptyView()
        case .running:
            Text("Checking Hermes services…")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("testConnectionResult")
        case .success(let message):
            Label {
                Text(message).font(.footnote)
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("testConnectionResult")
        case .failure(let message):
            Label {
                Text(message).font(.footnote).textSelection(.enabled)
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("testConnectionResult")
        }
    }

    private var isTesting: Bool {
        if case .running = test { true } else { false }
    }

    private var validationError: String? {
        guard APIClient.baseURL(from: serverURL) != nil else { return "Enter a valid HTTP or HTTPS Hermes server URL." }
        guard !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return "Enter your Hermes API token." }
        let selected = useDefaultVoice ? SpeechVoice.defaultReferenceID : voiceID.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard SpeechVoice.isValidReferenceID(selected) else { return "Enter a Fish voice ID containing exactly 32 hexadecimal characters." }
        return nil
    }

    private func save() {
        focusedField = nil
        if let validationError { saveError = validationError; return }
        do {
            try model.settings.save(serverURL: serverURL, token: token,
                                    voiceID: useDefaultVoice ? SpeechVoice.defaultReferenceID : voiceID)
            model.settingsChanged()
            dismiss()
        } catch {
            saveError = error.localizedDescription
        }
    }

    private func testConnection() async {
        if let validationError { test = .failure(validationError); return }
        guard let client = AppSettings.client(serverURL: serverURL, token: token) else { return }
        test = .running
        do {
            let result = try await client.health()
            guard !Task.isCancelled else { return }
            test = .success(result)
        } catch {
            guard !Task.isCancelled else { return }
            test = .failure(APIClient.describe(error))
        }
    }

    private static var version: String {
        let info = Bundle.main.infoDictionary
        let short = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(short) (\(build))"
    }
}
