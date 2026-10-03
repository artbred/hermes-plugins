import Foundation
import Testing
@testable import HermesVoice

@MainActor
@Suite("Reply notification routing")
struct ReplyNotificationTests {
    @Test("An alert opens only the local chat owning its run")
    func routesToOwningChat() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        let a = Chat(title: "A", messages: [ChatMessage(role: .assistant, text: "Reply A", stage: .completed, runID: "run-a")], titleGenerated: true)
        let b = Chat(title: "B", messages: [ChatMessage(role: .assistant, text: "Reply B", stage: .completed, runID: "run-b")], titleGenerated: true)
        try store.save(a)
        try store.save(b)
        let server = StubServer { _ in .json(404, #"{"detail":"Not found"}"#) }
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.selectChat(b.id)
        let mismatched = try #require(ReplyNotification(userInfo: ["kind": "hermes_reply", "chat_id": b.id, "run_id": "run-a"]))
        model.receivedReplyNotification(mismatched, openChat: true)
        #expect(model.selectedChatID == b.id)
        let valid = try #require(ReplyNotification(userInfo: ["kind": "hermes_reply", "chat_id": a.id, "run_id": "run-a"]))
        model.isSettingsPresented = true
        model.receivedReplyNotification(valid, openChat: true)
        #expect(model.selectedChatID == a.id)
        #expect(!model.isSettingsPresented)
        #expect(store.chat(id: a.id)?.messages.last?.text == "Reply A")
    }

    @Test("A foreground push cannot switch the user's current chat")
    func foregroundDoesNotNavigate() throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        let a = Chat(messages: [ChatMessage(role: .assistant, text: "Reply A", stage: .completed, runID: "run-a")], titleGenerated: true)
        let b = Chat(titleGenerated: true)
        try store.save(a)
        try store.save(b)
        let server = StubServer { _ in .json(404, #"{"detail":"Not found"}"#) }
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.selectChat(b.id)
        let notification = try #require(ReplyNotification(userInfo: ["kind": "hermes_reply", "chat_id": a.id, "run_id": "run-a"]))
        model.receivedReplyNotification(notification, openChat: false)
        #expect(model.selectedChatID == b.id)
    }

    @Test("An inactive notification tap opens a restored chat and recovers its reply", arguments: [false, true])
    func routesAfterRelaunch(receiptWasPersisted: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = ChatStore(directory: directory)
        let submitted = ChatMessage(
            role: .user, text: "Original request", stage: receiptWasPersisted ? .running : .submitting,
            runID: receiptWasPersisted ? "restored-run" : nil,
            submission: RunSubmission(input: "Original request", sessionID: nil, instructions: MobileResponseFormat.instructions),
            replyNotificationRequested: true
        )
        let owningChat = Chat(title: "Waiting for reply", messages: [submitted], titleGenerated: true)
        let otherChat = Chat(title: "Other chat", titleGenerated: true)
        try store.save(owningChat)
        try store.save(otherChat)
        let restored = ChatStore(directory: directory)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs"):
                return .json(202, #"{"run_id":"restored-run","status":"running"}"#)
            case ("GET", "/v1/runs/restored-run"):
                return .json(200, #"{"run_id":"restored-run","status":"completed","output":"Reply completed while away"}"#)
            default:
                return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let model = AppModel(store: restored, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.inactive)
        model.selectedChatID = otherChat.id
        model.isSettingsPresented = true
        model.isChatsPresented = true
        let notification = try #require(ReplyNotification(userInfo: [
            "kind": "hermes_reply", "chat_id": owningChat.id, "run_id": "restored-run"
        ]))

        model.receivedReplyNotification(notification, openChat: true)

        #expect(model.selectedChatID == owningChat.id)
        #expect(!model.isSettingsPresented)
        #expect(!model.isChatsPresented)
        let recovered = await eventually {
            restored.chat(id: owningChat.id)?.messages.last?.text == "Reply completed while away"
        }
        #expect(recovered)
        #expect(restored.chat(id: otherChat.id)?.messages.isEmpty == true)
        let admissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }
        #expect(admissions.count == (receiptWasPersisted ? 0 : 1))
        if !receiptWasPersisted {
            #expect(admissions.first?.request.value(forHTTPHeaderField: "Idempotency-Key") == submitted.requestKey)
        }
    }
}
