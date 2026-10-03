import Foundation

/// A native server inventory route, including provider-scoped Mixture of Agents presets.
struct HermesModelChoice: Codable, Equatable, Identifiable, Sendable {
    var provider: String
    var modelID: String
    var displayName: String

    // A length prefix keeps identities distinct even when model IDs contain separators.
    var id: String { "\(provider.utf8.count):\(provider)\(modelID)" }

    var isValid: Bool {
        guard Self.validIdentifier(provider, allowsSpaces: false),
              Self.validIdentifier(modelID, allowsSpaces: true) else { return false }
        // MoA models are preset names, so "default" is a real route within this provider.
        if provider.lowercased() == "moa" { return true }
        return !Self.reservedAliases.contains(provider.lowercased()) &&
            !Self.reservedAliases.contains(modelID.lowercased())
    }

    private static let reservedAliases: Set<String> = [
        "hermes", "hermes-agent", "default", "auto", "automatic", "openrouter/auto", "moa"
    ]

    private static func validIdentifier(_ value: String, allowsSpaces: Bool) -> Bool {
        !value.isEmpty && value == value.trimmingCharacters(in: .whitespacesAndNewlines) &&
        !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) } &&
        (allowsSpaces || !value.contains(where: \.isWhitespace))
    }
}
