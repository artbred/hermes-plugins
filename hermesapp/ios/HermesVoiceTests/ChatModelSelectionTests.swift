import Foundation
import Synchronization
import Testing
@testable import HermesVoice

@MainActor
@Suite("Explicit chat model selection")
struct ChatModelSelectionTests {
    private let first = HermesModelChoice(provider: "openrouter", modelID: "example/model-a", displayName: "Model A")
    private let second = HermesModelChoice(provider: "openrouter", modelID: "example/model-b", displayName: "Model B")

    @Test("Loading the server catalog never selects its global default or admits an unchosen chat")
    func noImplicitServerDefault() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        let server = StubServer { request in
            if request.path == "/api/model/options" {
                return .json(200, #"{"model":"server-global","provider":"openrouter","providers":[{"slug":"openrouter","authenticated":true,"models":["example/model-a","example/model-b"],"featured_models":["example/model-a"],"unavailable_models":[]}]}"#)
            }
            return .json(500, #"{"error":"Unchosen model must not start an agent"}"#)
        }
        let store = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: store, client: server.client())
        await model.refreshModelChoices()
        model.draft = "A new request"
        #expect(model.modelChoices.count == 2)
        #expect(model.selectedChatModel == nil)
        #expect(!model.canSend)
        model.sendText(model.draft)
        #expect(store.chats.isEmpty)
        #expect(server.requests.allSatisfy { $0.method != "POST" })
    }

    @Test("The five-choice menu preserves recent identities and accepts refreshed display labels")
    func recentAndRenamedChoices() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        defer { try? settings.savePreferredChatModel(nil) }
        let choices = (1...7).map { HermesModelChoice(provider: "openrouter", modelID: "example/model-\($0)", displayName: "Model \($0)") }
        var olderLabel = choices[2]
        olderLabel.displayName = "Old presentation"
        var recent = Chat(updatedAt: .now)
        recent.modelChoice = choices[6]
        let store = ChatStore(directory: directory)
        try store.save(recent)
        let model = AppModel(settings: settings, store: store, initialModelChoices: choices, initialModelChoice: olderLabel)
        model.selectedChatID = nil
        model.draft = "A request"
        #expect(model.selectedChatModel?.displayName == choices[2].displayName)
        #expect(model.canSend)
        #expect(model.modelChoices.map(\.id) == [choices[2], choices[6], choices[0], choices[1], choices[3]].map(\.id))
        model.selectChatModel(olderLabel)
        #expect(settings.preferredChatModel == choices[2])
    }

    @Test("Each chat retains its model while new chats use the last explicit preference")
    func perChatSelectionAndNewChatPreference() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        let settings = AppSettings(service: service)
        defer { try? settings.savePreferredChatModel(nil) }
        let store = ChatStore(directory: directory)
        var original = Chat()
        original.modelChoice = first
        try store.save(original)
        let model = AppModel(settings: settings, store: store, initialModelChoices: [first, second], initialModelChoice: first)
        model.selectChatModel(second)
        #expect(store.chat(id: original.id)?.modelChoice == second)
        model.newChat()
        let newer = try #require(model.selectedChat)
        #expect(newer.modelChoice == second)
        model.selectChat(original.id)
        model.selectChatModel(first)
        model.selectChat(newer.id)
        #expect(model.selectedChatModel == second)
        model.newChat()
        #expect(model.selectedChatModel == first)
        #expect(AppSettings(service: service).preferredChatModel == first)
    }

    @Test("A pending admission keeps its queued model when another chat changes the preference")
    func pendingAdmissionKeepsQueuedModel() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let settings = AppSettings(service: "HermesVoiceTests-\(UUID().uuidString)")
        defer { try? settings.savePreferredChatModel(nil) }
        let admission = DeferredStubResponse()
        let server = StubServer { request in
            switch request.path {
            case "/v1/runs": return .deferred(admission)
            case "/v1/runs/model-a":
                return .json(200, #"{"run_id":"model-a","status":"completed","session_id":"model-a","output":"A completed reply"}"#)
            default: return .json(500, #"{"error":"No unrelated endpoint needed"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let model = AppModel(settings: settings, store: store, client: server.client(), initialModelChoices: [first, second], initialModelChoice: first)
        model.sendText("Use the selected model")
        let originalID = try #require(model.selectedChatID)
        try #require(await eventually { server.requests.contains { $0.path == "/v1/runs" } })
        model.newChat()
        model.selectChatModel(second)
        admission.resolve(.json(202, #"{"run_id":"model-a","status":"queued"}"#))
        try #require(await eventually { store.chat(id: originalID)?.messages.last?.role == .assistant })
        let queued = try #require(store.chat(id: originalID)?.messages.first)
        #expect(queued.modelChoice == first)
        #expect(queued.submission?.modelChoice == first)
        #expect(settings.preferredChatModel == second)
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
        defer { try? settings.savePreferredChatModel(nil) }
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

    @Test("Model selection failure rolls back the preferred model and does not change the chat")
    func failedChatCommitRollsBackPreference() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        let settings = AppSettings(service: service)
        defer { try? settings.savePreferredChatModel(nil) }
        try settings.savePreferredChatModel(first)
        var chat = Chat()
        chat.modelChoice = first
        let store = ChatStore(directory: directory)
        try store.save(chat)
        let index = directory.appending(path: "chats.json")
        try FileManager.default.removeItem(at: index)
        try FileManager.default.createDirectory(at: index, withIntermediateDirectories: false)
        let model = AppModel(settings: settings, store: store, initialModelChoices: [first, second])
        model.selectChatModel(second)
        #expect(model.alert != nil)
        #expect(model.selectedChatModel == first)
        #expect(settings.preferredChatModel == first)
        #expect(AppSettings(service: service).preferredChatModel == first)
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
