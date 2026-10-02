import Foundation

enum MessageRole: String, Codable, Sendable {
    case user, assistant
}

enum MessageInput: String, Codable, Sendable {
    case text, voice
}

enum MessageStage: String, Codable, Sendable {
    case queued, transcribing, classifying, savingMemory, uploading, submitting, running, completed, failed, interrupted

    var isPending: Bool {
        switch self {
        case .queued, .transcribing, .classifying, .savingMemory, .uploading, .submitting, .running: true
        case .completed, .failed, .interrupted: false
        }
    }
}

struct ChatMessage: Identifiable, Codable, Equatable, Sendable {
    var id: String = UUID().uuidString.lowercased()
    var role: MessageRole
    var input: MessageInput = .text
    var text: String
    var createdAt: Date = .now
    var stage: MessageStage = .queued
    var runID: String?
    var audioFileName: String?
    var error: String?
    var replyTo: String?
    // Uncertain transport failures reuse this key. Explicit retry of a terminal or
    // unavailable run gets a new attempt; earlier actions may already have happened.
    var attempt = 0
    var runWasTerminal = false
    var needsSpeech = false
    var classification: RecordingClassification?
    var attachments: [ChatAttachment]?
    var submission: RunSubmission?
    // Stop intent survives uncertain admission and relaunch; an acknowledgement is not terminal.
    var stopRequested: Bool?
    var stopAcknowledged: Bool?
    var recordingDuration: TimeInterval?
    var replyNotificationRequested: Bool?
    var replyNotificationAcknowledged: Bool?

    var files: [ChatAttachment] { attachments ?? [] }

    var isBrainDump: Bool { classification == .brainDump }

    var requestKey: String { "\(id)-\(attempt)" }
}

struct Chat: Identifiable, Codable, Equatable, Sendable {
    var id: String = UUID().uuidString.lowercased()
    var title: String = "New chat"
    var createdAt: Date = .now
    var updatedAt: Date = .now
    var sessionID: String?
    var messages: [ChatMessage] = []
    var titleGenerated: Bool?
    var draftAttachments: [ChatAttachment]?

    var sessionKey: String { "ios-chat:\(id)" }
    var hasPendingMessages: Bool { messages.contains { $0.role == .user && $0.stage.isPending } }
}
