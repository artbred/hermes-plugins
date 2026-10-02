import Foundation
import Synchronization
import Testing
@testable import HermesVoice

@MainActor
@Suite("Independent chats")
struct ChatIsolationTests {
    @Test("An attachment import in A cannot prevent sending in B", arguments: [false, true])
    func importDoesNotBlockAnotherChat(fails: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appending(path: "coordinated.txt")
        try Data("Attachment belonging to A".utf8).write(to: source)
        let gate = CoordinationGate()
        DispatchQueue.global().async {
            var error: NSError?
            NSFileCoordinator().coordinate(writingItemAt: source, options: [], error: &error) { _ in
                gate.holding.withLock { $0 = true }
                _ = gate.release.wait(timeout: .now() + 15)
            }
        }
        defer { gate.release.signal() }
        let coordinated = await eventually { gate.holding.withLock { $0 } }
        try #require(coordinated)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs"):
                return .json(202, #"{"run_id":"run-b","status":"running"}"#)
            case ("GET", "/v1/runs/run-b"):
                return .json(200, #"{"run_id":"run-b","status":"completed","session_id":"session-b","output":"B completed independently"}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory.appending(path: "store"))
        let a = Chat(title: "A", titleGenerated: true)
        let b = Chat(title: "B", titleGenerated: true)
        try store.save(a)
        try store.save(b)
        let model = AppModel(store: store, client: server.client())
        model.selectChat(a.id)
        let importing = Task { await model.importAttachments([source]) }
        let started = await eventually { model.isImportingAttachments }
        try #require(started)
        model.selectChat(b.id)
        model.draft = "Run B while A is importing"
        let canSendB = model.canSend
        let bShowsImport = model.isImportingAttachments
        #expect(canSendB)
        #expect(!bShowsImport)
        model.sendText(model.draft)
        let bFinished = await eventually { store.chat(id: b.id)?.messages.last?.text == "B completed independently" }
        #expect(bFinished)
        if fails { try FileManager.default.removeItem(at: source) }
        gate.release.signal()
        await importing.value
        let savedA = try #require(store.chat(id: a.id))
        let savedB = try #require(store.chat(id: b.id))
        let bImportError = model.attachmentImportError
        let bAlert = model.alert?.message
        #expect(bImportError == nil)
        #expect(bAlert == nil)
        if fails {
            model.selectChat(a.id)
            let aImportError = model.attachmentImportError
            #expect(aImportError != nil)
            #expect(savedA.draftAttachments?.isEmpty != false)
        } else {
            #expect(savedA.draftAttachments?.first?.name == "coordinated.txt")
        }
        #expect(savedB.draftAttachments?.isEmpty != false)
    }

    @Test("History refresh cannot duplicate or resurrect a chat claimed during download", arguments: [false, true])
    func historyImportRechecksOwnership(deleted: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let history = DeferredStubResponse()
        let server = StubServer { request in
            switch request.path {
            case "/api/sessions":
                return .json(200, #"{"data":[{"id":"remote-a","title":"Remote A"}],"has_more":false}"#)
            case "/api/sessions/remote-a/messages":
                return .deferred(history)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        var local = Chat(title: "Local A", messages: [ChatMessage(role: .user, text: "Original local message", stage: .completed)], titleGenerated: true)
        try store.save(local)
        let model = AppModel(store: store, client: server.client())
        let refresh = Task { await model.refreshChats() }
        let downloading = await eventually { server.requests.contains { $0.path == "/api/sessions/remote-a/messages" } }
        try #require(downloading)
        local.sessionID = "remote-a"
        try store.save(local)
        if deleted { model.deleteChat(local.id) }
        history.resolve(.json(200, #"{"data":[{"id":"1","role":"user","content":"Remote copy"}]}"#))
        await refresh.value
        let owners = store.chats.filter { $0.sessionID == "remote-a" }
        #expect(owners.map(\.id) == (deleted ? [] : [local.id]))
        if !deleted { #expect(owners.first?.messages.first?.text == "Original local message") }
    }

    @Test("Concurrent runs finish independently and Stop targets only the selected chat")
    func independentRunsAndStop() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let state = Mutex((stopA: false, finishB: false))
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs/a/stop"):
                state.withLock { $0.stopA = true }
                return .json(200, #"{"run_id":"a","status":"stopping"}"#)
            case ("GET", "/v1/runs/a"):
                let status = state.withLock { $0.stopA } ? "cancelled" : "running"
                return .json(200, "{\"run_id\":\"a\",\"status\":\"\(status)\",\"session_id\":\"session-a\"}")
            case ("GET", "/v1/runs/b"):
                let status = state.withLock { $0.finishB } ? "completed" : "running"
                return .json(200, "{\"run_id\":\"b\",\"status\":\"\(status)\",\"session_id\":\"session-b\",\"output\":\"B alone\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let a = Chat(title: "A", messages: [ChatMessage(role: .user, text: "A work", stage: .running, runID: "a")], titleGenerated: true)
        let b = Chat(title: "B", messages: [ChatMessage(role: .user, text: "B work", stage: .running, runID: "b")], titleGenerated: true)
        try store.save(a)
        try store.save(b)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        let bothActive = await eventually {
            server.requests.contains { $0.path == "/v1/runs/a" } && server.requests.contains { $0.path == "/v1/runs/b" }
        }
        try #require(bothActive)
        model.selectChat(a.id)
        model.stopRun()
        let stoppedA = await eventually { store.chat(id: a.id)?.messages.first?.stage == .interrupted }
        try #require(stoppedA)
        #expect(store.chat(id: b.id)?.messages.first?.stage == .running)
        #expect(!server.requests.contains { $0.path == "/v1/runs/b/stop" })
        state.withLock { $0.finishB = true }
        let completedB = await eventually { store.chat(id: b.id)?.messages.last?.text == "B alone" }
        try #require(completedB)
        #expect(store.chat(id: a.id)?.messages.contains { $0.role == .assistant } == false)
        #expect(store.chat(id: b.id)?.sessionID == "session-b")
    }

    @Test("An approval action keeps its captured chat after selection changes")
    func approvalKeepsRequestIdentity() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let approvedA = Mutex(false)
        let finishB = Mutex(false)
        let server = StubServer { request in
            if request.method == "POST", request.path == "/v1/runs/a/approval" {
                approvedA.withLock { $0 = true }
                return .json(200, #"{"ok":true}"#)
            }
            for id in ["a", "b"] where request.path == "/v1/runs/\(id)" {
                if id == "a", approvedA.withLock({ $0 }) {
                    return .json(200, #"{"run_id":"a","status":"completed","output":"Approved A"}"#)
                }
                if id == "b", finishB.withLock({ $0 }) {
                    return .json(200, #"{"run_id":"b","status":"cancelled"}"#)
                }
                return .json(200, "{\"run_id\":\"\(id)\",\"status\":\"running\",\"approval\":{\"event\":\"approval.request\",\"request_id\":\"same-local-request-id\",\"command\":\"command-\(id)\",\"choices\":[\"once\",\"deny\"]}}")
            }
            return .json(404, #"{"detail":"Not found"}"#)
        }
        let store = ChatStore(directory: directory)
        let a = Chat(messages: [ChatMessage(role: .user, text: "A", stage: .running, runID: "a")], titleGenerated: true)
        let b = Chat(messages: [ChatMessage(role: .user, text: "B", stage: .running, runID: "b")], titleGenerated: true)
        try store.save(a)
        try store.save(b)
        let model = AppModel(store: store, client: server.client())
        model.scenePhaseChanged(.active)
        let bothWaiting = await eventually { model.pendingApproval(in: a.id) != nil && model.pendingApproval(in: b.id) != nil }
        try #require(bothWaiting)
        let pendingA = model.pendingApproval(in: a.id)
        let captured = try #require(pendingA)
        model.selectChat(b.id)
        await model.respondToApproval("once", approval: captured)
        let doneA = await eventually { store.chat(id: a.id)?.messages.last?.text == "Approved A" }
        try #require(doneA)
        let pendingB = model.pendingApproval(in: b.id)?.command
        #expect(pendingB == "command-b")
        await model.respondToApproval("deny", approval: captured)
        #expect(server.requests.filter { $0.path.hasSuffix("/approval") }.map(\.path) == ["/v1/runs/a/approval"])
        finishB.withLock { $0 = true }
        let endedB = await eventually { store.chat(id: b.id)?.messages.first?.runWasTerminal == true }
        #expect(endedB)
    }

    @Test("A fresh admitted run is not imported as another chat before its first status")
    func admittedSessionHasOneOwner() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let status = DeferredStubResponse()
        let server = StubServer { request in
            switch request.path {
            case "/api/sessions":
                return .json(200, #"{"data":[{"id":"fresh-run","title":"New native session"}],"has_more":false}"#)
            case "/api/sessions/fresh-run/messages":
                return .json(200, #"{"data":[{"id":"1","role":"user","content":"Pending local turn"}]}"#)
            case "/v1/runs/fresh-run":
                return .deferred(status)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let local = Chat(messages: [ChatMessage(role: .user, text: "Pending local turn", stage: .running, runID: "fresh-run")], titleGenerated: true)
        try store.save(local)
        let model = AppModel(store: store, client: server.client())
        await model.refreshChats()
        #expect(store.chats.map(\.id) == [local.id])
        status.resolve(.json(200, #"{"run_id":"fresh-run","status":"completed","session_id":"fresh-run","output":"Finished original chat"}"#))
        let finished = await eventually { store.chat(id: local.id)?.messages.last?.text == "Finished original chat" }
        #expect(finished)
    }
}

private final class CoordinationGate: Sendable {
    let release = DispatchSemaphore(value: 0)
    let holding = Mutex(false)
}
