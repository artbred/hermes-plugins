import Foundation
import Synchronization
import Testing
@testable import HermesVoice

@MainActor
@Suite("Cached chat opening")
struct CachedChatOpeningTests {
    private let first = HermesModelChoice(provider: "openrouter", modelID: "example/model-a", displayName: "Model A")
    private let second = HermesModelChoice(provider: "openrouter", modelID: "example/model-b", displayName: "Model B")

    nonisolated private static func inventory(defaultID: String = "example/model-a") -> StubResponse {
        .json(200, "{\"model\":\"\(defaultID)\",\"provider\":\"openrouter\",\"providers\":[{\"slug\":\"openrouter\",\"authenticated\":true,\"models\":[\"example/model-a\",\"example/model-b\"],\"unavailable_models\":[]}]}")
    }

    nonisolated private static var usage: StubResponse {
        .json(200, #"{"data":[{"id":"common","source":"cli","model":"example/model-b","started_at":1,"last_active":2}]}"#)
    }

    private func settings(for server: StubServer) throws -> AppSettings {
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        try settings.save(serverURL: server.baseURL().absoluteString, token: "synthetic-cache-token", voiceID: SpeechVoice.defaultReferenceID)
        return settings
    }

    private func seed(directory: URL, settings: AppSettings, server: StubServer) async throws {
        let model = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        await model.refreshModelChoices()
        try #require(model.configuredDefaultModel?.id == first.id)
        try #require(model.modelChoices.map(\.id) == [first.id, second.id])
    }

    @Test("Cold opening restores usable model controls and chat state before deferred metadata arrives")
    func coldOpeningPreservesConversationAndComposer() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let delayed = DeferredStubResponse()
        let warmed = Mutex(false)
        let server = StubServer { request in
            switch request.path {
            case "/api/model/options":
                return warmed.withLock { $0 } ? .deferred(delayed) : Self.inventory()
            case "/api/sessions": return Self.usage
            default: return .json(500, #"{"error":"No inference expected"}"#)
            }
        }
        let settings = try settings(for: server)
        let store = ChatStore(directory: directory)
        let file = ChatAttachment(id: "draft-file", name: "Notes.txt", contentType: "text/plain", byteCount: 3, localFileName: "notes.txt")
        try Data("abc".utf8).write(to: store.attachmentURL(fileName: file.localFileName))
        let message = ChatMessage(role: .assistant, text: "Cached reply", stage: .completed)
        let chat = Chat(messages: [message], draftAttachments: [file], modelChoice: first, thinkingLevel: .high)
        try store.save(chat)
        try await seed(directory: directory, settings: settings, server: server)
        warmed.withLock { $0 = true }
        let restored = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: restored, client: server.client())
        #expect(model.configuredDefaultModel?.id == first.id)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        #expect(model.selectedChat == chat)
        #expect(model.selectedThinkingLevel == .high)
        #expect(model.pendingAttachments == [file])
        #expect(model.canSend)
        model.draft = "Unsent text"
        let refresh = Task { await model.refreshModelChoices() }
        defer { refresh.cancel(); delayed.resolve(.failure(.cancelled)) }
        try #require(await eventually { server.requests.filter { $0.path == "/api/model/options" }.count == 2 })
        model.newChat()
        model.selectChat(chat.id)
        #expect(model.draft == "Unsent text")
        #expect(model.selectedChat == chat)
        #expect(model.selectedThinkingLevel == .high)
        #expect(model.pendingAttachments == [file])
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        #expect(model.canSend)
        delayed.resolve(Self.inventory())
        await refresh.value
        #expect(model.selectedChat == chat)
        #expect(model.draft == "Unsent text")
        #expect(try Data(contentsOf: restored.attachmentURL(fileName: file.localFileName)) == Data("abc".utf8))
        #expect(server.requests.allSatisfy { $0.method == "GET" })
    }

