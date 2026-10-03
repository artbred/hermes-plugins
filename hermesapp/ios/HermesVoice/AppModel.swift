import Foundation
import Network
import Observation
import SwiftUI
import UIKit

struct AppAlert: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    var offersSettings = false
}

struct RunApproval: Identifiable {
    let id: String
    let runID: String
    let chatID: String
    let command: String
    let choices: [String]
    var requestID: String?
}

@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    let settings: AppSettings
    let store: ChatStore
    let recorder: Recorder
    private let player = ReplyPlayer()
    let notifications: ReplyNotifications?
    @ObservationIgnored private var replyAcknowledgements: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var copyTask: Task<Void, Never>?
    @ObservationIgnored private let clientOverride: APIClient?
    @ObservationIgnored private let modelInventoryCache: ModelInventoryCache

    var selectedChatID: String?
    var draft = ""
    var isSettingsPresented = false
    var isChatsPresented = false
    var isComposerFocused = false
    var alert: AppAlert?
    private(set) var isRefreshing = false
    var isLoadingModels = false
    var modelSelectionError: String?
    var composerFocusRequest = UUID()
    private var availableModels: [HermesModelChoice] = []
    private var suggestedModels: [HermesModelChoice] = []
    @ObservationIgnored private var availableModelsByID: [String: HermesModelChoice] = [:]
    private(set) var configuredDefaultModel: HermesModelChoice?
    @ObservationIgnored private var manuallySelectedChatIDs: Set<String> = []
    @ObservationIgnored private var modelInventoryRevision = UUID()
    @ObservationIgnored private var observedModelServerURL: String
    @ObservationIgnored private var observedModelToken: String
    private var importingChatIDs: Set<String> = []
    private var importErrors: [String: String] = [:]
    private(set) var synthesizingMessageIDs: Set<String> = []
    var connectionMessage: String?
    private var approvals: [String: RunApproval] = [:]
    private var liveResponses: [String: String] = [:]
    @ObservationIgnored private var streamBuffers: [String: String] = [:]
    @ObservationIgnored private var streamFlushTasks: [String: Task<Void, Never>] = [:]

    private func appendStreaming(_ text: String, chatID: String) {
        streamBuffers[chatID, default: ""] += text
        guard streamFlushTasks[chatID] == nil else { return }
        streamFlushTasks[chatID] = Task { [weak self] in
            do { try await Task.sleep(for: .milliseconds(50)) }
            catch { return }
            guard let self, !Task.isCancelled else { return }
            if let text = self.streamBuffers.removeValue(forKey: chatID) {
                self.liveResponses[chatID, default: ""] += text
            }
            self.streamFlushTasks[chatID] = nil
        }
    }

    private func clearStreaming(_ chatID: String) {
        streamFlushTasks.removeValue(forKey: chatID)?.cancel()
        streamBuffers[chatID] = nil
        liveResponses[chatID] = nil
    }

    @ObservationIgnored private var workers: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var workerIDs: [String: UUID] = [:]
    @ObservationIgnored private var stopTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var stopTaskIDs: [String: UUID] = [:]
    @ObservationIgnored private var speechTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var observedSpeechVoice: SpeechVoice
    @ObservationIgnored private var speechSelectionRevision = UUID()
    @ObservationIgnored private var titleTasks: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var drafts: [String: String] = [:]
    @ObservationIgnored private var recordingChatID: String?
    @ObservationIgnored private var isStartingRecording = false
    @ObservationIgnored private var startWhenActive = false
    @ObservationIgnored private var foreground = true
    @ObservationIgnored private var backgroundTask: UIBackgroundTaskIdentifier = .invalid
    @ObservationIgnored private let monitor = NWPathMonitor()
    @ObservationIgnored private var networkAvailable: Bool?

    init(settings: AppSettings = AppSettings(), store: ChatStore = ChatStore(), recorder: Recorder = Recorder(), client: APIClient? = nil, notifications: ReplyNotifications? = nil, initialModelChoices: [HermesModelChoice]? = nil, initialModelChoice: HermesModelChoice? = nil) {
        clientOverride = client
        self.notifications = notifications ?? (client == nil ? .shared : nil)
        self.settings = settings
        observedSpeechVoice = settings.speechVoice
        let cache = ModelInventoryCache(directory: store.directory)
        modelInventoryCache = cache
        let inventory: HermesModelInventory
        if initialModelChoices != nil || initialModelChoice != nil {
            let choices = initialModelChoices ?? []
            inventory = HermesModelInventory(
                defaultModel: initialModelChoice.map { choice in choices.first { $0.id == choice.id } ?? choice },
                suggestedModels: choices, availableModels: choices)
        } else {
            inventory = cache.load(serverURL: settings.serverURL, token: settings.token)
                ?? HermesModelInventory(defaultModel: nil, suggestedModels: [], availableModels: [])
        }
        availableModels = inventory.availableModels
        availableModelsByID = Dictionary(inventory.availableModels.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        observedModelServerURL = settings.serverURL
        observedModelToken = settings.token
        configuredDefaultModel = inventory.defaultModel
        suggestedModels = inventory.suggestedModels
        self.store = store
        self.recorder = recorder
        selectedChatID = store.chats.first?.id
        recorder.onUnexpectedStop = { [weak self] in self?.enqueue($0) }
        player.onError = { [weak self] message in
            self?.alert = AppAlert(title: "Audio playback failed", message: message)
        }
        monitor.pathUpdateHandler = { [weak self] path in
            let available = path.status == .satisfied
            Task { @MainActor in
                guard let self else { return }
                defer { self.networkAvailable = available }
                if available, self.networkAvailable == false, self.foreground { self.resumePending() }
            }
        }
        monitor.start(queue: DispatchQueue(label: "com.artbred.hermesapp.network"))
        adoptConfiguredDefaultForEmptyChat()
    }

    var selectedChat: Chat? { selectedChatID.flatMap(store.chat(id:)) }
    var selectedChatModel: HermesModelChoice? {
        guard let selected = selectedChat?.modelChoice ?? configuredDefaultModel else { return nil }
        return availableModelsByID[selected.id] ?? selected
    }
    var selectedThinkingLevel: ThinkingLevel { selectedChat?.thinkingLevel ?? .automatic }
    var modelChoices: [HermesModelChoice] {
        guard !availableModels.isEmpty else { return [] }
        var choices: [HermesModelChoice] = []
        choices.reserveCapacity(min(5, availableModels.count))
        var seen = Set<String>()
        seen.reserveCapacity(5)
        func append(_ candidate: HermesModelChoice?) {
            guard choices.count < 5, let candidate,
                  let available = availableModelsByID[candidate.id], seen.insert(candidate.id).inserted else { return }
            choices.append(available)
        }
        append(configuredDefaultModel)
        for suggested in suggestedModels {
            if choices.count == 5 { break }
            append(suggested)
        }
        return choices
    }
    var canChangeModel: Bool { !isBusy && !isRecordingInProgress && !isImportingAttachments }
    var canStartNewChat: Bool { !isRecordingInProgress }
    var canFocusComposer: Bool { !isRecordingInProgress && !startWhenActive && !isChatsPresented && !isSettingsPresented }

    func refreshModelChoices() async {
        guard !isLoadingModels, let client = makeClient() else { return }
        let revision = modelInventoryRevision
        let serverURL = settings.serverURL
        let token = settings.token
        isLoadingModels = true
        defer { if revision == modelInventoryRevision { isLoadingModels = false } }
        do {
            let inventory = try await client.modelInventory()
            guard !Task.isCancelled, revision == modelInventoryRevision,
                  settings.serverURL == serverURL, settings.token == token else { return }
            // An injected transport can run tests without a configured connection.
            // Only real, configured settings provide a durable cache scope.
            if settings.isConfigured {
                do {
                    try modelInventoryCache.save(inventory, serverURL: serverURL, token: token)
                } catch {
                    show(error, title: "Could not save Hermes models")
                    return
                }
            }
            availableModels = inventory.availableModels
            availableModelsByID = Dictionary(inventory.availableModels.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
            configuredDefaultModel = inventory.defaultModel
            suggestedModels = inventory.suggestedModels
            adoptConfiguredDefaultForEmptyChat()
            if availableModels.isEmpty {
                modelSelectionError = "Hermes has no available chat models. Configure a provider before sending."
            } else if let selected = selectedChatModel, availableModelsByID[selected.id] == nil {
                modelSelectionError = "This model is no longer available. Choose another model."
            } else {
                modelSelectionError = nil
            }
        } catch {
            guard !Task.isCancelled, revision == modelInventoryRevision,
                  settings.serverURL == serverURL, settings.token == token else { return }
            modelSelectionError = "Could not load Hermes models: \(error.localizedDescription)"
        }
    }

    private func adoptConfiguredDefaultForEmptyChat() {
        guard canChangeModel, let configuredDefaultModel, var chat = selectedChat, chat.messages.isEmpty,
              !manuallySelectedChatIDs.contains(chat.id), chat.modelChoice != configuredDefaultModel else { return }
        chat.modelChoice = configuredDefaultModel
        do { try store.save(chat) }
        catch { show(error, title: "Could not save default model") }
    }

    func selectChatModel(_ choice: HermesModelChoice) {
        guard canChangeModel, let choice = availableModelsByID[choice.id] else { return }
        if selectedChat == nil { newChat(focusComposer: false) }
        guard var chat = selectedChat else { return }
        chat.modelChoice = choice
        do {
            try store.save(chat)
            manuallySelectedChatIDs.insert(chat.id)
            modelSelectionError = nil
        } catch { show(error, title: "Could not save model selection") }
    }

    func selectThinkingLevel(_ level: ThinkingLevel) {
        guard canChangeModel else { return }
        if selectedChat == nil { newChat(focusComposer: false) }
        guard var chat = selectedChat else { return }
        chat.thinkingLevel = level
        do { try store.save(chat) }
        catch { show(error, title: "Could not save thinking level") }
    }
    var isBusy: Bool { selectedChat?.hasPendingMessages == true }
    /// A take is recording, or its microphone session is still activating.
    private var isRecordingInProgress: Bool { recorder.isRecording || isStartingRecording }
    var activeMessage: ChatMessage? {
        selectedChat?.messages.first { $0.role == .user && $0.stage.isPending }
    }
    var isStopping: Bool { activeMessage.map { $0.stopRequested == true && $0.error == nil } ?? false }
    var playingMessageID: String? { player.messageID }
    var isAudioPlaying: Bool { player.isPlaying }
    var liveResponse: String { selectedChatID.flatMap { liveResponses[$0] } ?? "" }
    var approval: RunApproval? { selectedChatID.flatMap { approvals[$0] } }

    func pendingApproval(in chatID: String) -> RunApproval? { approvals[chatID] }

    var pendingAttachments: [ChatAttachment] { selectedChat?.draftAttachments ?? [] }
    var isImportingAttachments: Bool { selectedChatID.map { importingChatIDs.contains($0) } ?? false }
    var attachmentImportError: String? { selectedChatID.flatMap { importErrors[$0] } }
    var canSend: Bool {
        !isBusy && !isRecordingInProgress && !isImportingAttachments
            && selectedChatModel.map { availableModelsByID[$0.id] != nil } == true
            && (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !pendingAttachments.isEmpty)
    }

    func attachmentURL(_ file: ChatAttachment) -> URL {
        store.attachmentURL(fileName: file.localFileName)
    }

    func importAttachments(_ urls: [URL]) async {
        guard !urls.isEmpty, !isImportingAttachments, !recorder.isRecording, !isBusy else { return }
        if selectedChat == nil { newChat() }
        guard let chatID = selectedChatID else { return }
        importingChatIDs.insert(chatID)
        importErrors[chatID] = nil
        defer { importingChatIDs.remove(chatID) }
        var imported: [ChatAttachment] = []
        do {
            let directory = store.attachmentDirectory
            for url in urls {
                let file = try await Task.detached(priority: .userInitiated) {
                    try AttachmentImport.copy(url, to: directory, maximumBytes: APIClient.maxAttachmentBytes)
                }.value
                imported.append(file)
            }
            try Task.checkCancellation()
            try updateChat(chatID) { chat in
                chat.draftAttachments = (chat.draftAttachments ?? []) + imported
            }
        } catch {
            for file in imported { try? FileManager.default.removeItem(at: attachmentURL(file)) }
            if !(error is CancellationError) { importErrors[chatID] = error.localizedDescription }
        }
    }

    func removeAttachment(_ id: String) {
        guard var chat = selectedChat, let file = chat.draftAttachments?.first(where: { $0.id == id }) else { return }
        chat.draftAttachments?.removeAll { $0.id == id }
        do {
            try store.save(chat)
            try? FileManager.default.removeItem(at: attachmentURL(file))
        } catch { show(error, title: "Could not remove file") }
    }

    func newChat(focusComposer: Bool = true) {
        if isRecordingInProgress { discardRecording() }
        if let selectedChatID { drafts[selectedChatID] = draft }
        var chat = Chat()
        chat.modelChoice = configuredDefaultModel
        do {
            try store.save(chat)
            selectedChatID = chat.id
            draft = ""
            isChatsPresented = false
            stopPlayback()
            if focusComposer { composerFocusRequest = UUID() }
        } catch { show(error, title: "Could not create chat") }
    }

    func selectChat(_ id: String) {
        guard store.chat(id: id) != nil else { return }
        if isRecordingInProgress { discardRecording() }
        if let selectedChatID { drafts[selectedChatID] = draft }
        selectedChatID = id
        adoptConfiguredDefaultForEmptyChat()
        draft = drafts[id] ?? ""
        isChatsPresented = false
        stopPlayback()
        if selectedChat?.messages.isEmpty == true { composerFocusRequest = UUID() }
        resumePending()
    }

    func deleteChat(_ id: String) {
        guard let chat = store.chat(id: id) else { return }
        guard !importingChatIDs.contains(id) else {
            alert = AppAlert(title: "Files are being added", message: "Wait for this chat’s file import to finish before removing it. You can keep using other chats.")
            return
        }
        guard !chat.hasPendingMessages, recordingChatID != id else {
            alert = AppAlert(title: "Chat is active", message: "Stop the recording or agent run before removing this chat.")
            return
        }
        do {
            for message in chat.messages { speechTasks[message.id]?.cancel() }
            try store.remove(id: id)
            drafts[id] = nil
            importErrors[id] = nil
            if selectedChatID == id {
                stopPlayback()
                selectedChatID = store.chats.first?.id
                draft = selectedChatID.flatMap { drafts[$0] } ?? ""
            }
        } catch { show(error, title: "Could not remove chat") }
    }

    func sendText(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !isRecordingInProgress, !isImportingAttachments,
              !text.isEmpty || !pendingAttachments.isEmpty else { return }
        guard let choice = selectedChatModel, availableModelsByID[choice.id] != nil else {
            show(ChatError.modelNotSelected, title: "Choose a chat model")
            return
        }
        if selectedChat == nil { newChat(focusComposer: false) }
        guard var chat = selectedChat, !chat.hasPendingMessages else { return }
        var message = ChatMessage(role: .user, text: text, attachments: chat.draftAttachments)
        message.modelChoice = choice
        message.thinkingLevel = chat.thinkingLevel ?? .automatic
        chat.messages.append(message)
        chat.draftAttachments = nil
        chat.updatedAt = .now
        do {
            try store.save(chat)
            draft = ""
            drafts[chat.id] = nil
            kick(chat.id)
        } catch { show(error, title: "Message was not saved") }
    }

    func newVoiceChat() async {
        if recorder.isRecording {
            stopAndSend()
            return
        }
        guard !isStartingRecording, !startWhenActive else { return }
        isSettingsPresented = false
        isChatsPresented = false
        newChat(focusComposer: false)
        guard selectedChat?.messages.isEmpty == true else { return }
        if UIApplication.shared.applicationState != .active {
            startWhenActive = true
        } else {
            await startRecording()
        }
    }

    func startRecording() async {
        guard !isRecordingInProgress, !isBusy, !isImportingAttachments else { return }
        if selectedChat == nil { newChat(focusComposer: false) }
        guard let chatID = selectedChatID else { return }
        isStartingRecording = true
        defer { isStartingRecording = false }
        guard await Recorder.requestPermission() else {
            alert = AppAlert(title: "Microphone access needed", message: RecorderError.permissionDenied.localizedDescription, offersSettings: true)
            return
        }
        guard selectedChatID == chatID else { return }
        guard UIApplication.shared.applicationState == .active else {
            startWhenActive = true
            return
        }
        isSettingsPresented = false
        isChatsPresented = false
        alert = nil
        stopPlayback()
        let id = UUID().uuidString.lowercased()
        // Claimed before the microphone session activates, so the chat cannot be removed meanwhile;
        // switching chats discards the starting take instead.
        recordingChatID = chatID
        do {
            let started = try await recorder.start(id: id, url: store.audioURL(fileName: "\(id).m4a"))
            if !started { recordingChatID = nil }
        } catch {
            recordingChatID = nil
            show(error, title: "Cannot record")
        }
    }

    func stopAndSend() {
        guard let recording = recorder.stop() else { return }
        enqueue(recording)
    }

    func discardRecording() {
        recorder.discard()
        recordingChatID = nil
        startWhenActive = false
    }

    private func enqueue(_ recording: Recording) {
        guard let chatID = recordingChatID, var chat = store.chat(id: chatID) else { return }
        var message = ChatMessage(id: recording.id, role: .user, input: .voice, text: "", createdAt: recording.startedAt, audioFileName: recording.url.lastPathComponent, attachments: chat.draftAttachments, recordingDuration: recording.duration)
        message.modelChoice = chat.modelChoice ?? configuredDefaultModel
        message.thinkingLevel = chat.thinkingLevel ?? .automatic
        chat.messages.append(message)
        chat.draftAttachments = nil
        chat.updatedAt = .now
        do {
            try store.save(chat)
            recordingChatID = nil
            kick(chatID)
        } catch {
            // Preserve the recording on disk if storage fills up; never claim it was sent.
            show(error, title: "Recording could not be saved")
        }
    }

    func retry(_ message: ChatMessage) {
        guard let chat = store.chats.first(where: { $0.messages.contains(where: { $0.id == message.id }) }),
              !chat.hasPendingMessages, message.role == .user else { return }
        do {
            try updateMessage(chat.id, message.id) {
                if $0.runWasTerminal {
                    $0.attempt += 1
                    $0.runID = nil
                    $0.runWasTerminal = false
                    $0.submission = nil
                    $0.modelChoice = chat.modelChoice ?? configuredDefaultModel
                    $0.thinkingLevel = chat.thinkingLevel ?? .automatic
                }
                if $0.submission == nil, $0.runID == nil, $0.modelChoice == nil {
                    $0.modelChoice = chat.modelChoice ?? configuredDefaultModel
                }
                if $0.submission == nil, $0.runID == nil, $0.thinkingLevel == nil {
                    $0.thinkingLevel = chat.thinkingLevel ?? .automatic
                }
                $0.error = nil
                $0.stopRequested = nil
                $0.stopAcknowledged = nil
                $0.stage = $0.runID == nil ? .queued : .running
            }
            kick(chat.id)
        } catch { show(error, title: "Could not retry") }
    }

    func stopRun() {
        guard let chat = selectedChat, let message = activeMessage else { return }
        do {
            try updateMessage(chat.id, message.id) {
                $0.stopRequested = true
                $0.error = nil
            }
            approvals[chat.id] = nil
            if message.runID == nil, message.submission == nil, message.stage != .savingMemory, message.stage != .submitting {
                try interruptBeforeSubmission(message, chatID: chat.id)
                cancelWorker(chat.id)
                return
            }
            // Admission and memory retention may already have committed remotely. Keep the
            // same operation alive until its receipt is known instead of claiming it stopped.
            guard let client = makeClient() else {
                try updateMessage(chat.id, message.id) {
                    $0.error = "Connect to your Hermes server in Settings, then tap Stop to try again."
                }
                return
            }
            kick(chat.id)
            if let latest = store.chat(id: chat.id)?.messages.first(where: { $0.id == message.id }) {
                requestStop(latest, chatID: chat.id, client: client)
            }
        } catch { show(error, title: "Could not request stop") }
    }

    private func interruptBeforeSubmission(_ message: ChatMessage, chatID: String) throws {
        try updateMessage(chatID, message.id) {
            $0.stage = .interrupted
            $0.error = "Stopped before sending a request to Hermes."
        }
    }

    private func requestStop(_ message: ChatMessage, chatID: String, client: APIClient) {
        guard message.stopRequested == true, message.stopAcknowledged != true,
              let runID = message.runID, message.stage.isPending, stopTasks[chatID] == nil else { return }
        if message.error != nil {
            do { try updateMessage(chatID, message.id) { $0.error = nil } }
            catch { show(error, title: "Could not retry stop"); return }
        }
        let taskID = UUID()
        stopTaskIDs[chatID] = taskID
        stopTasks[chatID] = Task { [weak self] in
            guard let self else { return }
            defer {
                if stopTaskIDs[chatID] == taskID {
                    stopTasks[chatID] = nil
                    stopTaskIDs[chatID] = nil
                }
            }
            do {
                try await client.stopRun(id: runID)
                guard stopTaskIDs[chatID] == taskID, !Task.isCancelled,
                      let latest = store.chat(id: chatID)?.messages.first(where: { $0.id == message.id }),
                      latest.runID == runID, latest.stage.isPending, !latest.runWasTerminal else { return }
                try updateMessage(chatID, message.id) { $0.stopAcknowledged = true }
            } catch {
                guard stopTaskIDs[chatID] == taskID, !Task.isCancelled,
                      let latest = store.chat(id: chatID)?.messages.first(where: { $0.id == message.id }),
                      latest.runID == runID, latest.stage.isPending, !latest.runWasTerminal else { return }
                do {
                    if error as? APIError == .runNotFound {
                        try markRunUnavailable(latest, chatID: chatID)
                    } else {
                        try updateMessage(chatID, message.id) {
                            $0.error = "Could not stop Hermes: \(error.localizedDescription) Tap Stop to try again."
                        }
                    }
                } catch { show(error, title: "Could not save stop request") }
            }
        }
    }

    private func markRunUnavailable(_ message: ChatMessage, chatID: String) throws {
        guard message.runID != nil, message.stage.isPending, !message.runWasTerminal else { return }
        try updateMessage(chatID, message.id) {
            $0.stage = .failed
            // This local attempt cannot resume; its remote outcome is still unknown.
            $0.runWasTerminal = true
            $0.error = APIError.runNotFound.localizedDescription
        }
        cancelWorker(chatID)
    }

    private func cancelWorker(_ chatID: String) {
        workerIDs[chatID] = nil
        workers.removeValue(forKey: chatID)?.cancel()
        stopTaskIDs[chatID] = nil
        stopTasks.removeValue(forKey: chatID)?.cancel()
        clearStreaming(chatID)
        approvals[chatID] = nil
    }

    func respondToApproval(_ choice: String, approval: RunApproval) async {
        guard let current = approvals[approval.chatID],
              current.id == approval.id, current.runID == approval.runID,
              current.requestID == approval.requestID, current.command == approval.command,
              current.choices == approval.choices, current.choices.contains(choice),
              let message = store.chat(id: approval.chatID)?.messages.first(where: { $0.runID == approval.runID && $0.role == .user }),
              message.stage.isPending, message.stopRequested != true,
              let client = makeClient() else { return }
        do {
            try await client.approve(runID: approval.runID, choice: choice, requestID: approval.requestID)
            if approvals[approval.chatID]?.id == approval.id { approvals[approval.chatID] = nil }
        } catch { show(error, title: "Could not submit approval") }
    }

    func settingsChanged() {
        refreshSpeechSelection()
        connectionMessage = nil
        if observedModelServerURL != settings.serverURL || observedModelToken != settings.token {
            observedModelServerURL = settings.serverURL
            observedModelToken = settings.token
            modelInventoryRevision = UUID()
            let inventory = modelInventoryCache.load(serverURL: settings.serverURL, token: settings.token)
                ?? HermesModelInventory(defaultModel: nil, suggestedModels: [], availableModels: [])
            availableModels = inventory.availableModels
            availableModelsByID = Dictionary(inventory.availableModels.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
            configuredDefaultModel = inventory.defaultModel
            suggestedModels = inventory.suggestedModels
            isLoadingModels = false
            modelSelectionError = nil
            adoptConfiguredDefaultForEmptyChat()
            Task { await refreshModelChoices() }
        }
        resumePending()
        Task { await notifications?.refresh(client: makeClient()) }
    }

    private func kick(_ chatID: String) {
        guard workers[chatID] == nil, let client = makeClient(),
              store.chat(id: chatID)?.hasPendingMessages == true else { return }
        scheduleTitle(chatID)
        let workerID = UUID()
        workerIDs[chatID] = workerID
        workers[chatID] = Task { [weak self] in
            guard let self else { return }
            defer {
                if workerIDs[chatID] == workerID {
                    workers[chatID] = nil
                    workerIDs[chatID] = nil
                }
            }
            while !Task.isCancelled,
                  let message = store.chat(id: chatID)?.messages.first(where: { $0.role == .user && $0.stage.isPending }) {
                do {
                    try await process(message, chatID: chatID, client: client, workerID: workerID)
                } catch {
                    guard !Task.isCancelled, workerIDs[chatID] == workerID else { return }
                    do {
                        if error as? APIError == .runNotFound,
                           let latest = store.chat(id: chatID)?.messages.first(where: { $0.id == message.id }),
                           latest.runID != nil {
                            try markRunUnavailable(latest, chatID: chatID)
                            return
                        }
                        try updateMessage(chatID, message.id) {
                            guard $0.stage.isPending, !$0.runWasTerminal else { return }
                            if $0.runID != nil {
                                $0.error = $0.error ?? "Could not check the run: \(error.localizedDescription) Return to the app to reconnect, or tap Stop."
                            } else if $0.stopRequested == true, $0.submission != nil {
                                $0.error = "Could not confirm the submitted request: \(error.localizedDescription) Tap Stop to reconnect and stop the same request."
                            } else if $0.stopRequested == true, $0.stage == .savingMemory {
                                $0.error = "Could not confirm whether the thought was saved: \(error.localizedDescription) The save may already have been accepted. Tap Stop to check again."
                            } else {
                                $0.stage = .failed
                                $0.error = error.localizedDescription
                            }
                        }
                    } catch { show(error, title: "Could not save message state") }
                    return
                }
            }
        }
    }

    private func checkWorker(_ chatID: String, _ workerID: UUID) throws {
        try Task.checkCancellation()
        guard workerIDs[chatID] == workerID else { throw CancellationError() }
    }

    private func process(_ original: ChatMessage, chatID: String, client: APIClient, workerID: UUID) async throws {
        try checkWorker(chatID, workerID)
        var message = original
        if message.runID == nil {
            if message.stopRequested == true, message.submission == nil, message.stage != .savingMemory, message.stage != .submitting {
                try interruptBeforeSubmission(message, chatID: chatID)
                return
            }
            if message.input == .voice, message.text.isEmpty {
                guard let fileName = message.audioFileName else { throw ChatError.missingRecording }
                try updateMessage(chatID, message.id) { $0.stage = .transcribing; $0.error = nil }
                let transcript = try await client.transcribe(file: store.audioURL(fileName: fileName))
                try checkWorker(chatID, workerID)
                guard !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw ChatError.noSpeech }
                message.text = transcript
                try updateChat(chatID) { chat in
                    guard let index = chat.messages.firstIndex(where: { $0.id == message.id }) else { return }
                    chat.messages[index].text = transcript
                }
            }
            // Transcription makes the first voice message meaningful without waiting
            // for classification, agent completion, or a spoken reply.
            if message.input == .voice { scheduleTitle(chatID) }
            if message.input == .voice, message.files.isEmpty, message.classification == nil {
                try updateMessage(chatID, message.id) { $0.stage = .classifying; $0.error = nil }
                let previous = (store.chat(id: chatID)?.messages ?? [])
                    .prefix { $0.id != message.id }
                    .filter { !$0.isBrainDump && $0.stage == .completed }
                    .suffix(6)
                let context = await Task.detached(priority: .userInitiated) {
                    previous.map { RecordingContextMessage(role: $0.role.rawValue, text: $0.role == .assistant ? ResponseContent(raw: $0.text).plainText : $0.text) }
                }.value
                try checkWorker(chatID, workerID)
                let classification = try await client.classifyRecording(text: message.text, context: context)
                try checkWorker(chatID, workerID)
                message.classification = classification
                // A retry must preserve this decision, particularly after an uncertain memory write.
                try updateMessage(chatID, message.id) { $0.classification = classification }
            }
            if message.isBrainDump {
                try updateMessage(chatID, message.id) { $0.stage = .savingMemory; $0.error = nil }
                try await client.retainBrainDump(id: message.id, text: message.text, recordedAt: message.createdAt)
                guard workerIDs[chatID] == workerID else { throw CancellationError() }
                try updateChat(chatID) { chat in
                    guard let index = chat.messages.firstIndex(where: { $0.id == message.id }) else { return }
                    chat.messages[index].stage = .completed
                    chat.messages[index].error = nil
                    chat.messages[index].stopRequested = nil
                    chat.messages[index].stopAcknowledged = nil
                    if chat.sessionID == nil, chat.messages.filter({ $0.role == .user }).allSatisfy(\.isBrainDump) {
                        chat.titleNeedsPublishing = nil
                    }
                }
                scheduleTitle(chatID)
                return
            }
            if message.submission == nil {
                if availableModels.isEmpty { await refreshModelChoices() }
                try checkWorker(chatID, workerID)
                guard let choice = message.modelChoice ?? store.chat(id: chatID)?.modelChoice ?? configuredDefaultModel,
                      availableModelsByID[choice.id] != nil else { throw ChatError.modelNotSelected }
                message.modelChoice = choice
                for index in message.files.indices where message.files[index].remotePath == nil {
                    let file = message.files[index]
                    try updateMessage(chatID, message.id) { $0.stage = .uploading; $0.error = nil }
                    let uploaded = try await client.uploadAttachment(
                        file: attachmentURL(file), id: file.id, name: file.name, contentType: file.contentType)
                    try checkWorker(chatID, workerID)
                    message.attachments?[index].remotePath = uploaded.path
                    try updateMessage(chatID, message.id) { $0.attachments = message.attachments }
                }
                guard let chat = store.chat(id: chatID) else { throw CancellationError() }
                var submission = RunSubmission(input: try message.agentInput(), sessionID: chat.sessionID,
                                               instructions: MobileResponseFormat.instructions, sessionKey: chat.sessionKey)
                submission.modelChoice = message.modelChoice
                submission.thinkingLevel = message.thinkingLevel
                message.submission = submission
                try updateMessage(chatID, message.id) {
                    $0.modelChoice = message.modelChoice
                    $0.submission = submission
                }
            }
            guard let chat = store.chat(id: chatID) else { throw CancellationError() }
            try updateMessage(chatID, message.id) { $0.stage = .submitting; $0.error = nil }
            guard let submission = message.submission else { throw CancellationError() }
            let sessionKey = submission.sessionKey ?? chat.sessionKey
            // Legacy imported attempts predate UUID local IDs and could not register
            // push destinations. Recover their original fingerprint without adding one.
            let push = sessionKey == chat.sessionKey
                ? try await notifications?.destination(chatID: chatID, client: client) : nil
            try checkWorker(chatID, workerID)
            if push != nil {
                message.replyNotificationRequested = true
                try updateMessage(chatID, message.id) { $0.replyNotificationRequested = true }
            }
            let receipt = try await client.startRun(text: submission.input, sessionKey: sessionKey, sessionID: submission.sessionID, idempotencyKey: message.requestKey, push: push, instructions: submission.instructions, modelChoice: submission.modelChoice, thinkingLevel: submission.thinkingLevel)
            // Never let an old cancelled callback overwrite a replacement worker. The
            // persisted submission lets that worker recover this same run idempotently.
            guard workerIDs[chatID] == workerID else { throw CancellationError() }
            message.runID = receipt.runID
            try updateMessage(chatID, message.id) { $0.runID = receipt.runID; $0.stage = .running }
        }
        guard let runID = message.runID else { return }
        try checkWorker(chatID, workerID)
        if let latest = store.chat(id: chatID)?.messages.first(where: { $0.id == message.id }) {
            requestStop(latest, chatID: chatID, client: client)
        }
        let eventsTask = Task { [weak self] in
            do {
                for try await event in client.events(runID: runID) {
                    guard !Task.isCancelled else { return }
                    self?.receive(event, runID: runID, chatID: chatID, workerID: workerID)
                }
            } catch {
                // Polling is authoritative and also recovers outstanding approval requests.
            }
        }
        defer {
            eventsTask.cancel()
            if workerIDs[chatID] == workerID {
                clearStreaming(chatID)
                approvals[chatID] = nil
            }
        }
        while !Task.isCancelled {
            let run = try await client.run(id: runID)
            try checkWorker(chatID, workerID)
            if let sessionID = run.sessionID, store.chat(id: chatID)?.sessionID != sessionID {
                try updateChat(chatID) {
                    if $0.sessionRootID == nil { $0.sessionRootID = $0.sessionID ?? sessionID }
                    if $0.titleGenerated == true, $0.sessionID == nil { $0.titleNeedsPublishing = true }
                    $0.sessionID = sessionID
                }
                scheduleTitle(chatID)
            }
            if let request = run.approval { receive(request, runID: runID, chatID: chatID, workerID: workerID) }
            if run.isTerminal {
                try finish(run, message: message, chatID: chatID)
                if run.status == "completed" { scheduleTitle(chatID) }
                return
            }
            if let latest = store.chat(id: chatID)?.messages.first(where: { $0.id == message.id }),
               latest.error != nil, latest.stopRequested != true || latest.stopAcknowledged == true {
                try updateMessage(chatID, message.id) { $0.error = nil }
            }
            try await Task.sleep(for: .seconds(1))
        }
        throw CancellationError()
    }

    private func receive(_ event: RunEvent, runID: String, chatID: String, workerID: UUID) {
        guard workerIDs[chatID] == workerID,
              let message = store.chat(id: chatID)?.messages.first(where: { $0.runID == runID && $0.role == .user }),
              message.stage.isPending, !message.runWasTerminal else { return }
        switch event.type {
        case "message.delta":
            if let text = event.text { appendStreaming(text, chatID: chatID) }
        case "approval.request":
            guard message.stopRequested != true else { return }
            approvals[chatID] = RunApproval(id: event.id ?? runID, runID: runID, chatID: chatID, command: event.command ?? "Hermes requests permission to continue.", choices: event.choices ?? ["once", "deny"], requestID: event.id)
        default: break
        }
    }

    private func finish(_ run: AgentRun, message: ChatMessage, chatID: String) throws {
        let completed = run.status == "completed"
        let replyID = "\(message.id)-reply-\(message.attempt)"
        try updateChat(chatID) { chat in
            guard let index = chat.messages.firstIndex(where: { $0.id == message.id }) else { return }
            chat.messages[index].stage = completed ? .completed : (run.status == "cancelled" || run.status == "interrupted" ? .interrupted : .failed)
            chat.messages[index].runWasTerminal = true
            chat.messages[index].error = completed ? nil : (run.error ?? "Hermes ended the run (\(run.status)). Actions may already have been performed.")
            if completed {
                chat.messages[index].stopRequested = nil
                chat.messages[index].stopAcknowledged = nil
            }
            if let output = run.output, !output.isEmpty, !chat.messages.contains(where: { $0.id == replyID }) {
                chat.messages.append(ChatMessage(id: replyID, role: .assistant, input: message.input, text: output, stage: .completed, runID: run.id, replyTo: message.id, needsSpeech: completed && message.input == .voice, replyNotificationRequested: message.replyNotificationRequested))
            }
        }
        clearStreaming(chatID)
        approvals[chatID] = nil
        stopTaskIDs[chatID] = nil
        stopTasks.removeValue(forKey: chatID)?.cancel()
        if let reply = store.chat(id: chatID)?.messages.first(where: { $0.id == replyID }), reply.needsSpeech {
            scheduleSpeech(reply, chatID: chatID)
        }
        acknowledgeReplies(chatID)
    }

    func copyMessage(_ message: ChatMessage) {
        copyTask?.cancel()
        copyTask = nil
        guard message.role == .assistant else {
            UIPasteboard.general.string = message.text
            return
        }
        let raw = message.text
        let changeCount = UIPasteboard.general.changeCount
        copyTask = Task {
            guard !Task.isCancelled else { return }
            let worker = Task.detached(priority: .userInitiated) {
                guard !Task.isCancelled else { return "" }
                return ResponseContent(raw: raw).plainText
            }
            let text = await withTaskCancellationHandler {
                await worker.value
            } onCancel: {
                worker.cancel()
            }
            guard !Task.isCancelled, UIPasteboard.general.changeCount == changeCount else { return }
            UIPasteboard.general.string = text
        }
    }

    func play(_ message: ChatMessage) async {
        guard message.role == .assistant, !isRecordingInProgress else { return }
        refreshSpeechSelection()
        guard let chat = store.chats.first(where: { $0.messages.contains(where: { $0.id == message.id }) }),
              let current = chat.messages.first(where: { $0.id == message.id }) else { return }
        if playingMessageID == message.id, current.hasSpeech(for: settings.speechVoice), current.audioFileName != nil {
            do {
                if player.isPlaying { player.pause() }
                else { try await player.resume() }
            } catch { show(error, title: "Could not resume audio") }
            return
        }
        if playingMessageID == message.id { stopPlayback() }
        await synthesizeAndPlay(current, chatID: chat.id, automatic: false)
    }

    func pausePlayback() { player.pause() }

    func stopPlayback() { player.stop() }

    private func scheduleSpeech(_ message: ChatMessage, chatID: String) {
        guard speechTasks[message.id] == nil else { return }
        speechTasks[message.id] = Task { [weak self] in
            guard let self else { return }
            defer { speechTasks[message.id] = nil }
            await synthesizeAndPlay(message, chatID: chatID, automatic: true)
        }
    }

    private func refreshSpeechSelection() {
        guard observedSpeechVoice != settings.speechVoice else { return }
        observedSpeechVoice = settings.speechVoice
        speechSelectionRevision = UUID()
        stopPlayback()
    }

    private func synthesizeAndPlay(_ message: ChatMessage, chatID: String, automatic: Bool) async {
        guard message.role == .assistant, !synthesizingMessageIDs.contains(message.id) else { return }
        synthesizingMessageIDs.insert(message.id)
        defer { synthesizingMessageIDs.remove(message.id) }
        while !Task.isCancelled {
            refreshSpeechSelection()
            let generalVoice = settings.speechVoice
            let revision = speechSelectionRevision
            guard let current = store.chat(id: chatID)?.messages.first(where: { $0.id == message.id }) else { return }
            if automatic, !current.needsSpeech { return }
            do {
                var fileName = current.hasSpeech(for: generalVoice) ? current.audioFileName : nil
                if fileName == nil || !FileManager.default.fileExists(atPath: store.audioURL(fileName: fileName!).path) {
                    guard let client = makeClient() else { throw ChatError.notConfigured }
                    let speechText = await Task.detached(priority: .userInitiated) { ResponseContent(raw: current.text).spokenText }.value
                    try Task.checkCancellation()
                    guard revision == speechSelectionRevision, generalVoice == settings.speechVoice else {
                        if automatic { continue }
                        return
                    }
                    guard store.chat(id: chatID)?.messages.first(where: { $0.id == message.id })?.text == current.text else { return }
                    guard !speechText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                        throw APIError.cannotPrepare("This reply has no readable text to speak.")
                    }
                    let voice = try await client.selectSpeechVoice(text: speechText, generalVoice: generalVoice)
                    try Task.checkCancellation()
                    guard revision == speechSelectionRevision, generalVoice == settings.speechVoice else {
                        if automatic { continue }
                        return
                    }
                    guard store.chat(id: chatID)?.messages.first(where: { $0.id == message.id })?.text == current.text else { return }
                    let audio = try await client.speak(text: speechText, voice: voice)
                    try Task.checkCancellation()
                    guard revision == speechSelectionRevision, generalVoice == settings.speechVoice else {
                        // Pending automatic voice replies follow the latest selection. An
                        // on-demand Listen is discarded; the next Listen is a fresh request.
                        if automatic { continue }
                        return
                    }
                    guard store.chat(id: chatID)?.messages.first(where: { $0.id == message.id })?.text == current.text else { return }
                    let name = "\(message.id)-\(voice.cacheKey)-\(UUID().uuidString.lowercased()).\(audio.fileExtension)"
                    let url = store.audioURL(fileName: name)
                    try audio.data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                    do {
                        try updateMessage(chatID, message.id) {
                            $0.audioFileName = name
                            $0.speechVoice = voice
                            $0.speechGeneralVoice = generalVoice
                            $0.needsSpeech = false
                            $0.error = nil
                        }
                    } catch {
                        try? FileManager.default.removeItem(at: url)
                        throw error
                    }
                    // The previous file is retired only after the replacement metadata is
                    // durable, and never if another message (including a recording) uses it.
                    if let oldName = current.audioFileName, oldName != name,
                       !store.chats.contains(where: { $0.messages.contains(where: { $0.audioFileName == oldName }) }) {
                        try? FileManager.default.removeItem(at: store.audioURL(fileName: oldName))
                    }
                    fileName = name
                } else if current.needsSpeech || current.error != nil {
                    try updateMessage(chatID, message.id) { $0.needsSpeech = false; $0.error = nil }
                }
                guard revision == speechSelectionRevision, generalVoice == settings.speechVoice else { return }
                if let fileName, foreground, selectedChatID == chatID, !isRecordingInProgress,
                   !automatic || player.isIdle {
                    try await player.play(url: store.audioURL(fileName: fileName), messageID: message.id)
                }
                return
            } catch {
                if Task.isCancelled || error is CancellationError { return }
                guard revision == speechSelectionRevision, generalVoice == settings.speechVoice else {
                    if automatic { continue }
                    return
                }
                guard store.chat(id: chatID)?.messages.first(where: { $0.id == message.id })?.text == current.text else { return }
                do {
                    try updateMessage(chatID, message.id) { $0.needsSpeech = false; $0.error = "Audio: \(error.localizedDescription)" }
                } catch { show(error, title: "Could not save audio state") }
                return
            }
        }
    }

    func refreshChats() async {
        guard !isRefreshing, let client = makeClient() else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let sessions = try await client.sessions()
            for session in sessions {
                var aliases: Set<String> = [session.id, session.lineageRootID ?? session.id]
                guard aliases.isDisjoint(with: store.hiddenSessionIDs) else { continue }
                let before = store.chats
                let known = owner(in: before, sessionIDs: aliases)
                guard known?.hasPendingMessages != true,
                      known.map({ titleTasks[$0.id] == nil }) ?? true else { continue }
                let history = try await client.messages(sessionID: session.id)
                try Task.checkCancellation()
                aliases.formUnion(history.sessionIDs)
                // Row session IDs recover old phone records saved against a middle
                // compression segment before root identity was stored locally.
                let original = owner(in: before, sessionIDs: aliases)
                let latest = owner(in: store.chats, sessionIDs: aliases)
                guard aliases.isDisjoint(with: store.hiddenSessionIDs),
                      latest?.id == original?.id, latest?.messages == original?.messages,
                      latest?.hasPendingMessages != true,
                      latest.map({ titleTasks[$0.id] == nil }) ?? true else { continue }
                var chat = latest ?? Chat(title: session.title ?? "Untitled chat",
                                         createdAt: history.messages.first?.createdAt ?? .now,
                                         updatedAt: session.updatedAt ?? .now,
                                         titleGenerated: session.title == nil ? nil : true)
                chat.sessionRootID = session.lineageRootID ?? chat.sessionRootID ?? chat.sessionID ?? session.id
                chat.sessionID = history.sessionID
                chat.mergeHistory(history.messages, sessionID: session.id, supersededMessageIDs: history.supersededMessageIDs)
                guard !chat.messages.isEmpty else { continue }
                if let title = session.title, !title.isEmpty,
                   chat.titleNeedsPublishing != true, chat.titleGenerated == true {
                    chat.title = title
                }
                if let updatedAt = session.updatedAt { chat.updatedAt = updatedAt }
                if chat != latest { try store.save(chat) }
            }
            connectionMessage = nil
        } catch {
            if !Task.isCancelled { connectionMessage = error.localizedDescription }
        }
        resumePending()
    }

    private func owner(in chats: [Chat], sessionIDs: Set<String>) -> Chat? {
        chats.first { chat in
            sessionIDs.contains(chat.id)
                || chat.sessionID.map(sessionIDs.contains) == true
                || chat.sessionRootID.map(sessionIDs.contains) == true
                // A fresh native session can become visible before its first status reaches us.
                || chat.messages.contains { $0.role == .user && $0.runID.map(sessionIDs.contains) == true }
        }
    }

    /// Owned by RootView's foreground task; SwiftUI cancels it on suspension.
    func synchronizeChats() async {
        async let modelRefresh: Void = refreshModelChoices()
        await synchronizeChatHistory()
        await modelRefresh
    }

    private func synchronizeChatHistory() async {
        while !Task.isCancelled {
            await refreshChats()
            do { try await Task.sleep(for: .seconds(15)) }
            catch { return }
        }
    }

    func handle(_ url: URL) {
        guard url.scheme?.lowercased() == "hermesvoice" else { return }
        switch url.host()?.lowercased() {
        case "record": Task { await newVoiceChat() }
        case "compose": newChat()
        default: break
        }
    }

    func receivedReplyNotification(_ notification: ReplyNotification, openChat: Bool) {
        guard let chat = store.chat(id: notification.chatID),
              chat.messages.contains(where: { $0.runID == notification.runID })
                || chat.messages.contains(where: { $0.role == .user && $0.stage.isPending && $0.submission != nil && $0.runID == nil })
        else { return }
        if openChat {
            isSettingsPresented = false
            selectChat(chat.id)
        }
        if foreground {
            kick(chat.id)
            acknowledgeReplies(chat.id)
        }
    }

    private func acknowledgeReplies(_ chatID: String) {
        guard foreground, let notifications, let client = makeClient(),
              let chat = store.chat(id: chatID) else { return }
        for message in chat.messages where message.role == .assistant && message.replyNotificationRequested == true && message.replyNotificationAcknowledged != true {
            guard let runID = message.runID, replyAcknowledgements[runID] == nil else { continue }
            replyAcknowledgements[runID] = Task { [weak self] in
                guard let self else { return }
                defer { replyAcknowledgements[runID] = nil }
                do {
                    try await notifications.acknowledge(runID: runID, client: client)
                    try updateMessage(chatID, message.id) { $0.replyNotificationAcknowledged = true }
                } catch {
                    connectionMessage = "Could not confirm reply notification delivery: \(error.localizedDescription)"
                }
            }
        }
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .active:
            foreground = true
            endBackgroundTask()
            Task { await notifications?.refresh(client: makeClient()) }
            resumePending()
            if startWhenActive {
                startWhenActive = false
                Task { await startRecording() }
            }
        case .background:
            foreground = false
            if !workers.isEmpty || !speechTasks.isEmpty {
                backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "Finish Hermes submission") { [weak self] in
                    MainActor.assumeIsolated {
                        guard let self else { return }
                        for chat in self.store.chats { self.cancelWorker(chat.id) }
                        for task in self.speechTasks.values { task.cancel() }
                        self.endBackgroundTask()
                    }
                }
            }
        case .inactive:
            foreground = false
        default: break
        }
    }

    private func endBackgroundTask() {
        if backgroundTask != .invalid {
            UIApplication.shared.endBackgroundTask(backgroundTask)
            backgroundTask = .invalid
        }
    }

    private func resumePending() {
        guard makeClient() != nil else { return }
        for chat in store.chats {
            kick(chat.id)
            scheduleTitle(chat.id)
            if foreground { acknowledgeReplies(chat.id) }
            for message in chat.messages where message.role == .assistant && message.needsSpeech {
                scheduleSpeech(message, chatID: chat.id)
            }
        }
    }

    private func updateChat(_ id: String, _ change: (inout Chat) -> Void) throws {
        guard var chat = store.chat(id: id) else { throw CancellationError() }
        change(&chat)
        chat.updatedAt = .now
        try store.save(chat)
    }

    private func updateMessage(_ chatID: String, _ id: String, _ change: (inout ChatMessage) -> Void) throws {
        try updateChat(chatID) { chat in
            guard let index = chat.messages.firstIndex(where: { $0.id == id }) else { return }
            change(&chat.messages[index])
        }
    }

    private func show(_ error: any Error, title: String) {
        alert = AppAlert(title: title, message: error.localizedDescription)
    }

    private func makeClient() -> APIClient? { clientOverride ?? settings.makeClient() }

    private func scheduleTitle(_ chatID: String) {
        guard titleTasks[chatID] == nil, let client = makeClient(),
              let chat = store.chat(id: chatID), chat.titleGenerated != true || chat.titleNeedsPublishing == true,
              let first = chat.messages.first(where: {
                  $0.role == .user && (!$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !$0.files.isEmpty)
              }),
              chat.titleGenerated != true || chat.sessionID != nil
        else { return }
        titleTasks[chatID] = Task { [weak self] in
            guard let self else { return }
            defer { titleTasks[chatID] = nil }
            do {
                if chat.titleGenerated != true {
                    // A single topic description can be generated in parallel with
                    // the full agent run; assistant output is not needed for naming.
                    let text = first.text.isEmpty ? first.files.map(\.name).joined(separator: ", ") : first.text
                    let title = try await client.generateTitle(messages: [
                        RecordingContextMessage(role: MessageRole.user.rawValue, text: text)
                    ])
                    try Task.checkCancellation()
                    guard var latest = store.chat(id: chatID) else { return }
                    latest.title = title
                    latest.titleGenerated = true
                    latest.titleNeedsPublishing = latest.sessionID == nil
                        && latest.messages.filter({ $0.role == .user }).allSatisfy(\.isBrainDump) ? nil : true
                    try store.save(latest)
                }
                guard let latest = store.chat(id: chatID), latest.titleNeedsPublishing == true,
                      let sessionID = latest.sessionID else { return }
                let savedTitle = try await client.setSessionTitle(sessionID: sessionID, title: latest.title)
                try Task.checkCancellation()
                guard var saved = store.chat(id: chatID) else { return }
                saved.title = savedTitle
                saved.titleNeedsPublishing = nil
                try store.save(saved)
                if selectedChatID == chatID,
                   connectionMessage?.hasPrefix("Could not generate the chat title:") == true
                    || connectionMessage?.hasPrefix("Could not publish the chat title:") == true {
                    connectionMessage = nil
                }
            } catch {
                if !Task.isCancelled, selectedChatID == chatID {
                    let action = store.chat(id: chatID)?.titleGenerated == true ? "publish" : "generate"
                    connectionMessage = "Could not \(action) the chat title: \(error.localizedDescription)"
                }
            }
        }
    }
}

private enum ChatError: LocalizedError {
    case missingRecording, noSpeech, notConfigured, modelNotSelected
    var errorDescription: String? {
        switch self {
        case .missingRecording: "The recording file is missing. Record your message again."
        case .noSpeech: "No speech was detected. Record your message again, or type it instead."
        case .notConfigured: "Connect your Hermes server in Settings to generate speech."
        case .modelNotSelected: "Choose an available model in the chat header before sending. Hermes’s global default is never selected automatically."
        }
    }
}
