import Foundation
import Testing
import Synchronization
@testable import HermesVoice

@MainActor
@Suite("Native conversations")
struct ChatTests {
    @Test("Accepted run identity and audio survive a relaunch")
    func persistence() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        let user = ChatMessage(role: .user, input: .voice, text: "Remember the appointment", stage: .running, runID: "run-accepted", audioFileName: "recording.m4a")
        let chat = Chat(title: "Appointment", sessionID: "native-session", messages: [user])
        try Data([1, 2, 3]).write(to: store.audioURL(fileName: "recording.m4a"))
        try store.save(chat)
        let restored = ChatStore(directory: directory)
        #expect(restored.chat(id: chat.id) == chat)
        #expect(try Data(contentsOf: restored.audioURL(fileName: "recording.m4a")) == Data([1, 2, 3]))
    }

    @Test("A first-message title appears before admission and is published while the run is still active")
    func earlyTitleBeforeRunCompletion() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let admission = DeferredStubResponse()
        let finished = Mutex(false)
        let published = Mutex(false)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/api/voice/title"):
                return .json(200, #"{"choices":[{"message":{"content":"Healthy drink subscriptions"},"finish_reason":"stop"}]}"#)
            case ("POST", "/v1/runs"): return .deferred(admission)
            case ("GET", "/v1/runs/title-run"):
                return finished.withLock { $0 }
                    ? .json(200, #"{"run_id":"title-run","status":"completed","session_id":"shared","output":"Completed research"}"#)
                    : .json(200, #"{"run_id":"title-run","status":"running","session_id":"shared"}"#)
            case ("GET", "/api/sessions/shared/messages"):
                return .json(200, #"{"session_id":"shared","data":[]}"#)
            case ("PATCH", "/api/sessions/shared"):
                published.withLock { $0 = true }
                return .json(200, #"{"session":{"id":"shared","title":"Healthy drink subscriptions"}}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let model = AppModel(store: store, client: server.client())
        model.sendText("Find David Beckham's healthy drink delivery subscription and product links.")
        let chatID = try #require(model.selectedChatID)
        let early = await eventually { store.chat(id: chatID)?.titleGenerated == true }
        #expect(early)
        #expect(store.chat(id: chatID)?.sessionID == nil)
        #expect(store.chat(id: chatID)?.title == "Healthy drink subscriptions")
        #expect(store.chat(id: chatID)?.messages.contains { $0.role == .assistant } == false)
        admission.resolve(.json(202, #"{"run_id":"title-run","status":"started"}"#))
        let beforeCompletion = await eventually { published.withLock { $0 } }
        #expect(beforeCompletion)
        #expect(store.chat(id: chatID)?.messages.first?.stage == .running)
        finished.withLock { $0 = true }
        try #require(await eventually { store.chat(id: chatID)?.messages.last?.text == "Completed research" })
        #expect(store.chat(id: chatID)?.titleNeedsPublishing != true)
        #expect(ChatStore(directory: directory).chat(id: chatID)?.title == "Healthy drink subscriptions")
        #expect(server.requests.filter { $0.path == "/api/voice/title" }.count == 1)
    }

    @Test("Delayed title generation updates its original chat after selection changes")
    func titleKeepsOwningChat() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let title = DeferredStubResponse()
        let finished = Mutex(false)
        let published = Mutex(false)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/api/voice/title"): return .deferred(title)
            case ("POST", "/v1/runs"):
                return .json(202, #"{"run_id":"delayed-title-run","status":"started"}"#)
            case ("GET", "/v1/runs/delayed-title-run"):
                return finished.withLock { $0 }
                    ? .json(200, #"{"run_id":"delayed-title-run","status":"completed","session_id":"shared","output":"Finished"}"#)
                    : .json(200, #"{"run_id":"delayed-title-run","status":"running","session_id":"shared"}"#)
            case ("GET", "/api/sessions/shared/messages"):
                return .json(200, #"{"session_id":"tip","data":[]}"#)
            case ("PATCH", "/api/sessions/tip"):
                published.withLock { $0 = true }
                return .json(200, #"{"session":{"id":"tip","title":"Monthly expense tracking"}}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let model = AppModel(store: store, client: server.client())
        model.sendText("Design an Excel tracker for monthly household expenses and budget categories.")
        let originalID = try #require(model.selectedChatID)
        try #require(await eventually { store.chat(id: originalID)?.sessionID == "shared" })
        model.newChat()
        let emptyID = try #require(model.selectedChatID)
        title.resolve(.json(200, #"{"choices":[{"message":{"content":"Monthly expense tracking"},"finish_reason":"stop"}]}"#))
        try #require(await eventually { published.withLock { $0 } })
        #expect(store.chat(id: originalID)?.title == "Monthly expense tracking")
        #expect(store.chat(id: originalID)?.messages.first?.stage == .running)
        #expect(store.chat(id: originalID)?.titleNeedsPublishing != true)
        #expect(store.chat(id: emptyID)?.title == "New chat")
        #expect(store.chat(id: emptyID)?.messages.isEmpty == true)
        #expect(model.selectedChatID == emptyID)
        finished.withLock { $0 = true }
        try #require(await eventually { store.chat(id: originalID)?.messages.last?.text == "Finished" })
        #expect(server.requests.filter { $0.path == "/api/voice/title" }.count == 1)
    }

    @Test("A voice title can appear during classification and remains local for a brain dump")
    func titleAfterTranscription() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let classification = DeferredStubResponse()
        let message = ChatMessage(role: .user, input: .voice, text: "", audioFileName: "input.m4a")
        let server = StubServer { request in
            switch request.path {
            case "/api/audio/transcribe":
                return .json(200, #"{"ok":true,"transcript":"I felt restored after a quiet evening walk."}"#)
            case "/api/voice/classify": return .deferred(classification)
            case "/api/voice/title":
                return .json(200, #"{"choices":[{"message":{"content":"A restorative evening walk"},"finish_reason":"stop"}]}"#)
            case "/api/voice/retain":
                return .json(200, "{\"success\":true,\"bank_id\":\"voice\",\"items_count\":1,\"async\":true,\"operation_id\":\"\(message.id)\"}")
            default: return .json(404, #"{"detail":"Unexpected request"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        try Data([1, 2, 3]).write(to: store.audioURL(fileName: "input.m4a"))
        let chat = Chat(messages: [message])
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        try #require(await eventually { store.chat(id: chat.id)?.titleGenerated == true })
        #expect(store.chat(id: chat.id)?.messages.first?.stage == .classifying)
        classification.resolve(.json(200, #"{"answers":{"intent":{"type":"choice","choice":"brain_dump","confidence":0.99,"probabilities":{"chat":0.001,"brain_dump":0.998,"unsure":0.001}}}}"#))
        try #require(await eventually { store.chat(id: chat.id)?.messages.first?.stage == .completed })
        #expect(store.chat(id: chat.id)?.title == "A restorative evening walk")
        #expect(store.chat(id: chat.id)?.titleNeedsPublishing != true)
        #expect(store.chat(id: chat.id)?.sessionID == nil)
        #expect(server.requests.filter { $0.path == "/api/voice/title" }.count == 1)
        #expect(server.requests.filter { $0.path == "/v1/runs" || $0.method == "PATCH" }.isEmpty)
    }

    @Test("Legacy imports gain notification-safe local IDs without replaying an uncertain turn under a new key")
    func legacyImportedIdentity() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let older = ChatMessage(id: "remote-native-session-8", role: .user, text: "Earlier", stage: .completed)
        let pending = ChatMessage(role: .user, text: "Resume safely", stage: .submitting, audioFileName: "original.m4a",
                                  submission: RunSubmission(input: "Resume safely", sessionID: "native-session"))
        let store = ChatStore(directory: directory)
        try Data([1, 2, 3]).write(to: store.audioURL(fileName: "original.m4a"))
        try store.save(Chat(id: "native-session", sessionID: "native-session", messages: [older, pending], titleGenerated: true))
        let restored = ChatStore(directory: directory)
        let migrated = try #require(restored.chats.first)
        try #require(UUID(uuidString: migrated.id) != nil)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs"):
                guard request.request.value(forHTTPHeaderField: "X-Hermes-Session-Key") == "ios-chat:native-session",
                      request.request.value(forHTTPHeaderField: "Idempotency-Key") == pending.requestKey else {
                    return .json(409, #"{"error":{"message":"Original admission identity changed"}}"#)
                }
                return .json(202, #"{"run_id":"recovered","status":"started"}"#)
            case ("GET", "/v1/runs/recovered"):
                return .json(200, #"{"run_id":"recovered","status":"completed","session_id":"native-session","output":"Recovered"}"#)
            case ("GET", "/api/sessions"):
                return .json(200, #"{"data":[{"id":"native-session"}],"has_more":false}"#)
            case ("GET", "/api/sessions/native-session/messages"):
                return .json(200, #"{"session_id":"native-session","data":[{"id":8,"role":"user","content":"Earlier"},{"id":9,"role":"user","content":"Resume safely"},{"id":10,"role":"assistant","content":"Recovered"}]}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let model = AppModel(store: restored, client: server.client())
        model.scenePhaseChanged(.active)
        try #require(await eventually { restored.chat(id: migrated.id)?.messages.last?.text == "Recovered" })
        await model.refreshChats()
        let saved = try #require(restored.chat(id: migrated.id))
        #expect(saved.messages.map(\.text) == ["Earlier", "Resume safely", "Recovered"])
        #expect(saved.messages.prefix(2).map(\.id) == [older.id, pending.id])
        #expect(saved.messages[1].stage == .completed)
        #expect(try Data(contentsOf: restored.audioURL(fileName: "original.m4a")) == Data([1, 2, 3]))
        #expect(ChatStore(directory: directory).chats.map(\.id) == [migrated.id])
    }

    @Test("Unreadable history is preserved rather than replaced")
    func unreadableHistory() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let index = directory.appending(path: "chats.json")
        let original = Data("interrupted external write".utf8)
        try original.write(to: index)
        let store = ChatStore(directory: directory)
        #expect(throws: (any Error).self) { try store.save(Chat(title: "Must not overwrite")) }
        #expect(try Data(contentsOf: index) == original)
        #expect(store.chats.isEmpty)
    }

    @Test("A failed disk write cannot publish an unsaved message")
    func failedCommit() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        var chat = Chat(title: "Original")
        try store.save(chat)
        let index = directory.appending(path: "chats.json")
        try FileManager.default.removeItem(at: index)
        try FileManager.default.createDirectory(at: index, withIntermediateDirectories: false)
        chat.messages.append(ChatMessage(role: .user, text: "Do not send unsaved work"))
        #expect(throws: (any Error).self) { try store.save(chat) }
        #expect(store.chat(id: chat.id)?.messages.isEmpty == true)
    }

    @Test("Voice replies synthesize automatically; text replies synthesize only when played", arguments: [MessageInput.text, .voice])
    func speechPolicy(input: MessageInput) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let audio = Self.wave.base64EncodedString()
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/api/audio/transcribe"):
                return .json(200, #"{"ok":true,"transcript":"What is seven plus five?"}"#)
            case ("POST", "/api/voice/classify"):
                return .json(200, #"{"model":"typesafe/jev-1.13","answers":{"intent":{"type":"choice","choice":"chat","confidence":0.99,"probabilities":{"chat":0.995,"brain_dump":0.005,"unsure":0}}}}"#)
            case ("POST", "/api/voice/title"):
                return .json(200, #"{"choices":[{"message":{"content":"Simple addition"},"finish_reason":"stop"}]}"#)
            case ("POST", "/v1/runs"):
                return .json(202, #"{"run_id":"run-policy","status":"queued"}"#)
            case ("GET", "/v1/runs/run-policy"):
                return .json(200, #"{"run_id":"run-policy","status":"completed","session_id":"session-policy","output":"Twelve."}"#)
            case ("GET", "/api/sessions/session-policy/messages"):
                return .json(200, #"{"session_id":"session-policy","data":[]}"#)
            case ("PATCH", "/api/sessions/session-policy"):
                return .json(200, #"{"session":{"id":"session-policy","title":"Simple addition"}}"#)
            case ("POST", "/api/audio/speak"):
                return .json(200, "{\"ok\":true,\"data_url\":\"data:audio/wav;base64,\(audio)\",\"mime_type\":\"audio/wav\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let voiceFile = input == .voice ? "input.m4a" : nil
        if let voiceFile { try Data([1]).write(to: store.audioURL(fileName: voiceFile)) }
        let chat = Chat(messages: [ChatMessage(role: .user, input: input, text: input == .voice ? "" : "What is seven plus five?", audioFileName: voiceFile)])
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.selectedChatID = nil // Do not play into the test host's shared audio session.
        model.scenePhaseChanged(.active)
        #expect(await eventually { store.chat(id: chat.id)?.messages.last?.role == .assistant })
        let reply = try #require(store.chat(id: chat.id)?.messages.last)
        #expect(reply.text == "Twelve.")
        #expect(store.chat(id: chat.id)?.sessionID == "session-policy")
        #expect(store.chat(id: chat.id)?.messages.first?.stage == .completed)
        #expect(await eventually { store.chat(id: chat.id)?.titleGenerated == true && store.chat(id: chat.id)?.titleNeedsPublishing != true })
        #expect(store.chat(id: chat.id)?.title == "Simple addition")
        if input == .voice {
            #expect(await eventually { store.chat(id: chat.id)?.messages.last?.audioFileName != nil })
            #expect(server.requests.filter { $0.path == "/api/audio/speak" }.count == 1)
            #expect(store.chat(id: chat.id)?.messages.first?.text == "What is seven plus five?")
        } else {
            #expect(server.requests.filter { $0.path == "/api/audio/speak" }.isEmpty)
            await model.play(reply)
            #expect(server.requests.filter { $0.path == "/api/audio/speak" }.count == 1)
        }
        await model.play(try #require(store.chat(id: chat.id)?.messages.last))
        #expect(server.requests.filter { $0.path == "/api/audio/speak" }.count == 1) // Cached audio is reused.
    }

    @Test("Resuming an accepted run never submits a second agent turn")
    func resumeAcceptedRun() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = StubServer { request in
            if request.path == "/v1/runs/already-accepted" {
                return .json(200, #"{"run_id":"already-accepted","status":"completed","session_id":"existing-chat","output":"Finished while you were away."}"#)
            }
            return .json(404, #"{"detail":"Not found"}"#)
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(sessionID: "existing-chat", messages: [ChatMessage(role: .user, text: "Continue", stage: .running, runID: "already-accepted")])
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        #expect(await eventually { store.chat(id: chat.id)?.messages.last?.text == "Finished while you were away." })
        #expect(server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }.isEmpty)
        #expect(store.chat(id: chat.id)?.messages.filter { $0.role == .assistant }.count == 1)
    }

    @Test("A brain dump is saved verbatim without starting an agent or synthesizing a reply")
    func brainDumpDoesNotRunAgent() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let text = "Today I felt exhausted but the walk helped. Just a thought for my diary."
        let message = ChatMessage(role: .user, input: .voice, text: text, classification: .brainDump)
        let server = StubServer { request in
            switch request.path {
            case "/api/voice/retain":
                return .json(200, "{\"success\":true,\"bank_id\":\"voice\",\"items_count\":1,\"async\":true,\"operation_id\":\"\(message.id)\"}")
            case "/api/voice/title":
                return .json(200, #"{"choices":[{"message":{"content":"A restorative evening walk"},"finish_reason":"stop"}]}"#)
            default: return .json(404, #"{"detail":"Unexpected request"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [message])
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        #expect(await eventually { store.chat(id: chat.id)?.titleGenerated == true })
        let saved = try #require(store.chat(id: chat.id))
        #expect(saved.title == "A restorative evening walk")
        #expect(saved.messages.count == 1)
        #expect(saved.messages[0].text == text)
        #expect(saved.messages[0].stage == .completed)
        #expect(saved.messages[0].isBrainDump)
        #expect(saved.messages[0].runID == nil)
        #expect(server.requests.allSatisfy { ["/api/voice/retain", "/api/voice/title"].contains($0.path) })
        let retained = try #require(server.requests.first { $0.path == "/api/voice/retain" })
        let body = try #require(JSONSerialization.jsonObject(with: retained.body) as? [String: Any])
        let items = try #require(body["items"] as? [[String: Any]])
        #expect(items.first?["content"] as? String == text)
    }

    @Test("HTML replies speak visible prose instead of markup or executable content")
    func htmlSpeech() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let audio = Self.wave.base64EncodedString()
        let server = StubServer { request in
            guard request.path == "/api/audio/speak" else { return .json(404, #"{"detail":"Not found"}"#) }
            return .json(200, "{\"ok\":true,\"data_url\":\"data:audio/wav;base64,\(audio)\",\"mime_type\":\"audio/wav\"}")
        }
        let reply = ChatMessage(role: .assistant, text: """
        <article><h2>Today</h2><p>Review &amp; send.</p>
        <style>body { color: red }</style><script>window.secret = 'hidden script';</script>
        </article>
        """, stage: .completed)
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.selectedChatID = nil
        await model.play(reply)
        let request = try #require(server.requests.first { $0.path == "/api/audio/speak" })
        let body = try #require(JSONSerialization.jsonObject(with: request.body) as? [String: Any])
        let text = try #require(body["text"] as? String)
        #expect(text.split(whereSeparator: \.isNewline).map(String.init) == ["Today", "Review & send."])
        #expect(store.chat(id: chat.id)?.messages.last?.text == reply.text)
        #expect(store.chat(id: chat.id)?.messages.last?.audioFileName != nil)
    }

    @Test("Pre-HTML uncertain admissions retain their original request fingerprint after relaunch")
    func legacyFrozenAdmission() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let frozen = try JSONDecoder().decode(RunSubmission.self, from: Data(#"{"input":"Old request","sessionID":"old-session"}"#.utf8))
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs"):
                let body = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any]
                guard body?["instructions"] == nil else {
                    return .json(409, #"{"error":{"code":"idempotency_conflict","message":"The original admission had no instructions"}}"#)
                }
                return .json(202, #"{"run_id":"legacy-replayed","status":"completed","replayed":true}"#)
            case ("GET", "/v1/runs/legacy-replayed"):
                return .json(200, #"{"run_id":"legacy-replayed","status":"completed","output":"Original result","session_id":"old-session"}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let message = ChatMessage(role: .user, text: "Old request", stage: .submitting, submission: frozen)
        let chat = Chat(sessionID: "old-session", messages: [message], titleGenerated: true)
        try store.save(chat)
        let restored = ChatStore(directory: directory)
        let model = AppModel(store: restored, client: server.client())
        model.scenePhaseChanged(.active)
        #expect(await eventually { restored.chat(id: chat.id)?.messages.first?.stage == .completed })
        #expect(restored.chat(id: chat.id)?.messages.last?.text == "Original result")
        #expect(server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }.count == 1)
    }

    // A short valid PCM WAV: API parsing and on-disk caching exercise real audio bytes.
    private static var wave: Data {
        var data = Data("RIFF".utf8)
        func append<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
        }
        append(UInt32(36 + 320)); data.append(Data("WAVEfmt ".utf8))
        append(UInt32(16)); append(UInt16(1)); append(UInt16(1))
        append(UInt32(16_000)); append(UInt32(32_000)); append(UInt16(2)); append(UInt16(16))
        data.append(Data("data".utf8)); append(UInt32(320)); data.append(Data(repeating: 0, count: 320))
        return data
    }
}
