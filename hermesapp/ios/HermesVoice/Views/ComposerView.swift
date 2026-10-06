import SwiftUI
import QuickLook
import UniformTypeIdentifiers

struct ComposerView: View {
    @Bindable var model: AppModel
    let isModelPickerPresented: Bool
    @FocusState private var focused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var isImporterPresented = false
    @State private var previewURL: URL?
    @State private var handledFocusRequest: UUID?

    var body: some View {
        VStack(spacing: 12) {
            if model.recorder.isRecording {
                recordingControls
            } else {
                if !model.pendingAttachments.isEmpty {
                    ScrollView(.horizontal) {
                        HStack(spacing: 10) {
                            ForEach(model.pendingAttachments) { attachment in
                                AttachmentView(
                                    attachment: attachment,
                                    preview: {
                                        focused = false
                                        previewURL = model.attachmentURL(attachment)
                                    },
                                    remove: { model.removeAttachment(attachment.id) }
                                )
                                .frame(width: dynamicTypeSize.isAccessibilitySize ? 310 : 250)
                                .disabled(model.isBusy || model.isImportingAttachments)
                            }
                        }
                        .padding(.horizontal, 2)
                    }
                    .scrollIndicators(.hidden)
                    .accessibilityIdentifier("draftAttachments")
                }
                if model.isImportingAttachments {
                    ProgressView("Adding files…")
                        .font(.footnote)
                        .accessibilityIdentifier("attachmentImportStatus")
                }
                if let error = model.attachmentImportError {
                    Label("Couldn’t add files: \(error)", systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                        .accessibilityIdentifier("attachmentImportError")
                }
                Group {
                    if dynamicTypeSize.isAccessibilitySize {
                        VStack(alignment: .leading, spacing: 8) {
                            messageField.padding(.horizontal, 12)
                            HStack {
                                attachButton
                                Spacer()
                                composerActions
                            }
                        }
                    } else {
                        HStack(alignment: .bottom, spacing: 4) {
                            attachButton
                            messageField
                                .padding(.vertical, 11)
                            composerActions
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, dynamicTypeSize.isAccessibilitySize ? 12 : 16)
                .background(HermesPalette.control(colorScheme), in: RoundedRectangle(cornerRadius: 38))
            }
        }
        .frame(maxWidth: 760)
        .padding(.horizontal, model.recorder.isRecording ? 32 : 16)
        .padding(.top, 8)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity)
        .fileImporter(
            isPresented: $isImporterPresented,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            switch result {
            case .success(let urls):
                Task { await model.importAttachments(urls) }
            case .failure(let error):
                let nsError = error as NSError
                if nsError.domain != NSCocoaErrorDomain || nsError.code != NSUserCancelledError {
                    model.alert = AppAlert(title: "Couldn’t add files", message: error.localizedDescription)
                }
            }
        }
        .quickLookPreview($previewURL)
        .task(id: model.composerFocusRequest) {
            await focusForNewChatRequest()
        }
        .onChange(of: focused) { _, value in model.isComposerFocused = value }
        .onDisappear {
            focused = false
            model.isComposerFocused = false
        }
        .onChange(of: model.selectedChatID) { _, _ in
            focused = false
            isImporterPresented = false
            previewURL = nil
        }
        .onChange(of: model.recorder.isRecording) { _, isRecording in
            if isRecording {
                focused = false
                isImporterPresented = false
                previewURL = nil
            }
        }
        .onChange(of: model.isChatsPresented) { _, isPresented in
            if isPresented { focused = false }
        }
        .onChange(of: model.isSettingsPresented) { _, isPresented in
            if isPresented { focused = false }
        }
        .onChange(of: isModelPickerPresented) { _, isPresented in
            if isPresented { focused = false }
        }
    }

    @MainActor
    private func focusForNewChatRequest() async {
        let request = model.composerFocusRequest
        guard handledFocusRequest != request else { return }
        // The first task invocation also covers an initially displayed empty chat.
        // Consume even blocked requests so dismissing a sheet never reopens the keyboard.
        handledFocusRequest = request
        guard model.canFocusComposer, model.selectedChat?.messages.isEmpty != false else { return }
        await Task.yield()
        guard !Task.isCancelled,
              model.composerFocusRequest == request,
              model.selectedChat?.messages.isEmpty != false,
              model.canFocusComposer,
              !isModelPickerPresented,
              !isImporterPresented,
              !model.isImportingAttachments,
              previewURL == nil,
              !model.recorder.isRecording else { return }
        focused = true
    }

    private var messageField: some View {
        TextField("Ask Hermes", text: $model.draft, axis: .vertical)
            .lineLimit(1...(dynamicTypeSize.isAccessibilitySize ? 3 : 6))
            .focused($focused)
            .accessibilityLabel("Message Hermes")
            .accessibilityIdentifier("composerText")
    }

    private var attachButton: some View {
        Button {
            focused = false
            isImporterPresented = true
        } label: {
            Image(systemName: "plus")
                .font(.title3)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(model.isBusy || model.recorder.isRecording || model.isImportingAttachments)
        .accessibilityLabel("Attach files")
        .accessibilityIdentifier("attachFilesButton")
    }

    private var voiceButton: some View {
        Button {
            if model.isAudioPlaying {
                model.pausePlayback()
            } else {
                focused = false
                Task { await model.startRecording() }
            }
        } label: {
            Image(systemName: model.isAudioPlaying ? "stop.fill" : "waveform")
                .font(.title3.weight(.medium))
                .foregroundStyle(.white)
                .frame(width: 44, height: 44)
                .background(HermesPalette.actionAccent(colorScheme), in: Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(!model.isAudioPlaying && (model.isBusy || model.isImportingAttachments))
        .accessibilityLabel(model.isAudioPlaying ? "Stop audio" : "Record voice message")
        .accessibilityHint(model.isAudioPlaying
            ? "Pauses the reply. Resume audio beneath the reply, or tap here again to record."
            : "Tap Send to submit your recording. Touch anywhere else in the app to discard it.")
        .accessibilityIdentifier(model.isAudioPlaying ? "pauseReplyAudioButton" : "recordButton")
    }
    @ViewBuilder
    private var composerActions: some View {
        if model.isBusy {
            if model.isAudioPlaying { voiceButton }
            stopButton
        } else if !model.draft.isEmpty {
            clearDraftButton
            sendButton
        } else {
            voiceButton
            if !model.pendingAttachments.isEmpty { sendButton }
        }
    }

    private var clearDraftButton: some View {
        Button {
            model.draft = ""
            focused = true
        } label: {
            Image(systemName: "xmark")
                .font(.body.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Clear message")
        .accessibilityHint("Clears the typed draft without removing attachments.")
        .accessibilityIdentifier("clearDraftButton")
    }


    private var sendButton: some View {
        Button {
            focused = false
            model.sendText(model.draft)
        } label: {
            Image(systemName: "arrow.up")
                .font(.headline.weight(.semibold))
                .foregroundStyle(model.canSend ? Color.white : Color.secondary)
                .frame(width: 44, height: 44)
                .background(model.canSend ? HermesPalette.actionAccent(colorScheme) : Color(uiColor: .tertiarySystemFill), in: Circle())
        }
        .buttonStyle(.plain)
        .disabled(!model.canSend)
        .accessibilityLabel("Send message")
        .accessibilityIdentifier("sendTextButton")
    }

    private var stopButton: some View {
        Button { model.stopRun() } label: {
            Image(systemName: "stop.fill")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(model.isStopping ? Color.secondary : HermesPalette.background(colorScheme))
                .frame(width: 44, height: 44)
                .background(model.isStopping ? Color(uiColor: .tertiarySystemFill) : Color.primary, in: Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(model.isStopping)
        .keyboardShortcut(".", modifiers: .command)
        .accessibilityLabel(model.isStopping ? "Stopping request" : "Stop request")
        .accessibilityValue(model.isStopping ? "Waiting for confirmation" : "")
        .accessibilityHint(model.isStopping ? "The request has not finished stopping." : "Stops this request. Actions already taken are not undone.")
        .accessibilityIdentifier("stopRunButton")
    }

    private var recordingControls: some View {
        HStack(spacing: 8) {
            Image(systemName: "plus")
                .font(.system(size: 25, weight: .light))
                .frame(width: 44, height: 44)
                .accessibilityHidden(true)
            LevelMeter(levels: model.recorder.levels, reduceMotion: reduceMotion)
                .frame(maxWidth: 104, minHeight: 24, maxHeight: 24)
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 12)
            RecordingActions(model: model)
                .hidden()
                .anchorPreference(key: RecordingControlsBoundsKey.self, value: .bounds) { $0 }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 18)
        .background(HermesPalette.control(colorScheme), in: Capsule())
    }

}

struct RecordingControlsBoundsKey: PreferenceKey {
    static var defaultValue: Anchor<CGRect>? { nil }

    static func reduce(value: inout Anchor<CGRect>?, nextValue: () -> Anchor<CGRect>?) {
        value = nextValue() ?? value
    }
}

struct RecordingActions: View {
    @Bindable var model: AppModel
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: 4) {
            Button { model.discardRecording() } label: {
                Image(systemName: "stop")
                    .font(.system(size: 19, weight: .medium))
                    .foregroundStyle(.primary)
                    .frame(width: 40, height: 40)
                    .background(Color.primary.opacity(0.12), in: Circle())
                    .frame(width: 44, height: 44)
                    .contentShape(Circle())
            }
            .accessibilityLabel("Discard recording")
            .accessibilityHint("Stops recording without sending.")
            .accessibilityIdentifier("discardRecordingButton")

            Button { model.stopAndSend() } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 20, weight: .regular))
                    .foregroundStyle(.white)
                    .frame(width: 40, height: 40)
                    .background(
                        colorScheme == .dark
                            ? Color(red: 0.14, green: 0.23, blue: 0.65)
                            : HermesPalette.actionAccent(colorScheme),
                        in: Circle()
                    )
                    .frame(width: 44, height: 44)
                    .contentShape(Circle())
            }
            .accessibilityLabel("Send recording")
            .accessibilityIdentifier("stopSendButton")
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Recording")
        .accessibilityValue(Duration.seconds(model.recorder.elapsed).formatted(.time(pattern: .minuteSecond)))
    }
}

private struct LevelMeter: View {
    let levels: [Float]
    let reduceMotion: Bool

    var body: some View {
        GeometryReader { geometry in
            let count = 12
            let spacing = max(0, (geometry.size.width - CGFloat(count) * 2) / CGFloat(count - 1))
            HStack(alignment: .center, spacing: spacing) {
                ForEach(0..<count, id: \.self) { index in
                    let start = index * levels.count / count
                    let end = (index + 1) * levels.count / count
                    let level = CGFloat(min(max(levels[start..<end].max() ?? 0, 0), 1))
                    Capsule()
                        .fill(Color.primary.opacity(0.85))
                        .frame(width: 2, height: max(4, geometry.size.height * level))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .animation(reduceMotion ? nil : .linear(duration: 0.08), value: levels)
        }
        .accessibilityHidden(true)
    }
}
