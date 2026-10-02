import SwiftUI
import UniformTypeIdentifiers

enum HermesPalette {
    static func background(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color(red: 0.035, green: 0.039, blue: 0.047) : Color(red: 0.985, green: 0.985, blue: 0.995)
    }

    static func chatAccent(_ scheme: ColorScheme) -> Color {
        Color.purple.opacity(scheme == .dark ? 0.15 : 0.06)
    }

    static func control(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color(red: 0.14, green: 0.145, blue: 0.16) : Color(red: 0.925, green: 0.93, blue: 0.95)
    }

    static func menu(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color(red: 0.085, green: 0.09, blue: 0.105) : Color(red: 0.96, green: 0.965, blue: 0.98)
    }
}

struct AttachmentView: View {
    let attachment: ChatAttachment
    let preview: () -> Void
    var remove: (() -> Void)?
    @Environment(\.colorScheme) private var colorScheme

    private var icon: String {
        guard let type = UTType(mimeType: attachment.contentType) else { return "doc" }
        if type.conforms(to: .image) { return "photo" }
        if type.conforms(to: .pdf) { return "doc.richtext" }
        if type.conforms(to: .audio) { return "waveform" }
        if type.conforms(to: .movie) { return "film" }
        return "doc"
    }

    var body: some View {
        HStack(spacing: 0) {
            Button(action: preview) {
                HStack(spacing: 12) {
                    Image(systemName: icon)
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(attachment.name)
                            .font(.subheadline.weight(.medium))
                            .lineLimit(2)
                            .truncationMode(.middle)
                        Text(ByteCountFormatter.string(fromByteCount: attachment.byteCount, countStyle: .file))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(14)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Preview \(attachment.name)")
            .accessibilityValue(ByteCountFormatter.string(fromByteCount: attachment.byteCount, countStyle: .file))
            .accessibilityIdentifier("previewAttachment-\(attachment.id)")
            if let remove {
                Button(action: remove) {
                    Image(systemName: "xmark")
                        .font(.caption.weight(.semibold))
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Remove \(attachment.name)")
                .accessibilityIdentifier("removeAttachment-\(attachment.id)")
            }
        }
        .background(HermesPalette.control(colorScheme), in: RoundedRectangle(cornerRadius: 18))
    }
}
