import Foundation
import Synchronization
import Testing
@testable import HermesVoice

@MainActor
@Suite("File attachments")
struct AttachmentTests {
    @Test("Imported files remain available after the provider removes its copy and the app restarts")
    func durableDraft() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appending(path: "отчёт 2026.txt")
        let bytes = Data("A file provider may revoke access after the picker closes.".utf8)
        try bytes.write(to: source)
        let store = ChatStore(directory: directory.appending(path: "Chats"))
        let model = AppModel(store: store)
        await model.importAttachments([source])
        let file = try #require(model.pendingAttachments.first)
        let chatID = try #require(model.selectedChatID)
        try FileManager.default.removeItem(at: source)
        let restored = ChatStore(directory: store.directory)
        #expect(restored.chat(id: chatID)?.draftAttachments == [file])
        #expect(file.name == "отчёт 2026.txt")
        #expect(try Data(contentsOf: restored.attachmentURL(fileName: file.localFileName)) == bytes)
        #expect(model.canSend)
        model.removeAttachment(file.id)
        #expect(model.pendingAttachments.isEmpty)
        #expect(!FileManager.default.fileExists(atPath: model.attachmentURL(file).path))
    }

    @Test("An unavailable attachment blocks the agent turn instead of silently dropping the file")
    func uploadFailureDoesNotRunAgent() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = StubServer { _ in .json(503, #"{"detail":"Upload service unavailable"}"#) }
        let store = ChatStore(directory: directory)
        let file = try makeFile(store)
        let message = ChatMessage(role: .user, text: "Read the attachment", attachments: [file])
        let chat = Chat(messages: [message], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        #expect(await eventually { store.chat(id: chat.id)?.messages[0].stage == .failed })
        #expect(store.chat(id: chat.id)?.messages[0].files == [file])
        #expect(store.chat(id: chat.id)?.messages[0].submission == nil)
        #expect(server.requests.allSatisfy { $0.path == "/api/files/upload-stream" })
        #expect(FileManager.default.fileExists(atPath: store.attachmentURL(fileName: file.localFileName).path))
    }

    @Test("Lost admission replies preserve file references and session identity across retries")
    func stableSubmissionAfterTransportFailure() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        let file = try makeFile(store)
        let response = uploadReceipt(file)
        let attempts = Mutex(0)
        let server = StubServer { request in
            if request.path == "/api/files/upload-stream" { return .json(200, response) }
            if request.path == "/v1/runs", request.method == "POST" {
                let attempt = attempts.withLock { $0 += 1; return $0 }
                return attempt == 1 ? .failure(.networkConnectionLost) : .json(202, #"{"run_id":"file-run","status":"started"}"#)
            }
            if request.path == "/v1/runs/file-run" {
                return .json(200, #"{"run_id":"file-run","status":"completed","output":"Read the attached document.","session_id":"continued-session"}"#)
            }
            return .json(404, #"{"detail":"Not found"}"#)
        }
        let chat = Chat(messages: [ChatMessage(role: .user, text: "", attachments: [file])], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        #expect(await eventually { store.chat(id: chat.id)?.messages[0].stage == .failed })
        var modified = try #require(store.chat(id: chat.id))
        let frozen = try #require(modified.messages[0].submission)
        modified.sessionID = "newer-session-tip"
        try store.save(modified)
        model.retry(modified.messages[0])
        #expect(await eventually { store.chat(id: chat.id)?.messages[0].stage == .completed })
        #expect(store.chat(id: chat.id)?.messages[0].submission == frozen)
        #expect(server.requests.filter { $0.path == "/api/files/upload-stream" }.count == 1)
        let runs = server.requests.filter { $0.path == "/v1/runs" && $0.method == "POST" }
        #expect(runs.count == 2)
        for request in runs {
            let body = try #require(JSONSerialization.jsonObject(with: request.body) as? [String: Any])
            #expect(body["input"] as? String == frozen.input)
            #expect(body["session_id"] == nil)
        }
        #expect(runs.first?.request.value(forHTTPHeaderField: "Idempotency-Key") == runs.last?.request.value(forHTTPHeaderField: "Idempotency-Key"))
    }

    @Test("Removing a chat removes its message files and unsent draft files")
    func removesOwnedFiles() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        let sent = try makeFile(store)
        let draft = try makeFile(store)
        let chat = Chat(messages: [ChatMessage(role: .user, text: "Document", stage: .completed, attachments: [sent])], draftAttachments: [draft])
        try store.save(chat)
        try store.remove(id: chat.id)
        #expect(!FileManager.default.fileExists(atPath: store.attachmentURL(fileName: sent.localFileName).path))
        #expect(!FileManager.default.fileExists(atPath: store.attachmentURL(fileName: draft.localFileName).path))
    }

    private func makeFile(_ store: ChatStore) throws -> ChatAttachment {
        let id = UUID().uuidString.lowercased()
        let data = Data("Reference number: HERMES-FILE-47".utf8)
        let file = ChatAttachment(id: id, name: "reference note.txt", contentType: "text/plain", byteCount: Int64(data.count), localFileName: id + ".txt")
        try data.write(to: store.attachmentURL(fileName: file.localFileName))
        return file
    }

    private func uploadReceipt(_ file: ChatAttachment) -> String {
        let path = "/srv/attachments/uploads/ios/\(file.id).txt"
        return "{\"ok\":true,\"path\":\"\(path)\",\"entry\":{\"path\":\"\(path)\",\"size\":\(file.byteCount),\"is_directory\":false},\"root\":\"/srv/attachments\",\"locked_root\":\"/srv/attachments\",\"can_change_path\":false}"
    }
}
