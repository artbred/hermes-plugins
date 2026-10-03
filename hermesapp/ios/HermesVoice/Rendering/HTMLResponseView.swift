import SwiftUI

/// An assistant reply shown as its own small HTML page, inline in the transcript.
///
/// Every reply — HTML, or Markdown converted by ``ResponseContent`` — renders in an isolated web view (see
/// ``ResponseWebCoordinator``): its local scripts can sort, filter and toggle, but it cannot reach the network, the app
/// or other replies, and only a tapped https link opens, in the in-app browser. The view has a transparent background,
/// follows light and dark mode and Dynamic Type, and keeps its page — and anything the person changed in it — across
/// unrelated SwiftUI updates.
///
/// A final reply taller than most of the screen starts collapsed with a control to show it in full, or in full when
/// VoiceOver is running. A streaming reply (`isStreaming`) is a static preview, updated a few times a second, that
/// shows its newest part within the same bounded height.
/// New replies can opt into `animateTyping`: trusted DOM instrumentation reveals sanitized text at a human pace,
/// including a fast final answer. The authoritative content remains untouched, and local page scripts stay disabled
/// until `onTypingFinished` lets the caller turn animation off. Cached replies keep the immediate default.
struct HTMLResponseView: View {
    private let content: String
    private let isStreaming: Bool
    private let animateTyping: Bool
    private let onTypingFinished: (@MainActor () -> Void)?
    private let onContentVisible: (@MainActor () -> Void)?
    private let memoryKey: HTMLResponseMemory.Key?

    @State private var contentHeight: CGFloat?
    @State private var isExpanded: Bool
    @State private var sizingStopped = false
    @State private var availableHeight: CGFloat?
    @State private var fallbackText: String?
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme

    /// A reply at most this much taller than the collapsed height shows in full: collapsing would hide only a few lines.
    private static let collapseAllowance: CGFloat = 160
    /// Beyond this, even an expanded reply scrolls inside its frame.
    private static let maximumInlineHeight: CGFloat = 40_000

    init(content: String, isStreaming: Bool = false, animateTyping: Bool = false, onTypingFinished: (@MainActor () -> Void)? = nil, onContentVisible: (@MainActor () -> Void)? = nil) {
        self.content = content
        self.isStreaming = isStreaming
        self.animateTyping = animateTyping
        self.onTypingFinished = onTypingFinished
        self.onContentVisible = onContentVisible
        let key = isStreaming || animateTyping ? nil : HTMLResponseMemory.Key(content)
        memoryKey = key
        let remembered: HTMLResponseMemory.Entry? = if let key { HTMLResponseMemory.entry(for: key) } else { nil }
        _contentHeight = State(initialValue: remembered?.height)
        _isExpanded = State(initialValue: remembered?.isExpanded ?? false)
    }

