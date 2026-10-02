import SwiftUI
import QuickLook

struct ChatMessageView: View {
    @Bindable var model: AppModel
    let message: ChatMessage
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var previewURL: URL?

    private var isUser: Bool { message.role == .user }
    private var isVoice: Bool { isUser && message.input == .voice }
    private var wasStopped: Bool {
        isUser && message.stage == .interrupted && message.stopRequested == true
    }
    private var isSelectedForPlayback: Bool { model.playingMessageID == message.id }
    private var isPlaying: Bool { isSelectedForPlayback && model.isAudioPlaying }
    private var isSynthesizing: Bool { model.synthesizingMessageIDs.contains(message.id) }
    private var canRetry: Bool {
        isUser && (message.stage == .failed || message.stage == .interrupted)
    }
    private var hasActions: Bool { (!isVoice && !message.text.isEmpty) || canRetry }

    var body: some View {
        VStack(alignment: isUser ? .trailing : .leading, spacing: 10) {
            if isVoice {
                VoiceMessageView(message: message)
            } else if !message.text.isEmpty {
                messageText
            }
            ForEach(message.files) { attachment in
                AttachmentView(attachment: attachment, preview: {
                    previewURL = model.attachmentURL(attachment)
                })
                .frame(maxWidth: 360)
            }
            status
            if let error = message.error, !error.isEmpty {
                Label(error, systemImage: wasStopped ? "info.circle" : "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(wasStopped ? Color.secondary : Color.red)
                    .textSelection(.enabled)
            }
            if hasActions {
                actionLayout {
                    if !isUser && !message.isBrainDump && !message.text.isEmpty {
                        Button {
                            Task { await model.play(message) }
                        } label: {
                            if isSynthesizing {
                                Label { Text("Preparing audio…") } icon: { ProgressView().controlSize(.small) }
                            } else {
                                Label(
                                    isPlaying ? "Pause audio" : isSelectedForPlayback ? "Resume audio" : "Listen",
                                    systemImage: isPlaying ? "pause.fill" : isSelectedForPlayback ? "play.fill" : "speaker.wave.2"
                                )
                            }
                        }
                        .disabled(isSynthesizing || model.recorder.isRecording)
                        .accessibilityIdentifier("playReply-\(message.id)")
                    }
                    if !isVoice && !message.text.isEmpty {
                        Button("Copy", systemImage: "doc.on.doc") {
                            model.copyMessage(message)
                        }
                            .accessibilityLabel("Copy \(isUser ? "message" : "reply")")
                    }
                    if canRetry {
                        Button("Retry", systemImage: "arrow.clockwise") { model.retry(message) }
                            .disabled(model.isBusy || model.recorder.isRecording)
                            .accessibilityIdentifier("retry-\(message.id)")
                    }
                }
                .font(.footnote.weight(.medium))
                .buttonStyle(.borderless)
                .controlSize(.regular)
                .fixedSize(horizontal: !dynamicTypeSize.isAccessibilitySize, vertical: true)
                .frame(minHeight: 44)
            }
        }
        .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
        .padding(.leading, isUser ? 28 : 0)
        .padding(.trailing, isUser ? 0 : 8)
        .accessibilityElement(children: .contain)
        .quickLookPreview($previewURL)
    }

    private var actionLayout: AnyLayout {
        dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: isUser ? .trailing : .leading, spacing: 12))
            : AnyLayout(HStackLayout(spacing: 14))
    }

    @ViewBuilder
    private var messageText: some View {
        if isUser {
            Text(message.text)
                .textSelection(.enabled)
                .padding(16)
                .background(HermesPalette.control(colorScheme), in: RoundedRectangle(cornerRadius: 22))
        } else {
            HTMLResponseView(content: message.text)
                .accessibilityIdentifier("htmlReply-\(message.id)")
        }
    }

    @ViewBuilder
    private var status: some View {
        switch message.stage {
        case .queued, .transcribing, .classifying, .uploading, .submitting, .running, .savingMemory:
            EmptyView()
        case .failed:
            statusLabel("Couldn’t complete this request", icon: "exclamationmark.circle")
        case .interrupted:
            statusLabel(wasStopped ? "Request stopped" : "Request interrupted", icon: "stop.circle")
        case .completed:
            if message.isBrainDump {
                statusLabel("Saved to brain dump", icon: "checkmark")
            }
        }
    }

    private func statusLabel(_ text: String, icon: String) -> some View {
        Label(text, systemImage: icon)
            .font(.caption)
            .foregroundStyle(.secondary)
            .accessibilityIdentifier("messageStatus-\(message.id)")
    }
}
