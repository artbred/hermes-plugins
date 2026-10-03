import Foundation
import Testing
import Synchronization
@testable import HermesVoice

@Suite("Native Hermes API boundaries")
struct APIClientTests {
    private static let capabilitiesJSON = """
        {"object":"hermes.api_server.capabilities","features":{
          "run_submission":true,"run_status":true,"run_events_sse":true,
          "run_stop":true,"run_approval_response":true,"session_resources":true,
          "runs_idempotency":{"supported":true,"durable":true}
        }}
        """

    @Test("Native inventory ranks featured eligible models without conflating provider names")
    func concreteModelInventory() async throws {
        let server = StubServer { _ in .json(200, """
            {"model":"server-only-default","provider":"unconfigured","providers":[
              {"slug":"first","name":"Same name","authenticated":true,"source":"built-in",
               "models":["alpha-base","alpha-feature","shared-model","alpha-feature","paid-model","hermes-agent","default",""," padded ","bad\\nmodel"],
               "featured_models":["not-in-inventory","paid-model","alpha-feature","alpha-feature"],
               "unavailable_models":["paid-model"]},
              {"slug":"second","name":"Same name","authenticated":true,"source":"user-config",
               "aliases":["custom:second","legacy-second"],"models":["shared-model","second-base"],
               "featured_models":["shared-model"]},
              {"slug":"first","authenticated":true,"models":["shared-model","third-feature"],"featured_models":["third-feature"]},
              {"slug":"missing-auth","models":["not-authenticated"]},
              {"slug":"unconfigured","authenticated":false,"source":"configured-current","models":["server-only-default"],"featured_models":["server-only-default"]},
              {"slug":"moa","authenticated":true,"source":"virtual","auth_type":"virtual","models":["preset"]},
              {"slug":"unreachable","authenticated":true,"native_catalog_empty":true,"models":["stale-model"]},
              {"slug":"pending","authenticated":true,"free_tier_pending":true,"models":["locked-model"]},
              {"slug":" bad-provider ","authenticated":true,"models":["otherwise-valid"]},
              {"slug":"custom:local","authenticated":true,"models":["My Custom Model","MiniMax-M3:cloud"]},
              {"slug":"empty","authenticated":true,"models":[]}
            ]}
            """) }
        let choices = try await server.client(path: "/p/work").modelChoices()
        #expect(choices.map(\.provider) == ["first", "second", "first", "first", "first", "second", "custom:local", "custom:local"])
        #expect(choices.map(\.modelID) == [
            "alpha-feature", "shared-model", "third-feature", "alpha-base", "shared-model",
            "second-base", "My Custom Model", "MiniMax-M3:cloud"
        ])
        #expect(Set(choices.map(\.id)).count == choices.count)
        #expect(choices[1].id != choices[4].id)
    }

    @Test("Empty and ineligible inventories never synthesize the top-level default", arguments: [
        #"{"model":"global-default","provider":"global-provider","providers":[]}"#,
        #"{"model":"global-default","provider":"global-provider","providers":[{"slug":"global-provider","authenticated":false,"models":["global-default"]}]}"#,
        #"{"model":"global-default","provider":"global-provider","providers":[{"slug":"global-provider","authenticated":true,"models":["global-default"],"unavailable_models":["global-default"]}]}"#,
        #"{"model":"global-default","provider":"global-provider","providers":[{"slug":"global-provider","authenticated":true,"models":["hermes-agent","hermes","default","auto","openrouter/auto"]}]}"#
    ])
    func noInventoryDefaultFallback(payload: String) async throws {
        let server = StubServer { _ in .json(200, payload) }
        #expect(try await server.client().modelChoices().isEmpty)
        #expect(server.requests.map(\.path) == ["/api/model/options"])
    }

    @Test("Malformed native inventory fails rather than consulting virtual model aliases", arguments: [
        "{}", "[]", "<html>Sign in</html>", #"{"providers":{}}"#,
        #"{"providers":[{"slug":"valid","authenticated":true,"models":12}]}"#,
        #"{"providers":[{"slug":"valid","authenticated":true,"models":[null]}]}"#,
        #"{"providers":[{"slug":"valid","authenticated":"yes","models":["model"]}]}"#
    ])
    func malformedModelInventory(payload: String) async {
        let server = StubServer { _ in .json(200, payload) }
        let error = await #expect(throws: APIError.self) { try await server.client().modelChoices() }
        guard case .invalidResponse? = error else { Issue.record("Expected invalidResponse"); return }
        #expect(server.requests.map(\.path) == ["/api/model/options"])
    }

    @Test("Inventory authentication and missing routes do not fall back to /v1/models", arguments: [
        (401, APIError.unauthorized),
        (404, APIError.rejected(status: 404, message: "Inventory unavailable")),
        (503, APIError.server(status: 503, message: "Inventory unavailable"))
    ])
    func unavailableModelInventory(status: Int, expected: APIError) async {
        let server = StubServer { _ in .json(status, #"{"detail":"Inventory unavailable"}"#) }
        let error = await #expect(throws: APIError.self) { try await server.client().modelChoices() }
        #expect(error == expected)
        #expect(server.requests.map(\.path) == ["/api/model/options"])
    }

    @Test("Run admission rejects non-concrete model identities before transport", arguments: [
        ("", "model"), ("provider ", "model"), ("provider\nother", "model"),
        ("auto", "model"), ("provider", ""), ("provider", " model"),
        ("provider", "model\u{0}"), ("provider", "hermes-agent"), ("provider", "default"),
        ("provider", "openrouter/auto")
    ])
    func invalidRunModelChoice(provider: String, model: String) async {
        let server = StubServer { _ in .json(202, #"{"run_id":"unexpected","status":"started"}"#) }
        let error = await #expect(throws: APIError.self) {
            try await server.client().startRun(
                text: "Hello", sessionKey: "ios-chat:test", sessionID: nil, idempotencyKey: "attempt",
                modelChoice: HermesModelChoice(provider: provider, modelID: model, displayName: "Display"))
        }
        guard case .cannotPrepare? = error else { Issue.record("Expected cannotPrepare"); return }
        #expect(server.requests.isEmpty)
    }

    @Test("Frozen concrete admission replays its original model identity after preference changes")
    func frozenModelAdmission() async throws {
        let admitted = Mutex<[String: Data]>([:])
        let server = StubServer { request in
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: String] ?? [:]
            guard let key = request.request.value(forHTTPHeaderField: "Idempotency-Key"),
                  let fingerprint = try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys]) else {
                return .json(400, #"{"detail":"Missing admission identity"}"#)
            }
            let outcome = admitted.withLock { rows -> Int in
                if let previous = rows[key] { return previous == fingerprint ? 202 : 409 }
                rows[key] = fingerprint
                return 0
            }
            if outcome == 409 { return .json(409, #"{"error":{"message":"Admission fingerprint changed"}}"#) }
            if outcome == 0, key == "original" { return .failure(.networkConnectionLost) }
            guard let provider = body["provider"], ["first", "second"].contains(provider),
                  body["model"] == "Exact-MixedCase:model" else {
                return .json(400, #"{"detail":"Wrong concrete provider/model"}"#)
            }
            return .json(202, "{\"run_id\":\"\(provider)-run\",\"status\":\"started\"}")
        }
        let first = HermesModelChoice(provider: "first", modelID: "Exact-MixedCase:model", displayName: "Same display")
        let second = HermesModelChoice(provider: "second", modelID: first.modelID, displayName: first.displayName)
        let frozen = RunSubmission(input: "Hello", sessionID: "session", instructions: "Policy",
                                   sessionKey: "ios-chat:test", modelChoice: first)
        let persisted = try JSONEncoder().encode(frozen)
        _ = await #expect(throws: APIError.self) {
            try await server.client().startRun(
                text: frozen.input, sessionKey: frozen.sessionKey!, sessionID: frozen.sessionID,
                idempotencyKey: "original", instructions: frozen.instructions, modelChoice: frozen.modelChoice)
        }
        let restored = try JSONDecoder().decode(RunSubmission.self, from: persisted)
        let replay = try await server.client().startRun(
            text: restored.input, sessionKey: restored.sessionKey!, sessionID: restored.sessionID,
            idempotencyKey: "original", instructions: restored.instructions, modelChoice: restored.modelChoice)
        #expect(replay.runID == "first-run")
        let conflict = await #expect(throws: APIError.self) {
            try await server.client().startRun(
                text: restored.input, sessionKey: restored.sessionKey!, sessionID: restored.sessionID,
                idempotencyKey: "original", instructions: restored.instructions, modelChoice: second)
        }
        #expect(conflict == .rejected(status: 409, message: "Admission fingerprint changed"))
        let next = try await server.client().startRun(
            text: restored.input, sessionKey: restored.sessionKey!, sessionID: restored.sessionID,
            idempotencyKey: "next", instructions: restored.instructions, modelChoice: second)
        #expect(next.runID == "second-run")
    }

    @Test("Legacy frozen admission omits new model fields and preserves its logical fingerprint")
    func legacyFrozenAdmission() async throws {
        let legacy = Data(#"{"input":"Original","sessionID":"old-session","sessionKey":"ios-chat:old"}"#.utf8)
        let frozen = try JSONDecoder().decode(RunSubmission.self, from: legacy)
        #expect(frozen.modelChoice == nil)
        let encoded = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(frozen)) as? [String: String])
        #expect(encoded == ["input": "Original", "sessionID": "old-session", "sessionKey": "ios-chat:old"])
        let server = StubServer { request in
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: String]
            guard body == ["input": "Original", "session_id": "old-session"] else {
                return .json(409, #"{"detail":"Legacy admission fingerprint changed"}"#)
            }
            return .json(202, #"{"run_id":"original-run","status":"started"}"#)
        }
        let receipt = try await server.client().startRun(
            text: frozen.input, sessionKey: frozen.sessionKey!, sessionID: frozen.sessionID,
            idempotencyKey: "legacy-attempt", instructions: frozen.instructions, modelChoice: frozen.modelChoice)
        #expect(receipt.runID == "original-run")
    }

    @Test("Readiness rejects non-native and wrong-field speech validation failures", arguments: [
        ("transcribe", #"{"detail":"Unprocessable entity"}"#),
        ("transcribe", #"{"detail":[{"type":"missing","loc":["body","text"]}]}"#),
        ("speak", #"{"detail":[{"type":"missing","loc":["body","data_url"]}]}"#),
        ("speak", #"{"detail":[{"type":"invalid","loc":["body","text"]}]}"#),
        ("speak", "<html>Proxy validation failure</html>")
    ])
    func invalidSpeechReadiness(route: String, body: String) async {
        let server = StubServer { request in
            if request.path == "/v1/capabilities" { return .json(200, Self.capabilitiesJSON) }
            if request.path == "/api/audio/\(route)" { return .json(422, body) }
            return .json(422, #"{"detail":[{"type":"missing","loc":["body","data_url"]}]}"#)
        }
        let error = await #expect(throws: APIError.self) { try await server.client().health() }
        guard case .invalidResponse? = error else { Issue.record("Expected invalidResponse"); return }
    }

    @Test("Readiness does not treat speech authentication failures as validation success", arguments: [
        ("transcribe", 401, APIError.unauthorized),
        ("speak", 401, APIError.unauthorized),
        ("speak", 403, APIError.rejected(status: 403, message: "Speech access denied"))
    ])
    func speechReadinessAuthentication(route: String, status: Int, expected: APIError) async {
        let server = StubServer { request in
            if request.path == "/v1/capabilities" { return .json(200, Self.capabilitiesJSON) }
            if request.path == "/api/audio/\(route)" {
                return .json(status, #"{"detail":"Speech access denied"}"#)
            }
            return .json(422, #"{"detail":[{"type":"missing","loc":["body","data_url"]}]}"#)
        }
        let error = await #expect(throws: APIError.self) { try await server.client().health() }
        #expect(error == expected)
    }

    @Test("Server URLs reject embedded credentials and discarded URL components", arguments: [
        "", "https://", "ftp://example.com", "https://user:secret@example.com",
        "https://example.com?token=secret", "https://example.com/#fragment", "https://example.com:0",
        "https://example.com:70000", "https://exam ple.com", "https://example.com/line\nbreak"
    ])
    func unsafeBaseURLs(input: String) {
        #expect(APIClient.baseURL(from: input) == nil)
    }

    @Test("Resource IDs cannot escape their endpoint or add query parameters")
    func resourcePathEscaping() async throws {
        let server = StubServer { _ in .json(404, #"{"error":{"message":"Unknown run"}}"#) }
        let client = server.client(path: "/p/profile/")
        _ = await #expect(throws: APIError.self) { try await client.run(id: "other/../runs?token=x#fragment") }
        let url = try #require(server.requests.first?.request.url)
        #expect(url.absoluteString == "https://\(server.host)/p/profile/v1/runs/other%2F..%2Fruns%3Ftoken%3Dx%23fragment")
        #expect(url.query == nil)
        #expect(url.fragment == nil)
        let before = server.requests.count
        _ = await #expect(throws: APIError.self) { try await client.run(id: "..") }
        #expect(server.requests.count == before)
    }

    @Test("Native and dashboard errors preserve server explanations", arguments: [
        (401, #"{"detail":"Invalid session token"}"#, APIError.unauthorized),
        (404, #"{"error":{"code":"run_not_found","message":"Unknown run"}}"#, APIError.runNotFound),
        (404, #"{"error":{"code":"route_not_found","message":"Unknown route"}}"#, APIError.rejected(status: 404, message: "Unknown route")),
        (404, #"{"error":"run_not_found"}"#, APIError.rejected(status: 404, message: "run_not_found")),
        (404, #"{"code":"run_not_found","detail":"Unknown route"}"#, APIError.rejected(status: 404, message: "Unknown route")),
        (401, #"{"error":{"code":"run_not_found","message":"Authentication required"}}"#, APIError.unauthorized),
        (503, #"{"error":{"code":"run_not_found","message":"Status unavailable"}}"#, APIError.server(status: 503, message: "Status unavailable")),
        (409, #"{"error":{"message":"Idempotency-Key was already used with a different request payload","code":"idempotency_key_conflict"}}"#,
         APIError.rejected(status: 409, message: "Idempotency-Key was already used with a different request payload")),
        (413, #"{"detail":"Audio recording is too large"}"#, APIError.rejected(status: 413, message: "Audio recording is too large")),
        (503, #"{"error":"No model available"}"#, APIError.server(status: 503, message: "No model available"))
    ])
    func nativeErrors(status: Int, body: String, expected: APIError) async {
        let server = StubServer { _ in .json(status, body) }
        let error = await #expect(throws: APIError.self) { try await server.client().run(id: "run_a") }
        #expect(error == expected)
    }

    @Test("Malformed successful responses fail instead of presenting an empty conversation")
    func malformedMessages() async {
        let server = StubServer { _ in .json(200, "<html>Sign in</html>") }
        let error = await #expect(throws: APIError.self) { try await server.client().messages(sessionID: "session") }
        guard case .invalidResponse? = error else { Issue.record("Expected invalidResponse"); return }
    }

    @Test("History hides tools and reasoning and retains stable numeric identities")
    func visibleMessageHistory() async throws {
        let server = StubServer { _ in .json(200, """
            {"data":[
              {"id":10,"role":"system","content":"private instructions","timestamp":1700000000},
              {"id":11,"role":"user","content":[{"type":"text","text":"Describe this"},{"type":"image_url","image_url":{"url":"data:image/png;base64,abc"}}],"timestamp":1700000001},
              {"id":12,"role":"assistant","content":null,"reasoning":"private thought","tool_calls":[{}]},
              {"id":13,"role":"tool","content":"private tool output"},
              {"id":14,"role":"assistant","content":[{"type":"thinking","text":"private thought"},{"type":"text","text":"Visible answer"},{"type":"text","text":"Second paragraph"}],"timestamp":"2023-11-14T22:13:22Z"},
              {"id":15,"role":"assistant","content":"   "}
            ]}
            """) }
        let messages = try await server.client().messages(sessionID: "session").messages
        #expect(messages.map(\.id) == ["11", "14"])
        #expect(messages.map(\.text) == ["Describe this", "Visible answer\nSecond paragraph"])
        #expect(messages.map(\.createdAt) == [Date(timeIntervalSince1970: 1700000001), Date(timeIntervalSince1970: 1700000002)])
    }

    @Test("Verification candidates are superseded, not delivered alongside the accepted answer")
    func supersededVerificationDrafts() async throws {
        let server = StubServer { _ in .json(200, """
            {"session_id":"shared","data":[
              {"id":1,"role":"user","content":"Prepare the repositories"},
              {"id":2,"role":"assistant","content":"Preparing the repositories now.","finish_reason":"tool_calls"},
              {"id":3,"role":"assistant","content":"Ready to automate both repositories.","finish_reason":"verify_hook_continue"},
              {"id":4,"role":"assistant","content":"Automation is ready for both repositories.","finish_reason":"verification_required"},
              {"id":5,"role":"assistant","content":"Both repositories are ready for automation.","finish_reason":"stop"},
              {"id":6,"role":"user","content":"Repeat that"},
              {"id":7,"role":"assistant","content":"Both repositories are ready for automation.","finish_reason":"stop"}
            ]}
            """) }
        let history = try await server.client().messages(sessionID: "shared")
        #expect(history.messages.map(\.id) == ["1", "2", "5", "6", "7"])
        #expect(history.messages.map(\.text) == [
            "Prepare the repositories", "Preparing the repositories now.",
            "Both repositories are ready for automation.", "Repeat that",
            "Both repositories are ready for automation."
        ])
    }

    @Test("A full page of verification drafts cannot hide the accepted reply on the next page")
    func verificationDraftPagination() async throws {
        let server = StubServer { request in
            let query = URLComponents(url: request.request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if query.contains(URLQueryItem(name: "offset", value: "0")) {
                let drafts = (1...500).map { ["id": $0, "role": "assistant", "content": "Provisional \($0)", "finish_reason": "verification_required"] as [String: Any] }
                let data = try! JSONSerialization.data(withJSONObject: ["session_id": "shared", "data": drafts])
                return .json(200, String(decoding: data, as: UTF8.self))
            }
            return .json(200, #"{"session_id":"shared","data":[{"id":501,"role":"assistant","content":"Accepted final answer","finish_reason":"stop"}]}"#)
        }
        let history = try await server.client().messages(sessionID: "shared")
        #expect(history.messages.map(\.text) == ["Accepted final answer"])
        #expect(history.supersededMessageIDs == Set((1...500).map(String.init)))
    }

    @Test("Shared history includes desktop and CLI sessions without importing messaging platforms")
    func sessionPagination() async throws {
        let server = StubServer { request in
            let query = URLComponents(url: request.request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if query.contains(URLQueryItem(name: "source", value: "desktop")) {
                return .json(200, #"{"data":[{"id":"desktop"}],"has_more":false}"#)
            }
            if query.contains(URLQueryItem(name: "source", value: "cli")) {
                return .json(200, #"{"data":[{"id":"cli"}],"has_more":false}"#)
            }
            guard query.contains(URLQueryItem(name: "source", value: "api_server")) else {
                return .json(200, #"{"data":[{"id":"telegram-private"}],"has_more":false}"#)
            }
            if query.contains(URLQueryItem(name: "offset", value: "0")) {
                return .json(200, #"{"data":[{"id":"pinned","title":"Pinned"},{"id":"recent"}],"has_more":true}"#)
            }
            return .json(200, #"{"data":[{"id":"older"},{"id":"pinned","title":"Pinned"}],"has_more":false}"#)
        }
        #expect(Set(try await server.client().sessions().map(\.id)) == ["pinned", "recent", "older", "desktop", "cli"])
    }

    @Test("SSE supports CRLF, multiline data, Unicode, comments, and exact approval IDs")
    func eventFraming() throws {
        let wire = ": keepalive\r\n\r\nid: 7\r\ndata: {\"event\":\"message.delta\",\r\ndata: \"delta\":\"Привет \u{1D11E}\"}\r\n\r\n"
            + "id: 8\ndata: {\"event\":\"approval.request\",\"command\":\"rm file\",\"choices\":[\"once\",\"deny\"]}\n\n"
            + "id: 9\ndata: {\"event\":\"approval.request\",\"request_id\":\"approval-42\",\"command\":\"ls\",\"choices\":[\"once\",\"deny\"]}\n\n"
            + "data: {\"event\":\"message.interim\",\"text\":\"working\",\"already_streamed\":true}\n\n"
        var parser = RunEventParser()
        var events: [RunEvent] = []
        for byte in wire.utf8 { if let event = try parser.append(byte) { events.append(event) } }
        #expect(events.map(\.type) == ["message.delta", "approval.request", "approval.request", "message.interim"])
        #expect(events[0].text == "Привет \u{1D11E}")
        #expect(events[0].id == "7")
        #expect(events[1].id == nil)
        #expect(events[2].id == "approval-42")
        #expect(events[3].alreadyStreamed)
    }

    @Test("A truncated SSE frame is not published")
    func truncatedEvent() throws {
        var parser = RunEventParser()
        var delivered = false
        for byte in "data: {\"event\":\"message.delta\",\"delta\":\"partial\"}\n".utf8 {
            if try parser.append(byte) != nil { delivered = true }
        }
        #expect(!delivered)
    }

    @Test("Cancelling an event consumer closes its live HTTP connection")
    @MainActor
    func cancelStream() async {
        let server = StubServer { _ in .stream(chunks: [Data(": open\n\n".utf8)], finish: false) }
        let consumer = Task {
            do { for try await _ in server.client().events(runID: "running") {} }
            catch { }
        }
        let connected = await eventually { !server.requests.isEmpty }
        #expect(connected)
        consumer.cancel()
        #expect(await eventually { server.cancellations > 0 })
        await consumer.value
    }

    @Test("Speech requests the chosen voice and paid model and requires their exact acknowledgment")
    func selectedSpeechVoice() async throws {
        let voice = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        let server = StubServer { _ in
            .json(200, #"{"ok":true,"provider":"fish","reference_id":"0123456789abcdef0123456789abcdef","model":"s2.1-pro","mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,YWJj"}"#)
        }
        let audio = try await server.client().speak(text: "Hello", voice: voice)
        let request = try #require(server.requests.first)
        let body = try #require(JSONSerialization.jsonObject(with: request.body) as? [String: String])
        #expect(request.method == "POST")
        #expect(request.path == "/api/audio/speak")
        #expect(body == ["text": "Hello", "reference_id": voice.referenceID, "model": "s2.1-pro"])
        #expect(audio.fileExtension == "mp3")
    }

    @Test("Speech rejects legacy or mismatched provider, voice and model acknowledgments", arguments: [
        #"{"ok":true,"mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,YWJj"}"#,
        #"{"ok":true,"provider":"openai","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro","mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,YWJj"}"#,
        #"{"ok":true,"provider":"fish","reference_id":"0123456789abcdef0123456789abcdef","model":"s2.1-pro","mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,YWJj"}"#,
        #"{"ok":true,"provider":"fish","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro-free","mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,YWJj"}"#
    ])
    func mismatchedSpeechAcknowledgment(body: String) async {
        let server = StubServer { _ in .json(200, body) }
        let error = await #expect(throws: APIError.self) {
            try await server.client().speak(text: "Hello", voice: .defaultVoice)
        }
        guard case .invalidResponse? = error else { Issue.record("Expected invalidResponse"); return }
    }

    @Test("Invalid voices and unpaid models fail before sending speech", arguments: [
        SpeechVoice(referenceID: "invalid"),
        SpeechVoice(referenceID: "0123456789abcdef0123456789abcdeg"),
        SpeechVoice(referenceID: SpeechVoice.defaultReferenceID, modelID: "s2.1-pro-free")
    ])
    func invalidSpeechVoice(voice: SpeechVoice) async {
        let server = StubServer { _ in .json(500, #"{"error":"Unexpected request"}"#) }
        let error = await #expect(throws: APIError.self) {
            try await server.client().speak(text: "Hello", voice: voice)
        }
        guard case .cannotPrepare? = error else { Issue.record("Expected cannotPrepare"); return }
        #expect(server.requests.isEmpty)
    }

    @Test("Speech refuses corrupt base64 and mismatched MIME declarations", arguments: [
        #"{"ok":true,"provider":"fish","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro","mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,%%%"}"#,
        #"{"ok":true,"provider":"fish","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro","mime_type":"audio/mpeg","data_url":"data:text/html;base64,YWJj"}"#,
        #"{"ok":true,"provider":"fish","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro","mime_type":"audio/mpeg","data_url":"data:audio/mpeg;base64,"}"#
    ])
    func corruptSpeech(body: String) async {
        let server = StubServer { _ in .json(200, body) }
        let error = await #expect(throws: APIError.self) { try await server.client().speak(text: "Hello", voice: .defaultVoice) }
        guard case .invalidResponse? = error else { Issue.record("Expected invalidResponse"); return }
    }
}