    var body: some View {
        Group {
            if let fallbackText {
                fallback(fallbackText)
            } else {
                rendered(layout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .onChange(of: isStreaming) { _, streaming in
            if !streaming, fallbackText != nil { onTypingFinished?() }
        }
    }

    private func rendered(_ layout: Layout) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ResponseWebView(content: content, isStreaming: isStreaming, animateTyping: animateTyping, presentation: layout.presentation) { event in
                handle(event)
            }
            .frame(maxWidth: .infinity)
            .frame(height: layout.presentation.visibleHeight)
            .overlay(alignment: .top) {
                if layout.fadesTop { edgeFade(.top) }
            }
            .overlay(alignment: .bottom) {
                if layout.presentation.isCollapsed { edgeFade(.bottom) }
            }
            if layout.presentation.isCollapsed {
                expandButton
            }
        }
    }

    private struct Layout {
        var presentation: ResponseWebPresentation
        var fadesTop = false
    }

    private var layout: Layout {
        let collapsedHeight = max(320, ((availableHeight ?? 720) * 0.66).rounded())
        let height = contentHeight ?? min(collapsedHeight, Self.estimatedHeight(of: content))
        if isStreaming || animateTyping {
            let visible = min(height, collapsedHeight)
            return Layout(presentation: ResponseWebPresentation(visibleHeight: visible), fadesTop: height > visible + 1)
        }
        if sizingStopped {
            return Layout(presentation: ResponseWebPresentation(scrollsInside: true, visibleHeight: min(height, collapsedHeight)))
        }
        if !isExpanded, !voiceOverEnabled, height > collapsedHeight + Self.collapseAllowance {
            return Layout(presentation: ResponseWebPresentation(visibleHeight: collapsedHeight, isCollapsed: true))
        }
        return Layout(presentation: ResponseWebPresentation(
            scrollsInside: height > Self.maximumInlineHeight,
            visibleHeight: min(height, Self.maximumInlineHeight)
        ))
    }

    /// A rough first height from the reply's length, used until the page reports its own.
    private static func estimatedHeight(of content: String) -> CGFloat {
        max(44, CGFloat(content.utf8.count) / 5)
    }

    private func handle(_ event: ResponseWebEvent) {
        switch event {
        case .contentHeight(let height):
            if contentHeight != height { contentHeight = height }
            if let memoryKey { HTMLResponseMemory.record(height: height, for: memoryKey) }
        case .sizingStopped:
            sizingStopped = true
        case .availableHeight(let height):
            if availableHeight != height { availableHeight = height }
        case .expansionNeeded:
            expand(animated: false)
        case .renderingFailed(let plainText):
            fallbackText = plainText
            if !plainText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { onContentVisible?() }
            if !isStreaming { onTypingFinished?() }
        case .typingFinished:
            onTypingFinished?()
        case .contentVisible:
            onContentVisible?()
        }
    }

    private func expand(animated: Bool) {
        guard !isExpanded else { return }
        withAnimation(animated && !reduceMotion ? .easeInOut(duration: 0.25) : nil) {
            isExpanded = true
        }
        if let memoryKey { HTMLResponseMemory.markExpanded(memoryKey) }
    }

    private var expandButton: some View {
        Button {
            expand(animated: true)
        } label: {
            Label("Show full reply", systemImage: "chevron.down")
                .font(.subheadline.weight(.semibold))
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .accessibilityHint("Shows the rest of this reply")
    }

    /// Fades the clipped edge into the transcript background.
    private func edgeFade(_ edge: VerticalEdge) -> some View {
        let background = HermesPalette.background(colorScheme)
        return LinearGradient(
            colors: [background.opacity(0), background],
            startPoint: edge == .top ? .bottom : .top,
            endPoint: edge == .top ? .top : .bottom
        )
        .frame(height: 56)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func fallback(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !text.isEmpty {
                Text(text)
                    .textSelection(.enabled)
                    .lineSpacing(5)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Label("This reply’s formatting couldn’t be shown.", systemImage: "exclamationmark.triangle")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Reload formatting") {
                fallbackText = nil
                contentHeight = nil
                sizingStopped = false
            }
            .frame(minHeight: 44)
            .accessibilityHint("Recreates this reply’s interface. Local controls will reset.")
        }
    }
}

/// Measured heights and expansion of final replies, kept for the life of the process. The transcript's lazy stack
/// recreates rows scrolled far away; a recreated reply starts at its real height and expansion instead of jumping.
@MainActor
enum HTMLResponseMemory {
    /// Identifies a reply by its length and the start and end of its text, without hashing all of it on every update.
    /// A collision only costs one reply a wrong first height.
    struct Key: Hashable {
        private let length: Int
        private let fingerprint: Int

        init(_ content: String) {
            let bytes = content.utf8
            var hasher = Hasher()
            for byte in bytes.prefix(512) { hasher.combine(byte) }
            for byte in bytes.suffix(512) { hasher.combine(byte) }
            length = bytes.count
            fingerprint = hasher.finalize()
        }
    }

    struct Entry {
        var height: CGFloat?
        var isExpanded = false
    }

    private static let capacity = 300
    private static var entries: [Key: Entry] = [:]
    private static var order: [Key] = []

    static func entry(for key: Key) -> Entry? {
        entries[key]
    }

    static func record(height: CGFloat, for key: Key) {
        update(key) { $0.height = height }
    }

    static func markExpanded(_ key: Key) {
        update(key) { $0.isExpanded = true }
    }

    private static func update(_ key: Key, _ change: (inout Entry) -> Void) {
        if entries[key] == nil {
            order.append(key)
            if order.count > capacity {
                entries[order.removeFirst()] = nil
            }
        }
        change(&entries[key, default: Entry()])
    }
}
