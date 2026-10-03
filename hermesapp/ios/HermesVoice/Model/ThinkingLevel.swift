import Foundation

enum ThinkingLevel: String, Codable, CaseIterable, Identifiable, Sendable {
    case automatic
    case off = "none"
    case minimal
    case low
    case medium
    case high
    case xhigh
    case max

    var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .automatic: "Automatic"
        case .off: "Off"
        case .minimal: "Minimal"
        case .low: "Low"
        case .medium: "Medium"
        case .high: "High"
        case .xhigh: "Extra high"
        case .max: "Maximum"
        }
    }
}
