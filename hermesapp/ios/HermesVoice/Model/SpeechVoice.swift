import Foundation

struct SpeechVoice: Codable, Equatable, Sendable {
    static let defaultReferenceID = "933563129e564b19a115bedd57b7406a"
    static let model = "s2.1-pro"
    static let defaultVoice = SpeechVoice(referenceID: defaultReferenceID)

    let referenceID: String
    // Store the actual model with cached audio, rather than relabeling old audio
    // when the application's default model changes in a later release.
    let modelID: String

    init(referenceID: String, modelID: String = SpeechVoice.model) {
        self.referenceID = referenceID
        self.modelID = modelID
    }

    private enum CodingKeys: String, CodingKey { case referenceID, modelID }

    init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        referenceID = try values.decode(String.self, forKey: .referenceID)
        modelID = try values.decodeIfPresent(String.self, forKey: .modelID) ?? ""
    }

    var cacheKey: String { "\(referenceID)-\(modelID)" }

    static func isValidReferenceID(_ value: String) -> Bool {
        value.utf8.count == 32 && value.utf8.allSatisfy {
            (48...57).contains($0) || (97...102).contains($0)
        }
    }
}
