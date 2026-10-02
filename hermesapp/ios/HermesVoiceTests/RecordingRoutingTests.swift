import Foundation
import Testing
@testable import HermesVoice

@Suite("Automatic voice intent routing")
struct RecordingRoutingTests {
    @Test("Only confident brain dumps bypass chat", arguments: [
        ("brain_dump", 0.90, 0.95, RecordingClassification.brainDump),
        ("brain_dump", 0.899, 0.97, .chat),
        ("brain_dump", 0.99, 0.949, .chat),
        ("chat", 0.99, 0.01, .chat),
        ("unsure", 0.99, 0.01, .chat)
    ])
    func threshold(choice: String, confidence: Double, brainProbability: Double, expected: RecordingClassification) async throws {
        let chat = choice == "unsure" ? 0 : 1 - brainProbability
        let unsure = choice == "unsure" ? 1 - brainProbability : 0
        let response = "{\"answers\":{\"intent\":{\"type\":\"choice\",\"choice\":\"\(choice)\",\"confidence\":\(confidence),\"probabilities\":{\"chat\":\(chat),\"brain_dump\":\(brainProbability),\"unsure\":\(unsure)}}}}"
        let server = StubServer { _ in .json(200, response) }
        let result = try await server.client().classifyRecording(text: "An example recording", context: [])
        #expect(result == expected)
    }

    @Test("Malformed judgments cannot silently execute or retain", arguments: [
        #"{"answers":{"intent":{"type":"choice","choice":"brain_dump","confidence":1,"probabilities":{"brain_dump":1}}}}"#,
        #"{"answers":{"intent":{"type":"choice","choice":"brain_dump","confidence":1.2,"probabilities":{"brain_dump":1,"chat":0,"unsure":0}}}}"#,
        #"{"answers":{"intent":{"type":"choice","choice":"brain_dump","confidence":1,"probabilities":{"brain_dump":0.1,"chat":0.9,"unsure":0}}}}"#
    ])
    func malformed(response: String) async {
        let server = StubServer { _ in .json(200, response) }
        await #expect(throws: (any Error).self) {
            _ = try await server.client().classifyRecording(text: "A thought", context: [])
        }
    }

    @Test("Retention must acknowledge the same operation before a note is considered saved")
    func wrongMemoryReceipt() async {
        let server = StubServer { _ in
            .json(200, "{\"success\":true,\"bank_id\":\"voice\",\"items_count\":1,\"async\":true,\"operation_id\":\"\(UUID().uuidString)\"}")
        }
        await #expect(throws: (any Error).self) {
            try await server.client().retainBrainDump(id: UUID().uuidString, text: "Today was peaceful", recordedAt: .now)
        }
    }

    @Test("A truncated title is not accepted as a generated title")
    func truncatedTitle() async {
        let server = StubServer { _ in
            .json(200, #"{"choices":[{"message":{"content":"A title that"},"finish_reason":"length"}]}"#)
        }
        await #expect(throws: (any Error).self) {
            _ = try await server.client().generateTitle(messages: [.init(role: "user", text: "A thought")])
        }
    }
}