    @Test("Successful background inventory changes defaults without rewriting selected or queued identities")
    func newInventoryPreservesFrozenSubmission() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let delayed = DeferredStubResponse()
        let warmed = Mutex(false)
        let server = StubServer { request in
            if request.path == "/api/model/options" { return warmed.withLock { $0 } ? .deferred(delayed) : Self.inventory() }
            if request.path == "/api/sessions" { return Self.usage }
            return .json(500, #"{"error":"No admission expected"}"#)
        }
        let settings = try settings(for: server)
        try await seed(directory: directory, settings: settings, server: server)
        let submission = RunSubmission(input: "Frozen request", sessionKey: "ios-chat:cached", modelChoice: first, thinkingLevel: .high)
        let queued = ChatMessage(role: .user, text: "Frozen request", stage: .submitting, submission: submission, modelChoice: first, thinkingLevel: .high)
        let chat = Chat(messages: [queued], modelChoice: first, thinkingLevel: .high)
        try ChatStore(directory: directory).save(chat)
        warmed.withLock { $0 = true }
        let store = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: store, client: server.client())
        let refresh = Task { await model.refreshModelChoices() }
        defer { refresh.cancel(); delayed.resolve(.failure(.cancelled)) }
        try #require(await eventually { server.requests.filter { $0.path == "/api/model/options" }.count == 2 })
        #expect(model.selectedChatModel?.id == first.id)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        delayed.resolve(Self.inventory(defaultID: "example/model-b"))
        await refresh.value
        #expect(model.configuredDefaultModel?.id == second.id)
        #expect(model.modelChoices.map(\.id) == [second.id])
        #expect(model.selectedChatModel?.id == first.id)
        #expect(model.selectedThinkingLevel == .high)
        #expect(store.chat(id: chat.id) == chat)
        #expect(store.chat(id: chat.id)?.messages.first?.submission == submission)
        model.newChat()
        #expect(model.selectedChatModel?.id == second.id)
        #expect(model.selectedThinkingLevel == .automatic)
        let relaunched = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        #expect(relaunched.configuredDefaultModel?.id == second.id)
        #expect(relaunched.modelChoices.map(\.id) == [second.id])
        #expect(server.requests.allSatisfy { $0.method == "GET" })
    }

    @Test("Failed refresh exposes an error while keeping cached controls usable across relaunch")
    func failedRefreshPreservesCache() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let fail = Mutex(false)
        let server = StubServer { request in
            if request.path == "/api/model/options" { return fail.withLock { $0 } ? .failure(.notConnectedToInternet) : Self.inventory() }
            if request.path == "/api/sessions" { return Self.usage }
            return .json(500, #"{"error":"No inference expected"}"#)
        }
        let settings = try settings(for: server)
        try await seed(directory: directory, settings: settings, server: server)
        fail.withLock { $0 = true }
        let model = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        model.newChat()
        model.draft = "Ready offline"
        #expect(model.canSend)
        await model.refreshModelChoices()
        #expect(model.modelSelectionError?.isEmpty == false)
        #expect(model.configuredDefaultModel?.id == first.id)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        #expect(model.canSend)
        model.selectChatModel(second)
        #expect(model.selectedChatModel?.id == second.id)
        #expect(model.canSend)
        let restored = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        restored.draft = "Still ready"
        #expect(restored.modelChoices.map(\.id) == [first.id, second.id])
        #expect(restored.canSend)
    }

    @Test("A different server or token cannot reuse cached availability", arguments: [false, true])
    func connectionScopeInvalidation(changeToken: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let fail = Mutex(false)
        let server = StubServer { request in
            if request.path == "/api/model/options" { return fail.withLock { $0 } ? .failure(.notConnectedToInternet) : Self.inventory() }
            if request.path == "/api/sessions" { return Self.usage }
            return .json(500, #"{"error":"No inference expected"}"#)
        }
        let settings = try settings(for: server)
        try await seed(directory: directory, settings: settings, server: server)
        let model = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        model.newChat()
        model.draft = "Cached availability was usable"
        #expect(model.canSend)
        let originalURL = settings.serverURL
        let originalToken = settings.token
        fail.withLock { $0 = true }
        try settings.save(serverURL: changeToken ? originalURL : server.baseURL(path: "/other").absoluteString,
                          token: changeToken ? "other-synthetic-token" : originalToken,
                          voiceID: settings.voiceID)
        let restored = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        restored.draft = "Do not reuse another connection"
        #expect(restored.modelChoices.isEmpty)
        #expect(restored.configuredDefaultModel == nil)
        #expect(!restored.canSend)
        model.settingsChanged()
        #expect(model.modelChoices.isEmpty)
        #expect(model.configuredDefaultModel == nil)
        #expect(!model.canSend)
        try #require(await eventually { model.modelSelectionError != nil })
        #expect(model.modelChoices.isEmpty)
        try settings.save(serverURL: originalURL, token: originalToken, voiceID: settings.voiceID)
        let original = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        original.draft = "Original connection remains cached"
        #expect(original.modelChoices.map(\.id) == [first.id, second.id])
        #expect(original.canSend)
    }

    @Test("Speech-only settings preserve cached models and explicit initializer choices override them")
    func speechOnlyAndInjectedChoices() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = StubServer { request in
            if request.path == "/api/model/options" { return Self.inventory() }
            if request.path == "/api/sessions" { return Self.usage }
            return .json(500, #"{"error":"No inference expected"}"#)
        }
        let settings = try settings(for: server)
        try await seed(directory: directory, settings: settings, server: server)
        let model = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        model.newChat()
        model.draft = "A typed request"
        let requestsBefore = server.requests.count
        try settings.save(serverURL: settings.serverURL, token: settings.token, voiceID: "0123456789abcdef0123456789abcdef")
        model.settingsChanged()
        #expect(model.configuredDefaultModel?.id == first.id)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        #expect(model.canSend)
        #expect(server.requests.count == requestsBefore)
        let restored = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client())
        #expect(restored.modelChoices.map(\.id) == [first.id, second.id])
        let injected = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client(), initialModelChoices: [second], initialModelChoice: second)
        #expect(injected.configuredDefaultModel?.id == second.id)
        #expect(injected.modelChoices.map(\.id) == [second.id])
        #expect(injected.selectedChatModel?.id == second.id)
        let explicitlyEmpty = AppModel(settings: settings, store: ChatStore(directory: directory), client: server.client(), initialModelChoices: [])
        #expect(explicitlyEmpty.modelChoices.isEmpty)
        #expect(explicitlyEmpty.configuredDefaultModel == nil)
    }

    @Test("Foreground history merges fresh replies while model metadata remains deferred")
    func historyDoesNotWaitForModelInventory() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let options = DeferredStubResponse()
        let history = DeferredStubResponse()
        let warmed = Mutex(false)
        let server = StubServer { request in
            switch request.path {
            case "/api/model/options": return warmed.withLock { $0 } ? .deferred(options) : Self.inventory()
            case "/api/sessions":
                if request.query?.contains("include_children=false") == true { return Self.usage }
                if request.query?.contains("source=api_server") != true {
                    return .json(200, #"{"data":[],"has_more":false}"#)
                }
                return .json(200, #"{"data":[{"id":"shared","title":"Shared conversation","last_active":1700000004}],"has_more":false}"#)
            case "/api/sessions/shared/messages": return .deferred(history)
            default: return .json(500, #"{"error":"No inference expected"}"#)
            }
        }
        let settings = try settings(for: server)
        let store = ChatStore(directory: directory)
        let question = ChatMessage(role: .user, input: .voice, text: "Phone question", stage: .completed, runID: "phone-run", remoteMessageID: "1", audioFileName: "take.m4a")
        let answer = ChatMessage(role: .assistant, text: "Cached answer", stage: .completed, runID: "phone-run", remoteMessageID: "2", audioFileName: "reply.mp3", speechVoice: .defaultVoice, speechGeneralVoice: .defaultVoice, replyTo: question.id)
        let local = ChatMessage(role: .user, text: "Local unsynced note", stage: .completed)
        let chat = Chat(sessionID: "shared", messages: [question, answer, local], modelChoice: first, thinkingLevel: .high)
        try store.save(chat)
        try await seed(directory: directory, settings: settings, server: server)
        warmed.withLock { $0 = true }
        let restored = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: restored, client: server.client())
        #expect(model.selectedChat?.messages == chat.messages)
        model.draft = "Continue the conversation"
        let foreground = Task { await model.synchronizeChats() }
        defer {
            foreground.cancel()
            options.resolve(.failure(.cancelled))
            history.resolve(.failure(.cancelled))
        }
        #expect(await eventually { server.requests.filter { $0.path == "/api/model/options" }.count == 2 })
        let reachedHistory = await eventually { server.requests.contains { $0.path == "/api/sessions/shared/messages" } }
        #expect(reachedHistory)
        #expect(model.selectedChat?.messages == chat.messages)
        #expect(model.canSend)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        history.resolve(.json(200, #"{"session_id":"shared","data":[{"id":1,"role":"user","content":"Phone question","timestamp":1700000001},{"id":2,"role":"assistant","content":"Cached answer","timestamp":1700000002},{"id":3,"role":"assistant","content":"Fresh desktop answer","timestamp":1700000004}]}"#))
        let merged = await eventually { model.selectedChat?.messages.contains { $0.text == "Fresh desktop answer" } == true }
        #expect(merged)
        #expect(model.selectedChat?.messages.contains(question) == true)
        #expect(model.selectedChat?.messages.contains(answer) == true)
        #expect(model.selectedChat?.messages.contains(local) == true)
        #expect(model.selectedChat?.messages.count == 4)
        #expect(model.selectedChatModel?.id == first.id)
        #expect(model.selectedThinkingLevel == .high)
        #expect(model.draft == "Continue the conversation")
        #expect(model.canSend)
        foreground.cancel()
        options.resolve(Self.inventory(defaultID: "example/model-b"))
        await foreground.value
        #expect(model.configuredDefaultModel?.id == first.id)
    }
}
