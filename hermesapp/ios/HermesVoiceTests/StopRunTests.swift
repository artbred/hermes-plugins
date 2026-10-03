import Foundation
import Synchronization
import Testing
@testable import HermesVoice

@MainActor
@Suite("Stopping native runs")
struct StopRunTests {
    @Test("A stop acknowledgement is not a terminal outcome")
    func waitsForTerminalConfirmation() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        struct State: Sendable {
            var requested = false
            var finish = false
            var pollsAfterStop = 0
            var stops = 0
        }
        let state = Mutex(State())
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs/stop-confirmation/stop"):
                state.withLock { $0.requested = true; $0.stops += 1 }
                return .json(200, #"{"run_id":"stop-confirmation","status":"stopping"}"#)
            case ("GET", "/v1/runs/stop-confirmation"):
                let status = state.withLock { value in
                    if value.requested { value.pollsAfterStop += 1 }
                    return value.finish ? "interrupted" : value.requested ? "stopping" : "running"
                }
                return .json(200, "{\"run_id\":\"stop-confirmation\",\"status\":\"\(status)\",\"output\":\"A partial reply\"}")
            default:
                return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [ChatMessage(role: .user, input: .voice, text: "Wait for cancellation", stage: .running, runID: "stop-confirmation")], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let polling = await eventually { server.requests.contains { $0.path == "/v1/runs/stop-confirmation" } }
        try #require(polling)
        model.stopRun()
        model.stopRun()
        let requested = await eventually { state.withLock { $0.requested } }
        try #require(requested)
        let polledAfterStop = await eventually { state.withLock { $0.pollsAfterStop > 0 } }
        try #require(polledAfterStop)
        let busy = model.isBusy
        let stopping = model.isStopping
        let terminalBeforeConfirmation = store.chat(id: chat.id)?.messages[0].runWasTerminal
        let stopCount = state.withLock { $0.stops }
        #expect(busy, "Keep the run active until Hermes confirms it is terminal")
        #expect(stopping)
        #expect(terminalBeforeConfirmation == false)
        #expect(stopCount == 1)
        state.withLock { $0.finish = true }
        let interrupted = await eventually { store.chat(id: chat.id)?.messages[0].stage == .interrupted }
        try #require(interrupted)
        let messages = try #require(store.chat(id: chat.id)?.messages)
        #expect(messages[0].runWasTerminal)
        #expect(messages[0].stopRequested == true)
        #expect(messages.last?.text == "A partial reply")
        #expect(messages.last?.needsSpeech == false)
        let speechRequests = server.requests.filter { $0.path == "/api/audio/speak" }.count
        #expect(speechRequests == 0)
    }

