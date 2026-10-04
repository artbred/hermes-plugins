import SwiftUI

struct RootView: View {
    @Bindable var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.openURL) private var openURL
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @State private var reviewedApproval: RunApproval?
    @State private var isModelPickerPresented = false

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                NavigationStack {
                    ChatScreen(model: model, isModelPickerPresented: isModelPickerPresented) { approval in
                        dismissKeyboard()
                        reviewedApproval = approval
                    }
                        // Changing transcripts from the sidebar must not animate the
                        // outgoing conversation or recreate the composer's focus state.
                        .transaction(value: model.selectedChatID) {
                            $0.animation = nil
                            $0.disablesAnimations = true
                        }
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbarBackground(HermesPalette.background(colorScheme), for: .navigationBar)
                        .toolbarBackground(.visible, for: .navigationBar)
                        .toolbar { chatToolbar }
                        .tint(.primary)
                }
                .accessibilityHidden(model.isChatsPresented)
                .disabled(model.isChatsPresented)
                if model.isChatsPresented {
                    Color.black.opacity(colorScheme == .dark ? 0.5 : 0.22)
                        .ignoresSafeArea()
                        .onTapGesture { closeMenu() }
                        .accessibilityLabel("Close chats")
                        .accessibilityAddTraits(.isButton)
                        .accessibilityIdentifier("chatMenuScrim")
                        .transition(.opacity)
                    ChatListView(model: model, close: closeMenu)
                    .frame(width: min(360, geometry.size.width * 0.9))
                    .background(HermesPalette.menu(colorScheme).ignoresSafeArea())
                    .shadow(color: .black.opacity(0.12), radius: 24, x: 8)
                    .transition(.move(edge: .leading))
                    .zIndex(1)
                }
            }
        }
        .allowsHitTesting(!model.recorder.isRecording)
        .accessibilityHidden(model.recorder.isRecording)
        .overlayPreferenceValue(RecordingSendBoundsKey.self) { sendBounds in
            if model.recorder.isRecording {
                GeometryReader { geometry in
                    ZStack(alignment: .topLeading) {
                        RecordingDiscardTarget { model.discardRecording() }
                            .ignoresSafeArea()
                        if let sendBounds {
                            let frame = geometry[sendBounds]
                            RecordingSendButton(model: model)
                                .frame(width: frame.width, height: frame.height)
                                .position(x: frame.midX, y: frame.midY)
                        }
                    }
                }
            }
        }
        .onChange(of: model.isSettingsPresented) { _, presented in
            if presented {
                dismissKeyboard()
            }
        }
        .onChange(of: model.selectedChatID) { _, _ in
            reviewedApproval = nil
        }
        .sheet(isPresented: $model.isSettingsPresented) {
            NavigationStack {
                SettingsView(model: model)
            }
            .presentationDetents([.large])
            .presentationDragIndicator(.visible)
        }
        .sheet(item: $reviewedApproval) { approval in
            ApprovalView(model: model, approval: approval)
        }
        .sheet(isPresented: $isModelPickerPresented) {
            ChatModelPicker(model: model)
        }
        .alert(
            model.alert?.title ?? "",
            isPresented: Binding(get: { model.alert != nil }, set: { if !$0 { model.alert = nil } }),
            presenting: model.alert
        ) { alert in
            if alert.offersSettings {
                Button("Open Settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                }
                Button("Cancel", role: .cancel) {}
            } else {
                Button("OK", role: .cancel) {}
            }
        } message: { alert in
            Text(alert.message)
        }
        .onOpenURL { model.handle($0) }
        .onChange(of: scenePhase, initial: true) { _, phase in model.scenePhaseChanged(phase) }
        .task(id: scenePhase) {
            if scenePhase == .active { await model.synchronizeChats() }
        }
    }

    @ToolbarContentBuilder
    private var chatToolbar: some ToolbarContent {
        if #available(iOS 26.0, *) {
            ToolbarItem(placement: .topBarLeading) { headerLeading }
                .sharedBackgroundVisibility(.hidden)
            ToolbarItem(placement: .topBarTrailing) { newChatButton }
                .sharedBackgroundVisibility(.hidden)
        } else {
            ToolbarItem(placement: .topBarLeading) { headerLeading }
            ToolbarItem(placement: .topBarTrailing) { newChatButton }
        }
    }

    private var headerLeading: some View {
        HStack(spacing: 16) {
            Button {
                dismissKeyboard()
                withAnimation(menuAnimation) { model.isChatsPresented = true }
            } label: {
                Image(systemName: "line.3.horizontal")
                    .font(.body.weight(.medium))
                    .frame(width: 44, height: 44)
                    .background(HermesPalette.control(colorScheme), in: Circle())
                    .overlay(Circle().strokeBorder(.primary.opacity(0.08), lineWidth: 1))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Chats")
            .accessibilityIdentifier("chatsButton")

            Button {
                dismissKeyboard()
                isModelPickerPresented = true
            } label: {
                HStack(spacing: 4) {
                    Text(model.selectedChatModel?.displayName ?? "Choose model")
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                    if model.modelSelectionError != nil {
                        Image(systemName: "exclamationmark.circle")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!model.canChangeModel)
            .accessibilityLabel("Choose chat model")
            .accessibilityValue(model.selectedChatModel?.displayName ?? "No model selected")
            .accessibilityHint(model.modelSelectionError ?? "Select a model for new messages")
            .accessibilityIdentifier("chatModelButton")
        }
        .fixedSize(horizontal: true, vertical: false)
    }

    private var newChatButton: some View {
        Button { model.newChat() } label: {
            Image(systemName: "square.and.pencil")
                .font(.body.weight(.medium))
                .frame(width: 44, height: 44)
                .background(HermesPalette.control(colorScheme), in: Circle())
                .overlay(Circle().strokeBorder(.primary.opacity(0.08), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(!model.canStartNewChat)
        .accessibilityLabel("New chat")
        .accessibilityIdentifier("newChatButton")
    }

    private var menuAnimation: Animation? {
        reduceMotion ? nil : .easeInOut(duration: 0.24)
    }

    private func closeMenu() {
        dismissKeyboard()
        withAnimation(menuAnimation) { model.isChatsPresented = false }
    }

    private func dismissKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
}

private struct ChatModelPicker: View {
    @Bindable var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        NavigationStack {
            List {
                Picker("Thinking", selection: Binding(
                    get: { model.selectedThinkingLevel },
                    set: { model.selectThinkingLevel($0) }
                )) {
                    ForEach(ThinkingLevel.allCases) { level in
                        Text(level.displayName)
                            .tag(level)
                            .accessibilityIdentifier("chatThinking-\(level.rawValue)")
                    }
                }
                .pickerStyle(.menu)
                .disabled(!model.canChangeModel)
                .accessibilityIdentifier("chatThinkingPicker")
                .accessibilityValue(model.selectedThinkingLevel.displayName)
                if let error = model.modelSelectionError {
                    Section {
                        Label("Couldn’t load models", systemImage: "exclamationmark.triangle")
                            .font(.headline)
                        Text(error)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                        Button("Retry") { Task { await model.refreshModelChoices() } }
                            .disabled(model.isLoadingModels || !model.canChangeModel)
                            .accessibilityIdentifier("refreshChatModelsButton")
                    }
                    .accessibilityIdentifier("chatModelsError")
                }
                if !model.modelChoices.isEmpty {
                    Section {
                        ForEach(model.modelChoices) { choice in
                            Button {
                                model.selectChatModel(choice)
                                dismiss()
                            } label: {
                                HStack(spacing: 16) {
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(choice.displayName)
                                            .font(.body.weight(.medium))
                                        Text("\(choice.provider) · \(choice.modelID)")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                    .fixedSize(horizontal: false, vertical: true)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    if model.selectedChatModel?.id == choice.id {
                                        Image(systemName: "checkmark")
                                            .font(.body.weight(.semibold))
                                            .foregroundStyle(.tint)
                                            .accessibilityHidden(true)
                                    }
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .disabled(!model.canChangeModel)
                            .accessibilityValue(model.selectedChatModel?.id == choice.id ? "Selected" : "")
                            .accessibilityIdentifier("chatModel-\(choice.id)")
                        }
                    }
                } else if !model.isLoadingModels && model.modelSelectionError == nil {
                    Text("No selectable models are available from this server.")
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("chatModelsEmpty")
                }
            }
            .listStyle(.plain)
            .contentMargins(.top, 0, for: .scrollContent)
            .navigationTitle("Choose model")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents(dynamicTypeSize.isAccessibilitySize ? [.large] : [.medium, .large])
        .presentationDragIndicator(.visible)
        .task {
            if model.modelChoices.isEmpty && !model.isLoadingModels && model.modelSelectionError == nil {
                await model.refreshModelChoices()
            }
        }
    }
}

private struct RecordingDiscardTarget: UIViewRepresentable {
    let discard: () -> Void

    func makeUIView(context: Context) -> RecordingDiscardView {
        let view = RecordingDiscardView()
        view.backgroundColor = .clear
        view.isAccessibilityElement = true
        view.accessibilityLabel = "Discard recording"
        view.accessibilityHint = "Discards immediately without sending. Use Send to keep this recording."
        view.accessibilityTraits = .button
        view.accessibilityIdentifier = "discardRecordingTarget"
        view.discard = discard
        return view
    }

    func updateUIView(_ view: RecordingDiscardView, context: Context) {
        view.discard = discard
    }
}

final class RecordingDiscardView: UIView {
    var discard: (() -> Void)?
    private let acceptsTouchesAfter = ProcessInfo.processInfo.systemUptime

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        // This layer is inserted as the microphone action completes. A touch that
        // began before insertion belongs to that action, not to a new discard tap.
        guard touches.contains(where: { $0.timestamp >= acceptsTouchesAfter }) else { return }
        // Own the touch from its start so removing this overlay cannot activate
        // a toolbar button, link, or other control underneath it.
        discard?()
    }

    override func accessibilityActivate() -> Bool {
        discard?()
        return true
    }
}

private struct ChatScrollAppearance: ViewModifier {
    @ViewBuilder
    func body(content: Content) -> some View {
        let scroll = content.scrollBounceBehavior(.basedOnSize)
        if #available(iOS 26.0, *) {
            // Keep the scroll-edge effect separate from the opaque navigation-bar background.
            scroll.scrollEdgeEffectHidden(true, for: .top)
        } else {
            scroll
        }
    }
}

private struct ChatScreen: View {
    @Bindable var model: AppModel
    let isModelPickerPresented: Bool
    let reviewApproval: (RunApproval) -> Void
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(spacing: 0) {
            if !model.settings.isConfigured {
                Button { model.isSettingsPresented = true } label: {
                    Label("Connect your Hermes server", systemImage: "server.rack")
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                        .background(.tint.opacity(0.08), in: RoundedRectangle(cornerRadius: 16))
                }
                .buttonStyle(.plain)
                .padding(.horizontal)
                .padding(.top, 8)
                .accessibilityHint("Opens connection settings")
                .accessibilityIdentifier("openSettingsButton")
            }
            if let message = model.connectionMessage {
                Label(message, systemImage: "network")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.top, 8)
            }
            if let error = model.store.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .padding(.horizontal)
                    .padding(.top, 8)
            }
            if let chat = model.selectedChat, !chat.messages.isEmpty {
                ChatTranscript(model: model, chat: chat, reviewApproval: reviewApproval)
                    .id(chat.id)
            } else {
                Color.clear.accessibilityIdentifier("emptyChat")
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ComposerView(model: model, isModelPickerPresented: isModelPickerPresented)
        }
        // Keep the gradient attached to the keyboard-avoiding chat viewport, not the full-screen base color.
        .background {
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                LinearGradient(
                    colors: [
                        .clear,
                        Color.indigo.opacity(colorScheme == .dark ? 0.14 : 0.035),
                        Color.blue.opacity(colorScheme == .dark ? 0.20 : 0.06),
                        HermesPalette.chatAccent(colorScheme)
                    ],
                    startPoint: .top,
                    endPoint: .bottom
                )
                .frame(height: 320)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .ignoresSafeArea(.container, edges: .bottom)
            .allowsHitTesting(false)
        }
        .background {
            HermesPalette.background(colorScheme)
                .ignoresSafeArea(.container)
        }
        .background {
            // The system keyboard is rounded: continue the viewport's end color
            // behind it instead of exposing the hosting view's black corners.
            HermesPalette.background(colorScheme)
                .overlay(HermesPalette.chatAccent(colorScheme))
                .ignoresSafeArea()
        }
    }

}

private struct ChatTranscript: View {
    @Bindable var model: AppModel
    let chat: Chat
    let reviewApproval: (RunApproval) -> Void
    @State private var following = TranscriptFollowing()
    @State private var scrollPosition = ScrollPosition(edge: .bottom)
    @State private var mountedAt = Date.now

    private struct Row: Identifiable {
        let id: String
        let message: ChatMessage?
        let request: ChatMessage?
        let isNewReply: Bool
        var isCurrentRequest = false
    }

    private var rows: [Row] {
        let messageIDs = Set(chat.messages.map(\.id))
        let activeRequest = model.selectedChatID == chat.id ? model.activeMessage : nil
        var result = chat.messages.map { message in
            Row(
                id: message.id, message: message, request: nil,
                isNewReply: message.role == .assistant && message.replyTo != nil && message.createdAt >= mountedAt
            )
        }
        for request in chat.messages where request.role == .user && request.stage.isPending {
            let replyID = "\(request.id)-reply-\(request.attempt)"
            guard !messageIDs.contains(replyID) else { continue }
            result.append(Row(
                id: replyID, message: nil, request: request, isNewReply: true,
                isCurrentRequest: activeRequest?.id == request.id && activeRequest?.attempt == request.attempt
            ))
        }
        return result
    }

    var body: some View {
        GeometryReader { viewport in
            ScrollView {
                ZStack(alignment: .top) {
                    // Supply a minimum viewport extent without proposing a fixed
                    // height to the lazy stack or adding trailing scrollable space.
                    Color.clear
                        .frame(height: max(0, viewport.size.height - 24))
                        .accessibilityHidden(true)
                    LazyVStack(alignment: .leading, spacing: 28) {
                        ForEach(rows) { row in
                            if row.isNewReply {
                                AssistantReplyView(
                                    model: model, replyID: row.id, isCurrentRequest: row.isCurrentRequest,
                                    message: row.message, request: row.request,
                                    reviewApproval: reviewApproval
                                )
                            } else if let message = row.message {
                                ChatMessageView(model: model, message: message)
                            }
                        }
                    }
                    .frame(maxWidth: 760)
                    .padding(.horizontal, 20)
                    .padding(.top, 24)
                    .frame(maxWidth: .infinity)
                }
            }
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(.top, for: .alignment)
            // A real content margin keeps the newest row separate from its bottom breathing room.
            .contentMargins(.bottom, 24, for: .scrollContent)
            // A semantic edge stays anchored as replies measure and stream; geometry callbacks must not drive jumps.
            .scrollPosition($scrollPosition)
            .scrollDismissesKeyboard(.interactively)
            .modifier(ChatScrollAppearance())
            .onChange(of: scrollPosition.isPositionedByUser) { _, new in
                following.positionChanged(isPositionedByUser: new)
            }
            .onScrollPhaseChange { _, new, context in
                if following.phaseChanged(
                    to: new, at: TranscriptGeometry(context.geometry),
                    isPositionedByUser: scrollPosition.isPositionedByUser
                ) {
                    anchorBottom()
                }
            }
            .accessibilityIdentifier("chatTranscript")
        }
    }

    /// Restores following once when the user scrolls back to the visible end.
    /// SwiftUI maintains that edge through later layout changes without moving fitting content below the header.
    private func anchorBottom() {
        if scrollPosition.edge != .bottom {
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { scrollPosition.scrollTo(edge: .bottom) }
        }
        following.positionChanged(isPositionedByUser: scrollPosition.isPositionedByUser)
    }
}


/// The framework's visible viewport already accounts for the composer, keyboard and other content insets.
struct TranscriptGeometry {
    let contentHeight: CGFloat
    let visibleRect: CGRect

    /// An unmeasured or fitting viewport does not require history navigation.
    var isScrollable: Bool { visibleRect.height > 0 && contentHeight > visibleRect.height }

    /// Use the actual visible end without an arbitrary near-bottom threshold.
    var isEndVisible: Bool { contentHeight <= visibleRect.maxY }
}

extension TranscriptGeometry {
    init(_ geometry: ScrollGeometry) {
        self.init(contentHeight: geometry.contentSize.height, visibleRect: geometry.visibleRect)
    }
}

/// Rearms bottom-edge following only after the user's completed scroll reaches the visible end.
/// A pending edge assignment must be acknowledged before another completed scroll can rearm it.
struct TranscriptFollowing {
    private var isAwaitingBottomPosition = false

    mutating func positionChanged(isPositionedByUser: Bool) {
        if !isPositionedByUser { isAwaitingBottomPosition = false }
    }

    mutating func phaseChanged(
        to new: ScrollPhase, at geometry: TranscriptGeometry, isPositionedByUser: Bool
    ) -> Bool {
        positionChanged(isPositionedByUser: isPositionedByUser)
        guard new == .idle, isPositionedByUser,
              geometry.isScrollable, geometry.isEndVisible,
              !isAwaitingBottomPosition else {
            return false
        }
        isAwaitingBottomPosition = true
        return true
    }
}

private struct ApprovalView: View {
    @Bindable var model: AppModel
    let approval: RunApproval
    @Environment(\.dismiss) private var dismiss
    @State private var isResponding = false

    private var isCurrentRequest: Bool {
        guard model.selectedChatID == approval.chatID,
              let current = model.pendingApproval(in: approval.chatID) else { return false }
        return current.id == approval.id && current.runID == approval.runID
            && current.chatID == approval.chatID && current.requestID == approval.requestID
            && current.command == approval.command && current.choices == approval.choices
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Label("Your permission is needed", systemImage: "hand.raised")
                        .font(.title2.weight(.semibold))
                    Text("Hermes wants to run the command below. Review it carefully before allowing it. Only approve actions you trust.")
                        .foregroundStyle(.secondary)
                    Text(approval.command)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
                    ForEach(approval.choices, id: \.self) { choice in
                        Button {
                            guard isCurrentRequest, !isResponding, !model.isStopping else { return }
                            isResponding = true
                            Task {
                                await model.respondToApproval(choice, approval: approval)
                                isResponding = false
                            }
                        } label: {
                            Text(choice.replacingOccurrences(of: "_", with: " ").capitalized)
                                .frame(maxWidth: .infinity, minHeight: 32)
                        }
                        .buttonStyle(.bordered)
                        .disabled(isResponding || model.isStopping || !isCurrentRequest)
                    }
                    if isResponding { ProgressView("Sending your decision…") }
                    Button(model.isStopping ? "Stopping…" : "Stop this request", systemImage: "stop.circle", role: .destructive) {
                        guard isCurrentRequest, !isResponding, !model.isStopping else { return }
                        model.stopRun()
                    }
                    .frame(minHeight: 44)
                    .disabled(isResponding || model.isStopping || !isCurrentRequest)
                    .accessibilityValue(model.isStopping ? "Waiting for confirmation" : "")
                    if model.isStopping {
                        Text("Waiting for Hermes to confirm the stop. Actions already taken are not undone.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(24)
            }
            .navigationTitle("Approve action")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close", systemImage: "xmark") { dismiss() }
                        .accessibilityLabel("Close approval review")
                        .accessibilityIdentifier("dismissApprovalButton")
                }
            }
        }
        .presentationDragIndicator(.visible)
        .onChange(of: isCurrentRequest, initial: true) { _, current in
            if !current { dismiss() }
        }
    }
}
