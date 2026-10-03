import Foundation

/// Eligible concrete identities and the configured default plus observed interactive usage.
struct HermesModelInventory: Codable, Equatable, Sendable {
    var defaultModel: HermesModelChoice?
    var suggestedModels: [HermesModelChoice]
    var availableModels: [HermesModelChoice]
}