    @Test("Stopping during admission waits for the same request's run ID")
    func stopsAfterAdmission() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let admission = DeferredStubResponse()
        let stopped = Mutex(false)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs"):
                return .deferred(admission)
            case ("POST", "/v1/runs/admitted/stop"):
                stopped.withLock { $0 = true }
                return .json(200, #"{"run_id":"admitted","status":"stopping"}"#)
            case ("GET", "/v1/runs/admitted"):
                let status = stopped.withLock { $0 } ? "interrupted" : "running"
                return .json(200, "{\"run_id\":\"admitted\",\"status\":\"\(status)\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let message = ChatMessage(role: .user, text: "One turn only")
        let chat = Chat(messages: [message], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let submitting = await eventually { server.requests.contains { $0.path == "/v1/runs" } }
        try #require(submitting)
        model.stopRun()
        model.stopRun()
        let waiting = try #require(store.chat(id: chat.id)?.messages.first)
        let busy = model.isBusy
        let stopping = model.isStopping
        #expect(waiting.stage == .submitting)
        #expect(waiting.stopRequested == true)
        #expect(waiting.runID == nil)
        #expect(!waiting.runWasTerminal)
        #expect(busy && stopping)
        admission.resolve(.json(202, #"{"run_id":"admitted","status":"queued"}"#))
        let interrupted = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .interrupted }
        try #require(interrupted)
        let submissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }
        #expect(submissions.count == 1)
        #expect(submissions.first?.request.value(forHTTPHeaderField: "Idempotency-Key") == message.requestKey)
        let stops = server.requests.filter { $0.path == "/v1/runs/admitted/stop" }.count
        #expect(stops == 1)
        let final = try #require(store.chat(id: chat.id)?.messages.first)
        #expect(final.attempt == 0)
        #expect(final.runID == "admitted")
        #expect(final.runWasTerminal)
    }

    @Test("Completion racing a stop keeps the completed reply", arguments: [500, 404])
    func completionWinsRace(stopStatus: Int) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let acknowledgement = DeferredStubResponse()
        let stopping = Mutex(false)
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs/completion-race/stop"):
                stopping.withLock { $0 = true }
                return .deferred(acknowledgement)
            case ("GET", "/v1/runs/completion-race"):
                if stopping.withLock({ $0 }) {
                    return .json(200, #"{"run_id":"completion-race","status":"completed","output":"The completed answer"}"#)
                }
                return .json(200, #"{"run_id":"completion-race","status":"running"}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [ChatMessage(role: .user, text: "Finish normally", stage: .running, runID: "completion-race")], titleGenerated: true)
        try store.save(chat)
        let client = server.client()
        let model = AppModel(store: store, client: client, initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        model.stopRun()
        let completed = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .completed }
        try #require(completed)
        acknowledgement.resolve(.json(stopStatus, #"{"error":{"code":"run_not_found","message":"Late stop failure"}}"#))
        _ = try await client.run(id: "completion-race")
        let saved = try #require(store.chat(id: chat.id))
        let busy = model.isBusy
        let alertMessage = model.alert?.message
        #expect(saved.messages.first?.stage == .completed)
        #expect(saved.messages.first?.error == nil)
        #expect(saved.messages.first?.stopRequested == nil)
        #expect(saved.messages.last?.text == "The completed answer")
        #expect(!busy)
        #expect(alertMessage == nil)
    }

    @Test("A failed stop stays active and can be retried")
    func stopFailureIsRetryable() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        struct State: Sendable {
            var stops = 0
            var finish = false
        }
        let state = Mutex(State())
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs/retry-stop/stop"):
                let count = state.withLock { $0.stops += 1; return $0.stops }
                return count == 1
                    ? .json(503, #"{"detail":"Stop unavailable"}"#)
                    : .json(200, #"{"run_id":"retry-stop","status":"stopping"}"#)
            case ("GET", "/v1/runs/retry-stop"):
                let status = state.withLock { $0.finish } ? "interrupted" : "running"
                return .json(200, "{\"run_id\":\"retry-stop\",\"status\":\"\(status)\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [ChatMessage(role: .user, text: "Try stopping", stage: .running, runID: "retry-stop")], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        model.stopRun()
        let failed = await eventually { store.chat(id: chat.id)?.messages.first?.error != nil }
        try #require(failed)
        let failure = try #require(store.chat(id: chat.id)?.messages.first)
        let busyAfterFailure = model.isBusy
        let stoppingAfterFailure = model.isStopping
        #expect(failure.stage == .running)
        #expect(!failure.runWasTerminal)
        #expect(failure.stopRequested == true)
        #expect(failure.stopAcknowledged != true)
        #expect(failure.error?.contains("Stop unavailable") == true)
        #expect(busyAfterFailure && !stoppingAfterFailure)
        model.stopRun()
        model.stopRun()
        let acknowledged = await eventually { store.chat(id: chat.id)?.messages.first?.stopAcknowledged == true }
        try #require(acknowledged)
        let count = state.withLock { $0.stops }
        #expect(count == 2)
        state.withLock { $0.finish = true }
        let interrupted = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .interrupted }
        try #require(interrupted)
    }

    @Test("A missing saved run releases the chat for explicit sending, retry, or deletion", arguments: ["send", "retry", "delete"])
    func missingRunStatusReleasesChat(recovery: String) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let status = DeferredStubResponse()
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("GET", "/v1/runs/missing-status"):
                return .deferred(status)
            case ("GET", "/v1/runs/missing-status/events"):
                return .stream(chunks: [Data("data: {\"event\":\"message.delta\",\"delta\":\"Partial reply\"}\n\n".utf8)], finish: false)
            case ("POST", "/v1/runs"):
                return .json(202, #"{"run_id":"explicit-next-run","status":"queued"}"#)
            case ("GET", "/v1/runs/explicit-next-run"):
                return .json(200, #"{"run_id":"explicit-next-run","status":"completed","output":"Explicit next reply"}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let message = ChatMessage(role: .user, text: "Original request", stage: .running, runID: "missing-status",
                                  submission: RunSubmission(input: "Original request", sessionID: "existing-session"))
        let chat = Chat(sessionID: "existing-session", messages: [message], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let connected = await eventually {
            model.liveResponse == "Partial reply" && server.requests.contains { $0.path == "/v1/runs/missing-status" }
        }
        try #require(connected)
        status.resolve(.json(404, #"{"error":{"code":"run_not_found","message":"Unknown run"}}"#))
        let released = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .failed }
        try #require(released)
        let closedStream = await eventually { server.cancellations > 0 }
        #expect(closedStream)
        let failed = try #require(store.chat(id: chat.id)?.messages.first)
        let attemptClosed = failed.runWasTerminal
        let savedRunID = failed.runID
        let busy = model.isBusy
        let liveResponse = model.liveResponse
        let assistantReplies = store.chat(id: chat.id)?.messages.filter { $0.role == .assistant }.map(\.text)
        let automaticSubmissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }.count
        let restored = ChatStore(directory: directory)
        let restoredStage = restored.chat(id: chat.id)?.messages.first?.stage.rawValue
        let restoredAttemptClosed = restored.chat(id: chat.id)?.messages.first?.runWasTerminal
        #expect(attemptClosed)
        #expect(savedRunID == "missing-status")
        #expect(!busy)
        #expect(liveResponse == "")
        #expect(assistantReplies == [])
        #expect(automaticSubmissions == 0)
        #expect(restoredStage == "failed")
        #expect(restoredAttemptClosed == true)
        model.draft = "A new request"
        let canSend = model.canSend
        #expect(canSend)
        if recovery == "delete" {
            model.deleteChat(chat.id)
            let deleted = store.chat(id: chat.id) == nil
            #expect(deleted)
            return
        }
        let retryOriginal = recovery == "retry"
        if retryOriginal { model.retry(failed) }
        else { model.sendText("A new request") }
        let completed = await eventually { store.chat(id: chat.id)?.messages.last?.text == "Explicit next reply" }
        try #require(completed)
        let submissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }
        let submissionCount = submissions.count
        let requestKey = submissions.first?.request.value(forHTTPHeaderField: "Idempotency-Key")
        let body = submissions.first.flatMap { try? JSONDecoder().decode([String: String].self, from: $0.body) }
        let input = body?["input"]
        let sessionID = body?["session_id"]
        let originalAttempt = store.chat(id: chat.id)?.messages.first?.attempt
        let originalStage = store.chat(id: chat.id)?.messages.first?.stage.rawValue
        let originalKey = message.requestKey
        #expect(submissionCount == 1)
        #expect(requestKey != originalKey)
        #expect(input == (retryOriginal ? "Original request" : "A new request"))
        #expect(sessionID == "existing-session")
        #expect(originalAttempt == (retryOriginal ? 1 : 0))
        #expect(originalStage == (retryOriginal ? "completed" : "failed"))
        if retryOriginal {
            let retryKey = "\(message.id)-1"
            #expect(requestKey == retryKey)
        }
    }

    @Test("A missing run during Stop can be deleted without cancelling another chat")
    func missingRunStopReleasesOnlyItsChat() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let stop = DeferredStubResponse()
        let otherStatus = DeferredStubResponse()
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("GET", "/v1/runs/missing-stop"): return .never
            case ("GET", "/v1/runs/missing-stop/events"):
                return .stream(chunks: [Data("data: {\"event\":\"tool.started\",\"tool\":\"terminal\"}\n\n".utf8)], finish: false)
            case ("POST", "/v1/runs/missing-stop/stop"): return .deferred(stop)
            case ("GET", "/v1/runs/unaffected-run"): return .deferred(otherStatus)
            case ("GET", "/v1/runs/unaffected-run/events"):
                return .stream(chunks: [Data("data: {\"event\":\"tool.started\",\"tool\":\"search\"}\n\n".utf8)], finish: false)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let missing = Chat(messages: [ChatMessage(role: .user, text: "Lost status", stage: .running, runID: "missing-stop")], titleGenerated: true)
        let other = Chat(messages: [ChatMessage(role: .user, text: "Keep running", stage: .running, runID: "unaffected-run")], titleGenerated: true)
        try store.save(missing)
        try store.save(other)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.selectChat(other.id)
        let otherConnected = await eventually { model.activeTool == "search" }
        try #require(otherConnected)
        model.selectChat(missing.id)
        let missingConnected = await eventually {
            model.activeTool == "terminal" && server.requests.contains { $0.path == "/v1/runs/missing-stop" }
        }
        try #require(missingConnected)
        model.stopRun()
        let stopRequested = await eventually { server.requests.contains { $0.path == "/v1/runs/missing-stop/stop" } }
        try #require(stopRequested)
        stop.resolve(.json(404, #"{"error":{"code":"run_not_found","message":"Unknown run"}}"#))
        let released = await eventually { store.chat(id: missing.id)?.messages.first?.stage == .failed }
        try #require(released)
        let cancelledRequests = await eventually { server.cancellations >= 2 }
        #expect(cancelledRequests)
        let busy = model.isBusy
        let stopping = model.isStopping
        let activeTool = model.activeTool
        let stopAcknowledged = store.chat(id: missing.id)?.messages.first?.stopAcknowledged
        let restored = ChatStore(directory: directory)
        let savedStage = restored.chat(id: missing.id)?.messages.first?.stage.rawValue
        let savedAttemptClosed = restored.chat(id: missing.id)?.messages.first?.runWasTerminal
        let otherStage = store.chat(id: other.id)?.messages.first?.stage.rawValue
        #expect(!busy && !stopping)
        #expect(activeTool == nil)
        #expect(stopAcknowledged != true)
        #expect(savedStage == "failed")
        #expect(savedAttemptClosed == true)
        #expect(otherStage == "running")
        model.deleteChat(missing.id)
        let deleted = store.chat(id: missing.id) == nil
        #expect(deleted)
        model.selectChat(other.id)
        let otherTool = model.activeTool
        #expect(otherTool == "search")
        otherStatus.resolve(.json(200, #"{"run_id":"unaffected-run","status":"completed","output":"Unaffected reply"}"#))
        let otherCompleted = await eventually { store.chat(id: other.id)?.messages.last?.text == "Unaffected reply" }
        try #require(otherCompleted)
        let automaticSubmissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }.count
        let otherPolls = server.requests.filter { $0.path == "/v1/runs/unaffected-run" }.count
        #expect(automaticSubmissions == 0)
        #expect(otherPolls == 1)
    }

    @Test("Unknown status and stop failures do not terminalize a live run", arguments: [
        ("status", 404), ("stop", 404), ("status", 401), ("stop", 401), ("status", 0), ("stop", 0)
    ])
    func unknownRunErrorsRemainPending(endpoint: String, status: Int) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let finish = Mutex(false)
        let server = StubServer { request in
            let failure: StubResponse = status == 0 ? .failure(.networkConnectionLost)
                : .json(status, #"{"error":{"code":"route_not_found","message":"Cannot find this route"}}"#)
            switch (request.method, request.path) {
            case ("POST", "/v1/runs/uncertain-status/stop"): return failure
            case ("GET", "/v1/runs/uncertain-status"):
                if finish.withLock({ $0 }) {
                    return .json(200, #"{"run_id":"uncertain-status","status":"completed","output":"Recovered actual outcome"}"#)
                }
                return endpoint == "status" ? failure : .json(200, #"{"run_id":"uncertain-status","status":"running"}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [ChatMessage(role: .user, text: "May still be running", stage: .running, runID: "uncertain-status")], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        if endpoint == "stop" { model.stopRun() }
        let failed = await eventually { store.chat(id: chat.id)?.messages.first?.error != nil }
        try #require(failed)
        model.draft = "Do not send over an uncertain run"
        let stage = store.chat(id: chat.id)?.messages.first?.stage.rawValue
        let attemptClosed = store.chat(id: chat.id)?.messages.first?.runWasTerminal
        let busy = model.isBusy
        let canSend = model.canSend
        #expect(stage == "running")
        #expect(attemptClosed == false)
        #expect(busy && !canSend)
        model.deleteChat(chat.id)
        let retained = store.chat(id: chat.id) != nil
        #expect(retained)
        finish.withLock { $0 = true }
        model.scenePhaseChanged(.active)
        let recovered = await eventually { store.chat(id: chat.id)?.messages.last?.text == "Recovered actual outcome" }
        try #require(recovered)
        let submissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }.count
        #expect(submissions == 0)
    }

    @Test("Uncertain admission restores the stop intent and original idempotency key")
    func recoversUncertainAdmission() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let firstAdmission = DeferredStubResponse()
        struct State: Sendable {
            var admissions = 0
            var stopped = false
        }
        let state = Mutex(State())
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/v1/runs"):
                let body = try? JSONDecoder().decode([String: String].self, from: request.body)
                guard body?["input"] == "Recover this exact input", body?["session_id"] == "existing-session" else {
                    return .json(409, #"{"error":{"message":"Idempotency input changed"}}"#)
                }
                let count = state.withLock { $0.admissions += 1; return $0.admissions }
                return count == 1 ? .deferred(firstAdmission) : .json(202, #"{"run_id":"recovered-admission","status":"running"}"#)
            case ("POST", "/v1/runs/recovered-admission/stop"):
                state.withLock { $0.stopped = true }
                return .json(200, #"{"run_id":"recovered-admission","status":"stopping"}"#)
            case ("GET", "/v1/runs/recovered-admission"):
                let status = state.withLock { $0.stopped } ? "interrupted" : "running"
                return .json(200, "{\"run_id\":\"recovered-admission\",\"status\":\"\(status)\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let message = ChatMessage(role: .user, text: "Recover this exact input")
        let chat = Chat(sessionID: "existing-session", messages: [message], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let submitted = await eventually { state.withLock { $0.admissions == 1 } }
        try #require(submitted)
        model.stopRun()
        firstAdmission.resolve(.failure(.networkConnectionLost))
        let failed = await eventually { store.chat(id: chat.id)?.messages.first?.error != nil }
        try #require(failed)
        let beforeRestart = try #require(store.chat(id: chat.id)?.messages.first)
        #expect(beforeRestart.stage == .submitting)
        #expect(beforeRestart.stopRequested == true)
        #expect(!beforeRestart.runWasTerminal)
        let restored = ChatStore(directory: directory)
        var continued = try #require(restored.chat(id: chat.id))
        continued.sessionID = "new-session-tip"
        try restored.save(continued)
        let relaunched = AppModel(store: restored, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        relaunched.scenePhaseChanged(.active)
        let interrupted = await eventually { restored.chat(id: chat.id)?.messages.first?.stage == .interrupted }
        try #require(interrupted)
        let requests = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }
        #expect(requests.count == 2)
        #expect(requests.map { $0.request.value(forHTTPHeaderField: "Idempotency-Key") } == [message.requestKey, message.requestKey])
        let saved = try #require(restored.chat(id: chat.id))
        #expect(saved.messages.filter { $0.role == .user }.count == 1)
        #expect(saved.messages.first?.attempt == 0)
        #expect(saved.messages.first?.runID == "recovered-admission")
    }

    @Test("An acknowledged stop is polled after relaunch without being sent again")
    func resumesAcknowledgedStop() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = StubServer { request in
            if request.method == "GET", request.path == "/v1/runs/already-stopping" {
                return .json(200, #"{"run_id":"already-stopping","status":"cancelled","output":"Preserved partial"}"#)
            }
            return .json(404, #"{"detail":"Unexpected request"}"#)
        }
        let store = ChatStore(directory: directory)
        let message = ChatMessage(role: .user, text: "Already stopping", stage: .running, runID: "already-stopping", stopRequested: true, stopAcknowledged: true)
        let chat = Chat(messages: [message], titleGenerated: true)
        try store.save(chat)
        let restored = ChatStore(directory: directory)
        let model = AppModel(store: restored, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        model.stopRun()
        let interrupted = await eventually { restored.chat(id: chat.id)?.messages.first?.stage == .interrupted }
        try #require(interrupted)
        let mutations = server.requests.filter { $0.method == "POST" }.count
        #expect(mutations == 0)
        let messages = try #require(restored.chat(id: chat.id)?.messages)
        #expect(messages[0].runWasTerminal)
        #expect(messages.last?.text == "Preserved partial")
    }

    @Test("Cancelling local classification cannot later start the old agent turn")
    func cancelsBeforeAdmission() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let classification = DeferredStubResponse()
        let server = StubServer { request in
            switch (request.method, request.path) {
            case ("POST", "/api/voice/classify"): return .deferred(classification)
            case ("POST", "/v1/runs"):
                return .json(202, #"{"run_id":"replacement-turn","status":"queued"}"#)
            case ("GET", "/v1/runs/replacement-turn"):
                return .json(200, #"{"run_id":"replacement-turn","status":"completed","output":"New turn reply"}"#)
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let first = ChatMessage(role: .user, input: .voice, text: "Do not submit this")
        let chat = Chat(messages: [first], titleGenerated: true)
        try store.save(chat)
        let client = server.client()
        let model = AppModel(store: store, client: client, initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let classifying = await eventually { server.requests.contains { $0.path == "/api/voice/classify" } }
        try #require(classifying)
        model.stopRun()
        let stopped = try #require(store.chat(id: chat.id)?.messages.first)
        #expect(stopped.stage == .interrupted)
        #expect(!stopped.runWasTerminal)
        #expect(stopped.runID == nil)
        model.sendText("Submit this instead")
        let completed = await eventually { store.chat(id: chat.id)?.messages.last?.text == "New turn reply" }
        try #require(completed)
        classification.resolve(.json(200, #"{"answers":{"intent":{"type":"choice","choice":"chat","confidence":1,"probabilities":{"chat":1,"brain_dump":0,"unsure":0}}}}"#))
        _ = try await client.run(id: "replacement-turn")
        let submissions = server.requests.filter { $0.method == "POST" && $0.path == "/v1/runs" }
        #expect(submissions.count == 1)
        #expect(submissions.first?.request.value(forHTTPHeaderField: "Idempotency-Key") != first.requestKey)
        let messages = try #require(store.chat(id: chat.id)?.messages)
        #expect(messages.first?.stage == .interrupted)
        #expect(messages.last?.text == "New turn reply")
    }

    @Test("Stopping an in-flight memory write does not falsely mark an accepted thought unsaved")
    func confirmsAcceptedMemory() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let retention = DeferredStubResponse()
        let server = StubServer { request in
            if request.path == "/api/voice/retain" { return .deferred(retention) }
            return .json(404, #"{"detail":"Unexpected request"}"#)
        }
        let store = ChatStore(directory: directory)
        let message = ChatMessage(role: .user, input: .voice, text: "Keep this thought", classification: .brainDump)
        let chat = Chat(messages: [message], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let saving = await eventually { server.requests.contains { $0.path == "/api/voice/retain" } }
        try #require(saving)
        model.stopRun()
        let waiting = try #require(store.chat(id: chat.id)?.messages.first)
        let busy = model.isBusy
        #expect(waiting.stage == .savingMemory)
        #expect(waiting.stopRequested == true)
        #expect(busy)
        retention.resolve(.json(200, "{\"success\":true,\"bank_id\":\"voice\",\"items_count\":1,\"async\":true,\"operation_id\":\"\(message.id)\"}"))
        let completed = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .completed }
        try #require(completed)
        let saved = try #require(store.chat(id: chat.id)?.messages.first)
        #expect(saved.isBrainDump)
        #expect(saved.error == nil)
        #expect(saved.stopRequested == nil)
        #expect(saved.text == message.text)
        let agentRequests = server.requests.filter { $0.path.hasPrefix("/v1/runs") }.count
        #expect(agentRequests == 0)
    }

    @Test("Real tool events are scoped to the selected chat and cleared at terminal")
    func tracksNativeToolActivity() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let terminal = Mutex(false)
        let server = StubServer { request in
            switch request.path {
            case "/v1/runs/tool-run/events":
                return .stream(chunks: [Data("data: {\"event\":\"tool.started\",\"tool\":\"terminal\",\"preview\":\"private arguments\"}\n\n".utf8)], finish: false)
            case "/v1/runs/tool-run":
                let status = terminal.withLock { $0 } ? "completed" : "running"
                return .json(200, "{\"run_id\":\"tool-run\",\"status\":\"\(status)\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [ChatMessage(role: .user, text: "Use a tool", stage: .running, runID: "tool-run")], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let started = await eventually { model.activeTool == "terminal" }
        try #require(started)
        model.newChat()
        let otherChatTool = model.activeTool
        #expect(otherChatTool == nil)
        model.selectChat(chat.id)
        let selectedTool = model.activeTool
        #expect(selectedTool == "terminal")
        terminal.withLock { $0 = true }
        let completed = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .completed }
        try #require(completed)
        let finalTool = model.activeTool
        #expect(finalTool == nil)
    }

    @Test("Tool completion clears activity while the run continues")
    func clearsCompletedTool() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let terminal = Mutex(false)
        let events = """
        data: {"event":"tool.started","tool":"terminal"}

        data: {"event":"tool.completed","tool":"terminal"}

        data: {"event":"message.delta","delta":"Still responding"}


        """
        let server = StubServer { request in
            switch request.path {
            case "/v1/runs/finished-tool/events":
                return .stream(chunks: [Data(events.utf8)], finish: false)
            case "/v1/runs/finished-tool":
                let status = terminal.withLock { $0 } ? "completed" : "running"
                return .json(200, "{\"run_id\":\"finished-tool\",\"status\":\"\(status)\"}")
            default: return .json(404, #"{"detail":"Not found"}"#)
            }
        }
        let store = ChatStore(directory: directory)
        let chat = Chat(messages: [ChatMessage(role: .user, text: "Use a tool", stage: .running, runID: "finished-tool")], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client(), initialModelChoices: [.testModel], initialModelChoice: .testModel)
        model.scenePhaseChanged(.active)
        let responding = await eventually { model.liveResponse == "Still responding" }
        try #require(responding)
        let tool = model.activeTool
        let busy = model.isBusy
        #expect(tool == nil)
        #expect(busy)
        terminal.withLock { $0 = true }
        let completed = await eventually { store.chat(id: chat.id)?.messages.first?.stage == .completed }
        try #require(completed)
    }
}
