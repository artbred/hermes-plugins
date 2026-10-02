import Foundation
import Testing
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
