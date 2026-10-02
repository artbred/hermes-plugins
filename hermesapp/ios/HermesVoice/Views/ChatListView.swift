import SwiftUI

struct ChatListView: View {
    @Bindable var model: AppModel
    let close: () -> Void
    let openSettings: () -> Void
    @Environment(\.colorScheme) private var colorScheme
    @State private var search = ""
    @State private var chatToDelete: Chat?
    @State private var readableReplies: [String: ReadableReply] = [:]
    @State private var indexingReplies = false

    private struct ReadableReply: Sendable {
        let source: String
        let text: String
    }

    private var chats: [Chat] {
        let ordered = model.store.chats.filter { !$0.messages.isEmpty }
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return ordered }
        return ordered.filter { chat in
            chat.title.localizedCaseInsensitiveContains(query)
                || chat.messages.contains {
                    ($0.role == .assistant ? readableReplies[$0.id]?.text ?? "" : $0.text).localizedCaseInsensitiveContains(query)
                        || $0.files.contains { $0.name.localizedCaseInsensitiveContains(query) }
                }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Text("Hermes")
                    .font(.title2.weight(.medium))
                Spacer(minLength: 8)
                Button(action: close) {
                    Image(systemName: "xmark")
                        .font(.body.weight(.medium))
                        .frame(width: 44, height: 44)
                }
                .accessibilityLabel("Close chats")
                .accessibilityIdentifier("closeChatsButton")
            }
            .padding(.top, 8)
            .padding(.horizontal, 20)

            HStack(spacing: 12) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search chats", text: $search)
                    .submitLabel(.search)
                    .accessibilityIdentifier("chatSearchField")
                if !search.isEmpty {
                    Button { search = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                            .frame(width: 44, height: 44)
                    }
                    .accessibilityLabel("Clear search")
                }
            }
            .frame(minHeight: 44)
            .padding(.horizontal, 24)

            VStack(spacing: 0) {
                HStack {
                    Text("Recents")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        Task { await model.refreshChats() }
                    } label: {
                        Group {
                            if model.isRefreshing {
                                ProgressView().controlSize(.small)
                            } else {
                                Image(systemName: "arrow.clockwise")
                            }
                        }
                        .frame(width: 44, height: 44)
                    }
                    .disabled(model.isRefreshing)
                    .accessibilityLabel("Refresh history")
                    .accessibilityIdentifier("refreshChatsButton")
                }
                .padding(.leading, 24)
                .padding(.trailing, 16)

                List {
                    ForEach(chats) { chat in
                        Button {
                            close()
                            model.selectChat(chat.id)
                        } label: {
                            row(chat)
                        }
                        .buttonStyle(.plain)
                        .disabled(model.recorder.isRecording)
                        .accessibilityHint("Continues this conversation")
                        .accessibilityIdentifier("chatRow-\(chat.id)")
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                        .listRowInsets(EdgeInsets(top: 3, leading: 12, bottom: 3, trailing: 12))
                        .swipeActions {
                            Button("Delete", systemImage: "trash", role: .destructive) {
                                chatToDelete = chat
                            }
                            .disabled(model.recorder.isRecording)
                        }
                        .contextMenu {
                            Button("Delete from iPhone", systemImage: "trash", role: .destructive) {
                                chatToDelete = chat
                            }
                            .disabled(model.recorder.isRecording)
                        }
                    }
                    if indexingReplies && !search.isEmpty {
                        ProgressView("Preparing search…")
                            .listRowBackground(Color.clear)
                    } else if chats.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(search.isEmpty ? "Your chats start here" : "No matching chats")
                                .font(.body.weight(.medium))
                            Text(search.isEmpty ? "Start a conversation, or pull down to load your Hermes history." : "Try a different title, message, or file name.")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 16)
                        .listRowSeparator(.hidden)
                        .listRowBackground(Color.clear)
                    }
                    if let message = model.connectionMessage {
                        Label(message, systemImage: "network")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .listRowSeparator(.hidden)
                            .listRowBackground(Color.clear)
                    }
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await model.refreshChats() }
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            Button(action: openSettings) {
                Label("Settings", systemImage: "gearshape")
                    .font(.body)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 12)
                    .background(HermesPalette.menu(colorScheme))
            }
            .accessibilityIdentifier("settingsButton")
            .accessibilityHint("Opens connection settings")
        }
        .tint(.primary)
        .confirmationDialog(
            "Delete this chat from your iPhone?",
            isPresented: Binding(get: { chatToDelete != nil }, set: { if !$0 { chatToDelete = nil } }),
            titleVisibility: .visible,
            presenting: chatToDelete
        ) { chat in
            Button("Delete Chat", role: .destructive) { model.deleteChat(chat.id) }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("This removes the local conversation, audio, and attachments. It does not delete the session on your Hermes server.")
        }
        .accessibilityElement(children: .contain)
        .task(id: model.store.chats) {
            let messages = model.store.chats.flatMap(\.messages).filter { $0.role == .assistant }
            let cached = readableReplies
            indexingReplies = true
            let worker = Task.detached(priority: .userInitiated) {
                var next: [String: ReadableReply] = [:]
                next.reserveCapacity(messages.count)
                for message in messages {
                    guard !Task.isCancelled else { return next }
                    if let saved = cached[message.id], saved.source == message.text {
                        next[message.id] = saved
                    } else {
                        next[message.id] = ReadableReply(source: message.text, text: ResponseContent(raw: message.text).plainText)
                    }
                }
                return next
            }
            let next = await withTaskCancellationHandler {
                await worker.value
            } onCancel: {
                worker.cancel()
            }
            guard !Task.isCancelled else { return }
            readableReplies = next
            indexingReplies = false
        }
    }

    private func row(_ chat: Chat) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text(chat.title)
                    .font(.body)
                    .lineLimit(2)
                    .foregroundStyle(.primary)
                if let last = chat.messages.last {
                    Text(last.text.isEmpty ? (last.files.first?.name ?? "Voice message") : (last.role == .assistant ? readableReplies[last.id]?.text ?? "Formatting reply…" : last.text))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if let activity = activity(for: chat) {
                    Label(activity.title, systemImage: activity.symbol)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            if chat.id == model.selectedChatID {
                Image(systemName: "checkmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Current chat")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            chat.id == model.selectedChatID ? HermesPalette.control(colorScheme) : .clear,
            in: RoundedRectangle(cornerRadius: 18)
        )
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private func activity(for chat: Chat) -> (title: String, symbol: String)? {
        if model.pendingApproval(in: chat.id) != nil {
            return ("Needs approval", "hand.raised")
        }
        guard let pending = chat.messages.first(where: { $0.role == .user && $0.stage.isPending }) else { return nil }
        if pending.stopRequested == true {
            return pending.error == nil
                ? ("Stopping…", "stop.circle")
                : ("Stop not confirmed", "exclamationmark.circle")
        }
        return ("In progress", "ellipsis.circle")
    }
}
