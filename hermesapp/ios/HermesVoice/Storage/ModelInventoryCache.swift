import CryptoKit
import Foundation

/// A single connection-scoped snapshot, separate from the durable chat index.
struct ModelInventoryCache {
    let directory: URL
    private var fileURL: URL { directory.appending(path: "model-inventory.json") }

    private struct Snapshot: Codable {
        var version = 1
        var scope: String
        var inventory: HermesModelInventory
    }

    func load(serverURL: String, token: String) -> HermesModelInventory? {
        guard let scope = Self.scope(serverURL: serverURL, token: token),
              let data = try? Data(contentsOf: fileURL),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data),
              snapshot.version == 1, snapshot.scope == scope,
              Self.isUsable(snapshot.inventory) else { return nil }
        return snapshot.inventory
    }

    func save(_ inventory: HermesModelInventory, serverURL: String, token: String) throws {
        guard let scope = Self.scope(serverURL: serverURL, token: token),
              Self.isUsable(inventory) else { throw CacheError.invalidInventory }
        let data = try JSONEncoder().encode(Snapshot(scope: scope, inventory: inventory))
        try data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    private static func scope(serverURL: String, token: String) -> String? {
        guard APIClient.baseURL(from: serverURL) != nil,
              !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        var hash = SHA256()
        // Frame each UTF-8 field so separators in a URL or credential cannot collide.
        for field in [serverURL, token] {
            hash.update(data: Data("\(field.utf8.count):".utf8))
            hash.update(data: Data(field.utf8))
        }
        return Data(hash.finalize()).base64EncodedString()
    }

    private static func isUsable(_ inventory: HermesModelInventory) -> Bool {
        guard inventory.availableModels.allSatisfy(\.isValid),
              inventory.suggestedModels.allSatisfy(\.isValid),
              inventory.defaultModel?.isValid != false else { return false }
        let availableIDs = Set(inventory.availableModels.map(\.id))
        guard availableIDs.count == inventory.availableModels.count,
              Set(inventory.suggestedModels.map(\.id)).count == inventory.suggestedModels.count,
              inventory.suggestedModels.allSatisfy({ availableIDs.contains($0.id) }) else { return false }
        return inventory.defaultModel.map { availableIDs.contains($0.id) } ?? true
    }

    private enum CacheError: LocalizedError {
        case invalidInventory

        var errorDescription: String? {
            "The Hermes model inventory could not be saved because its connection or model identities are invalid."
        }
    }
}
