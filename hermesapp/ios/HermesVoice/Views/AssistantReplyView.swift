import SwiftUI

/// The pending request and its authoritative reply share one transcript identity and renderer.
struct AssistantReplyView: View {
    @Bindable var model: AppModel
    let replyID: String
    let isCurrentRequest: Bool
    let message: ChatMessage?
    let request: ChatMessage?
    let reviewApproval: (RunApproval) -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled
    @State private var typingFinished = false
    @State private var contentVisible = false

    private var content: String {
        if let message { return message.text }
        return isCurrentRequest ? model.liveResponse : ""
    }

    private var animateTyping: Bool {
        !reduceMotion && !voiceOverEnabled && !typingFinished
    }

    private var renderedMessage: ChatMessage {
        message ?? ChatMessage(id: replyID, role: .assistant, text: "", stage: .completed)
    }

    private var stoppingDetail: String? {
        guard isCurrentRequest, let request else { return nil }
        if model.isStopping {
            if request.stage == .savingMemory {
                return "The save is already in progress. Waiting to confirm whether your thought was saved."
            }
            if request.runID != nil || request.stage == .submitting {
                return "Waiting for Hermes to confirm the stop. Actions already taken are not undone."
            }
            return "Cancelling this request…"
        }
        if request.stopRequested == true {
            return "Stop not confirmed. Hermes may still be working. Use Stop to try again."
        }
        return nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ChatMessageView(
                model: model,
                message: renderedMessage,
                responseContent: content,
                isResponseStreaming: message == nil,
                animateResponseTyping: animateTyping,
                onResponseTypingFinished: { typingFinished = true },
                onResponseContentVisible: { contentVisible = true },
                showsActions: message != nil && !animateTyping
            )
            .frame(minHeight: (message == nil || animateTyping) && !contentVisible ? 22 : 0)
            .overlay(alignment: .topLeading) {
                if (message == nil || animateTyping) && !contentVisible && !typingFinished {
                    waitingDots
                }
            }
            if let stoppingDetail {
                Text(stoppingDetail)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("stopRequestStatus")
            }
            if isCurrentRequest, let approval = model.approval, !model.isStopping {
                Button("Review request") { reviewApproval(approval) }
                    .font(.subheadline.weight(.semibold))
                    .buttonStyle(.bordered)
                    .frame(minHeight: 44)
                    .accessibilityHint("Opens the pending command. Closing the review leaves your decision pending.")
                    .accessibilityIdentifier("reviewApprovalButton")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
    }

    private var waitingDots: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { _ in
                Circle().frame(width: 5, height: 5)
            }
        }
        .foregroundStyle(.secondary)
        .frame(height: 22)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Waiting for reply")
        .accessibilityIdentifier("waitingReply-\(replyID)")
    }
}
