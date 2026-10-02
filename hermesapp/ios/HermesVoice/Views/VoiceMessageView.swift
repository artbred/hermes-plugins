import SwiftUI

struct VoiceMessageView: View {
    let message: ChatMessage
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Audio message", systemImage: "waveform")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            if !message.text.isEmpty {
                Text(message.text)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("transcript-\(message.id)")
            }
        }
        .padding(16)
        .frame(maxWidth: 420, alignment: .leading)
        .background(HermesPalette.control(colorScheme), in: RoundedRectangle(cornerRadius: 22))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("voiceMessage-\(message.id)")
    }
}
