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
    var remoteMessageID: String?
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
    var sessionRootID: String?
    var messages: [ChatMessage] = []
    var titleGenerated: Bool?
    var titleNeedsPublishing: Bool?
    var draftAttachments: [ChatAttachment]?

    var sessionKey: String { "ios-chat:\(id)" }
    var hasPendingMessages: Bool { messages.contains { $0.role == .user && $0.stage.isPending } }

    /// Merge server-owned turns while retaining local recordings, retries and diary entries.
    /// Content matching binds legacy/local completed turns once; subsequent reads use row IDs.
    mutating func mergeHistory(_ remote: [RemoteMessage], sessionID: String) {
        var merged: [ChatMessage] = []
        var cursor = 0
        for row in remote {
            guard let role = MessageRole(rawValue: row.role) else { continue }
            let match = messages[cursor...].firstIndex { local in
                if let remoteID = local.remoteMessageID { return remoteID == row.id }
                if local.id == "remote-\(sessionID)-\(row.id)" || local.id == "remote-\(id)-\(row.id)" { return true }
                guard local.role == role, !local.isBrainDump, !local.stage.isPending,
                      local.runID != nil else { return false }
                return (local.role == .user ? local.submission?.input ?? local.text : local.text) == row.text
            }
            if let match {
                merged.append(contentsOf: messages[cursor..<match])
                var local = messages[match]
                local.remoteMessageID = row.id
                // Local user text omits the uploaded-file manifest; keep that presentation.
                if local.role == .user, local.runID == nil { local.text = row.text }
                if local.role == .assistant, local.text != row.text {
                    local.text = row.text
                    local.audioFileName = nil
                    local.needsSpeech = false
                }
                merged.append(local)
                cursor = match + 1
            } else {
                if let timestamp = row.createdAt {
                    while cursor < messages.count, messages[cursor].runID == nil,
                          messages[cursor].remoteMessageID == nil,
                          !messages[cursor].id.hasPrefix("remote-"),
                          messages[cursor].createdAt <= timestamp {
                        merged.append(messages[cursor])
                        cursor += 1
                    }
                }
                merged.append(ChatMessage(id: "remote-\(sessionID)-\(row.id)", role: role, text: row.text,
                                          createdAt: row.createdAt ?? .now, stage: .completed, remoteMessageID: row.id))
            }
        }
        merged.append(contentsOf: messages[cursor...])
        messages = merged
    }
}
