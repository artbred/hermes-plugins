import Foundation
import Synchronization
import Testing
@testable import HermesVoice

@MainActor
@Suite("Explicit chat model selection")
struct ChatModelSelectionTests {
    private let first = HermesModelChoice(provider: "openrouter", modelID: "example/model-a", displayName: "Model A")
    private let second = HermesModelChoice(provider: "openrouter", modelID: "example/model-b", displayName: "Model B")

    @Test("Hermes default and common interactive models replace old preferences without catalogue refill")
    func configuredDefaultAndCommonChoices() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        let settings = AppSettings(service: service)
        let legacyPreference = KeychainItem(service: service, account: "nativePreferredChatModel")
        defer { _ = legacyPreference.write(nil) }
        #expect(legacyPreference.write(String(decoding: try JSONEncoder().encode(second), as: UTF8.self)))
        var empty = Chat()
        empty.modelChoice = second
        let store = ChatStore(directory: directory)
        try store.save(empty)
        let server = StubServer { request in
            if request.path == "/api/model/options" {
                return .json(200, #"{"model":"example/model-a","provider":"openrouter","providers":[{"slug":"openrouter","authenticated":true,"models":["example/model-a","example/model-b","example/random"],"featured_models":["example/random"],"unavailable_models":[]}]}"#)
            }
            if request.path == "/api/sessions" {
                return .json(200, #"{"data":[{"id":"common","source":"cli","model":"example/model-b","started_at":1,"last_active":2}]}"#)
            }
            return .json(500, #"{"error":"No inference expected"}"#)
        }
        let model = AppModel(settings: settings, store: store, client: server.client())
        await model.refreshModelChoices()
        #expect(model.configuredDefaultModel?.id == first.id)
        #expect(model.selectedChatModel?.id == first.id)
        #expect(store.chat(id: empty.id)?.modelChoice?.id == first.id)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        model.draft = "A new request"
        #expect(model.canSend)
        #expect(server.requests.allSatisfy { $0.method != "POST" })
    }

    @Test("Injected default labels normalize by exact identity and remain first after manual selection")
    func normalizedDefaultFirstChoices() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        var olderLabel = first
        olderLabel.displayName = "Old presentation"
        let model = AppModel(settings: settings, store: ChatStore(directory: directory), initialModelChoices: [second, first], initialModelChoice: olderLabel)
        #expect(model.selectedChatModel == first)
        #expect(model.configuredDefaultModel == first)
        model.selectChatModel(second)
        #expect(model.selectedChatModel == second)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
        model.newChat()
        #expect(model.selectedChatModel == first)
    }

    @Test("Manual empty-chat choice survives inventory refresh but new chats use the latest server default")
    func manualChoiceAndDefaultChange() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        let useSecondDefault = Mutex(false)
        let server = StubServer { request in
            if request.path == "/api/model/options" {
                let defaultID = useSecondDefault.withLock { $0 } ? "example/model-b" : "example/model-a"
                return .json(200, "{\"model\":\"\(defaultID)\",\"provider\":\"openrouter\",\"providers\":[{\"slug\":\"openrouter\",\"authenticated\":true,\"models\":[\"example/model-a\",\"example/model-b\"],\"unavailable_models\":[]}]}")
            }
            if request.path == "/api/sessions" { return .json(200, #"{"data":[]}"#) }
            return .json(500, #"{"error":"No inference expected"}"#)
        }
        let store = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: store, client: server.client())
        await model.refreshModelChoices()
        model.newChat()
        model.selectChatModel(second)
        let manualID = try #require(model.selectedChatID)
        await model.refreshModelChoices()
        #expect(model.selectedChatModel?.id == second.id)
        #expect(model.modelChoices.map(\.id) == [first.id])
        model.newChat()
        #expect(model.selectedChatModel?.id == first.id)
        useSecondDefault.withLock { $0 = true }
        await model.refreshModelChoices()
        #expect(model.configuredDefaultModel?.id == second.id)
        #expect(model.selectedChatModel?.id == second.id)
        model.selectChatModel(first)
        await model.refreshModelChoices()
        #expect(model.selectedChatModel?.id == first.id)
        model.newChat()
        #expect(model.selectedChatModel?.id == second.id)
        #expect(store.chat(id: manualID)?.modelChoice?.id == second.id)
    }

    @Test("A refreshed server default does not rewrite an existing nonempty chat")
    func nonemptyChatKeepsExplicitChoice() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        let server = StubServer { request in
            if request.path == "/api/model/options" {
                return .json(200, #"{"model":"example/model-b","provider":"openrouter","providers":[{"slug":"openrouter","authenticated":true,"models":["example/model-a","example/model-b"],"unavailable_models":[]}]}"#)
            }
            if request.path == "/api/sessions" { return .json(200, #"{"data":[]}"#) }
            return .json(500, #"{"error":"No inference expected"}"#)
        }
        var chat = Chat(messages: [ChatMessage(role: .assistant, text: "Already complete")])
        chat.modelChoice = first
        let store = ChatStore(directory: directory)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        await model.refreshModelChoices()
        #expect(model.selectedChatModel?.id == first.id)
        #expect(store.chat(id: chat.id)?.modelChoice == first)
        #expect(model.modelChoices.map(\.id) == [second.id])
        model.newChat()
        #expect(model.selectedChatModel?.id == second.id)
    }

    @Test("A pending admission keeps its frozen model when the server default changes")
    func pendingAdmissionKeepsQueuedModel() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        let admission = DeferredStubResponse()
        let server = StubServer { request in
            switch request.path {
            case "/v1/runs": return .deferred(admission)
            case "/v1/runs/model-a":
                return .json(200, #"{"run_id":"model-a","status":"completed","session_id":"model-a","output":"A completed reply"}"#)
            case "/api/model/options":
                return .json(200, #"{"model":"example/model-b","provider":"openrouter","providers":[{"slug":"openrouter","authenticated":true,"models":["example/model-a","example/model-b"],"unavailable_models":[]}]}"#)
            case "/api/sessions": return .json(200, #"{"data":[]}"#)
            default: return .json(500, #"{"error":"No unrelated endpoint needed"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: store, client: server.client(), initialModelChoices: [first, second], initialModelChoice: first)
        model.sendText("Use the selected model")
        let originalID = try #require(model.selectedChatID)
        try #require(await eventually { server.requests.contains { $0.path == "/v1/runs" } })
        await model.refreshModelChoices()
        #expect(model.configuredDefaultModel?.id == second.id)
        #expect(model.selectedChatModel?.id == first.id)
        model.newChat()
        model.selectChatModel(second)
        admission.resolve(.json(202, #"{"run_id":"model-a","status":"queued"}"#))
        try #require(await eventually { store.chat(id: originalID)?.messages.last?.role == .assistant })
        let queued = try #require(store.chat(id: originalID)?.messages.first)
        #expect(queued.modelChoice == first)
        #expect(queued.submission?.modelChoice == first)
        #expect(model.selectedChatModel?.id == second.id)
        let request = try #require(server.requests.first { $0.path == "/v1/runs" })
        let body = try #require(JSONSerialization.jsonObject(with: request.body) as? [String: Any])
        #expect(body["provider"] as? String == first.provider)
        #expect(body["model"] as? String == first.modelID)
    }

    @Test("Lost admission recovery preserves the frozen choice after relaunch and an explicit model change")
    func lostAdmissionKeepsFrozenChoice() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        let admit = Mutex(false)
        let server = StubServer { request in
            switch request.path {
            case "/v1/runs":
                return admit.withLock { $0 } ? .json(202, #"{"run_id":"recovered","status":"queued"}"#) : .failure(.networkConnectionLost)
            case "/v1/runs/recovered":
                return .json(200, #"{"run_id":"recovered","status":"completed","session_id":"recovered","output":"Recovered the original model"}"#)
            default: return .json(500, #"{"error":"No unrelated endpoint needed"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let initial = AppModel(settings: settings, store: store, client: server.client(), initialModelChoices: [first, second], initialModelChoice: first)
        initial.sendText("Recover exactly this request")
        let chatID = try #require(initial.selectedChatID)
        try #require(await eventually { store.chat(id: chatID)?.messages.first?.stage == .failed })
        let restored = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: restored, client: server.client(), initialModelChoices: [first, second], initialModelChoice: second)
        model.selectChatModel(second)
        admit.withLock { $0 = true }
        model.retry(try #require(restored.chat(id: chatID)?.messages.first))
        try #require(await eventually { restored.chat(id: chatID)?.messages.last?.role == .assistant })
        let requests = server.requests.filter { $0.path == "/v1/runs" }
        #expect(requests.count == 2)
        let firstBody = try #require(JSONSerialization.jsonObject(with: requests[0].body) as? NSDictionary)
        let replayBody = try #require(JSONSerialization.jsonObject(with: requests[1].body) as? NSDictionary)
        #expect(firstBody == replayBody)
        #expect(requests[0].request.value(forHTTPHeaderField: "Idempotency-Key") == requests[1].request.value(forHTTPHeaderField: "Idempotency-Key"))
        #expect(restored.chat(id: chatID)?.messages.first?.submission?.modelChoice == first)
        #expect(restored.chat(id: chatID)?.modelChoice == second)
    }

    @Test("A legacy frozen request recovers without adding model fields that change its idempotency fingerprint")
    func legacyFrozenRecoveryDoesNotBackfill() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        let server = StubServer { request in
            if request.path == "/v1/runs" {
                let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: Any]
                return body?["provider"] == nil && body?["model"] == nil
                    ? .json(202, #"{"run_id":"legacy","status":"queued"}"#)
                    : .json(409, #"{"detail":"Frozen fingerprint changed"}"#)
            }
            if request.path == "/v1/runs/legacy" {
                return .json(200, #"{"run_id":"legacy","status":"completed","session_id":"legacy","output":"Recovered legacy acceptance"}"#)
            }
            return .json(500, #"{"error":"No unrelated endpoint needed"}"#)
        }
        var message = ChatMessage(role: .user, text: "Legacy input", stage: .submitting)
        message.submission = RunSubmission(input: "Legacy input", sessionKey: "ios-chat:legacy")
        let chat = Chat(messages: [message])
        let store = ChatStore(directory: directory)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client(), initialModelChoices: [first], initialModelChoice: first)
        model.scenePhaseChanged(.active)
        try #require(await eventually { store.chat(id: chat.id)?.messages.last?.role == .assistant })
        #expect(store.chat(id: chat.id)?.messages.first?.submission?.modelChoice == nil)
    }

    @Test("Model selection failure does not change the chat or configured default")
    func failedChatCommitKeepsSelection() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        let settings = AppSettings(service: service)
        var chat = Chat()
        chat.modelChoice = first
        let store = ChatStore(directory: directory)
        try store.save(chat)
        let index = directory.appending(path: "chats.json")
        try FileManager.default.removeItem(at: index)
        try FileManager.default.createDirectory(at: index, withIntermediateDirectories: false)
        let model = AppModel(settings: settings, store: store, initialModelChoices: [first, second], initialModelChoice: first)
        model.selectChatModel(second)
        #expect(model.alert != nil)
        #expect(model.selectedChatModel == first)
        #expect(model.configuredDefaultModel == first)
    }

    @Test("Changing only the speech voice retains the explicit chat-model catalog and selection")
    func speechPreferenceDoesNotResetChatModels() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        let settings = AppSettings(service: service)
        defer {
            for account in ["nativeServerURL", "nativeAPIToken", "nativeVoiceID", "nativeSpeechURL", "nativeSpeechToken", "nativePreferredChatModel"] {
                KeychainItem(service: service, account: account).write(nil)
            }
        }
        try settings.save(serverURL: "https://example.com", token: "synthetic-token", voiceID: SpeechVoice.defaultReferenceID)
        let model = AppModel(settings: settings, store: ChatStore(directory: directory), initialModelChoices: [first, second], initialModelChoice: first)
        model.draft = "A request"
        try settings.save(serverURL: "https://example.com", token: "synthetic-token", voiceID: "9a9cf47702da476aa4629e2506d4a857")
        model.settingsChanged()
        #expect(model.canSend)
        #expect(model.selectedChatModel == first)
        #expect(model.modelChoices.map(\.id) == [first.id, second.id])
    }
}
