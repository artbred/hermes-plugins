import Foundation
import Observation
import os

@MainActor
@Observable
final class ChatStore {
    private(set) var chats: [Chat] = []
    private(set) var hiddenSessionIDs: Set<String> = []
    private(set) var error: String?
    let directory: URL
    private var loadFailed = false
    private var indexURL: URL { directory.appending(path: "chats.json") }

    private struct Index: Codable {
        var version = 1
        var chats: [Chat]
        var hiddenSessionIDs: Set<String>
    }

    init(directory: URL = URL.applicationSupportDirectory.appending(path: "Chats")) {
        self.directory = directory
        do {
            try FileManager.default.createDirectory(at: directory.appending(path: "Audio"), withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: attachmentDirectory, withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: indexURL.path) {
                let index = try JSONDecoder().decode(Index.self, from: Data(contentsOf: indexURL))
                guard index.version == 1 else { throw StoreError.unsupportedVersion }
                chats = index.chats.sorted { $0.updatedAt > $1.updatedAt }
                hiddenSessionIDs = index.hiddenSessionIDs
                var migrated = chats
                for index in migrated.indices where UUID(uuidString: migrated[index].id) == nil {
                    let previousID = migrated[index].id
                    migrated[index].id = UUID().uuidString.lowercased()
                    let prefix = "remote-\(previousID)-"
                    for messageIndex in migrated[index].messages.indices {
                        if migrated[index].messages[messageIndex].submission != nil,
                           migrated[index].messages[messageIndex].submission?.sessionKey == nil {
                            migrated[index].messages[messageIndex].submission?.sessionKey = "ios-chat:\(previousID)"
                        }
                        if migrated[index].messages[messageIndex].remoteMessageID == nil,
                           migrated[index].messages[messageIndex].id.hasPrefix(prefix) {
                            migrated[index].messages[messageIndex].remoteMessageID =
                                String(migrated[index].messages[messageIndex].id.dropFirst(prefix.count))
                        }
                    }
                }
                if migrated != chats { try commit(migrated, hidden: hiddenSessionIDs) }
            }
        } catch {
            self.error = error.localizedDescription
            loadFailed = true
        }
    }

    func chat(id: String) -> Chat? { chats.first { $0.id == id } }

    func audioURL(fileName: String) -> URL {
        directory.appending(path: "Audio").appending(path: URL(fileURLWithPath: fileName).lastPathComponent)
    }

    var attachmentDirectory: URL { directory.appending(path: "Attachments", directoryHint: .isDirectory) }

    func attachmentURL(fileName: String) -> URL {
        attachmentDirectory.appending(path: URL(fileURLWithPath: fileName).lastPathComponent)
    }

    // Commit the disk snapshot before exposing the new state. A failed write must never
    // permit submission of a message whose idempotency key cannot survive a relaunch.
    func save(_ chat: Chat) throws {
        var next = chats.filter { $0.id != chat.id }
        next.append(chat)
        next.sort { $0.updatedAt > $1.updatedAt }
        try commit(next, hidden: hiddenSessionIDs)
    }

    func remove(id: String) throws {
        guard let chat = chat(id: id) else { return }
        var hidden = hiddenSessionIDs
        if let sessionID = chat.sessionID { hidden.insert(sessionID) }
        if let root = chat.sessionRootID { hidden.insert(root) }
        try commit(chats.filter { $0.id != id }, hidden: hidden)
        for message in chat.messages {
            if let fileName = message.audioFileName {
                try? FileManager.default.removeItem(at: audioURL(fileName: fileName))
            }
        }
        for file in chat.messages.flatMap(\.files) + (chat.draftAttachments ?? []) {
            try? FileManager.default.removeItem(at: attachmentURL(fileName: file.localFileName))
        }
    }

    private func commit(_ chats: [Chat], hidden: Set<String>) throws {
        guard !loadFailed else { throw StoreError.unreadableIndex }
        do {
            let data = try JSONEncoder().encode(Index(chats: chats, hiddenSessionIDs: hidden))
            try data.write(to: indexURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            self.chats = chats
            hiddenSessionIDs = hidden
            error = nil
        } catch {
            self.error = error.localizedDescription
            throw error
        }
    }
}

private enum StoreError: LocalizedError {
    case unreadableIndex, unsupportedVersion
    var errorDescription: String? {
        switch self {
        case .unreadableIndex: "The saved chats could not be read. Your files have been preserved; new messages cannot be saved until storage is repaired."
        case .unsupportedVersion: "These chats were saved by a newer version of Hermes Voice. Update the app to open them."
        }
    }
}

enum Log {
    static let audio = Logger(subsystem: "com.artbred.hermesapp", category: "audio")
}
