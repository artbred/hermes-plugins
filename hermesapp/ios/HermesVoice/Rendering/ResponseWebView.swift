import SwiftUI
import UIKit
import WebKit

/// How the reply's web view sits in its SwiftUI frame.
struct ResponseWebPresentation: Equatable {
    /// Whether the page scrolls inside its frame. Normally the frame fits the content and the transcript scrolls; only
    /// a reply taller than any inline frame, or one whose height would not settle, scrolls inside.
    var scrollsInside = false
    /// The height of the visible frame, in points.
    var visibleHeight: CGFloat = 0
    /// Whether the frame shows only the start of a taller reply until the person expands it.
    var isCollapsed = false
}

/// What the web view reports to ``HTMLResponseView``.
enum ResponseWebEvent {
    /// The rendered content's height changed.
    case contentHeight(CGFloat)
    /// The content kept growing with its frame; its height is no longer followed.
    case sizingStopped
    /// The window height available for replies, which bounds inline frames.
    case availableHeight(CGFloat)
    /// A tapped `#fragment` link targets content below the collapsed frame.
    case expansionNeeded
    /// The web content process ended; keep the response readable and let the user explicitly reload its formatting.
    case renderingFailed(plainText: String)
    /// The latest authoritative final snapshot has been fully revealed.
    case typingFinished
    case contentVisible
}

/// A reply document in an isolated, non-persistent `WKWebView`. See ``ResponseWebCoordinator`` for its boundaries.
struct ResponseWebView: UIViewRepresentable {
    let content: String
    let isStreaming: Bool
    var animateTyping = false
    let presentation: ResponseWebPresentation
    let onEvent: @MainActor (ResponseWebEvent) -> Void

    func makeCoordinator() -> ResponseWebCoordinator {
        ResponseWebCoordinator()
    }

    func makeUIView(context: Context) -> ResponseWebContainerView {
        context.coordinator.makeContainer()
    }

    func updateUIView(_ container: ResponseWebContainerView, context: Context) {
        let coordinator = context.coordinator
        coordinator.onEvent = onEvent
        coordinator.present(presentation)
        coordinator.submit(content, isStreaming: isStreaming, animateTyping: animateTyping)
    }

    static func dismantleUIView(_ container: ResponseWebContainerView, coordinator: ResponseWebCoordinator) {
        coordinator.tearDown()
    }
}

/// Hosts the web view and reports the window changes the coordinator needs.
final class ResponseWebContainerView: UIView {
    let webView: WKWebView
    var onAvailableHeightChange: ((CGFloat) -> Void)?
    var onLayout: (() -> Void)?
    private var availableHeight: CGFloat?

    init(webView: WKWebView) {
        self.webView = webView
        super.init(frame: .zero)
        backgroundColor = .clear
        isOpaque = false
        clipsToBounds = true
        webView.frame = bounds
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        addSubview(webView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) is not supported")
    }

    /// The nearest scroll view around the reply, normally the transcript.
    var enclosingScrollView: UIScrollView? {
        var view = superview
        while let current = view {
            if let scrollView = current as? UIScrollView { return scrollView }
            view = current.superview
        }
        return nil
    }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        reportAvailableHeight()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        reportAvailableHeight()
        onLayout?()
    }

    private func reportAvailableHeight() {
        guard let window else { return }
        let height = (window.bounds.height - window.safeAreaInsets.top - window.safeAreaInsets.bottom).rounded()
        guard height > 0, height != availableHeight else { return }
        availableHeight = height
        onAvailableHeightChange?(height)
    }
}

/// Renders one reply and enforces the boundaries around it.
///
/// - Parsing: ``ResponseContent`` runs off the main actor. Final replies render once; streaming snapshots are
///   throttled and applied in place, without reloading the document, and run no page scripts.
/// - Isolation: a non-persistent store with no app credentials, a document-level content security policy that allows
///   no loads or connections, and navigation limited to the reply document and its `#fragments`. No popups, forms,
///   downloads, file pickers, camera, microphone, motion or location access.
/// - App messages: one handler, only in `WKContentWorld.defaultClient`. Messages must come from the main frame of this
///   web view and the current document generation, within a budget. Links open only from real taps, only for
///   https, and only in the in-app Safari view.
@MainActor
final class ResponseWebCoordinator: NSObject {
    var onEvent: (@MainActor (ResponseWebEvent) -> Void)?

    private static let messageBudget = 400
    private static let maximumContentHeight: CGFloat = 100_000
    private static let growthWindow: Duration = .milliseconds(1_500)
    private static let maximumGrowthSteps = 30
    private static let linkInterval: Duration = .milliseconds(700)

    private weak var container: ResponseWebContainerView?
    private var webView: WKWebView? { container?.webView }

    // Rendering: the latest content SwiftUI passed and the snapshot now shown.
    private var submitted: (content: String, isStreaming: Bool, animateTyping: Bool, revision: Int)?
    private var revision = 0
    private var renderTask: Task<Void, Never>?
    private var lastRenderStart: ContinuousClock.Instant?
    private var rendered: (source: String, isStreaming: Bool, animateTyping: Bool, revision: Int)?
    private var payload: ResponseRenderPayload?

    // The loaded document.
    private var generation = 0
    private var mode: ResponseWebDocument.Mode?
    private var navigation: WKNavigation?
    private var pendingDocumentLoads = 0
    private var allowsPageScripts = false
    private var isDocumentReady = false
    private var pendingFragment: ResponseRenderPayload?
    private var completedTypingRevision: Int?

    // Sizing.
    private var presentation = ResponseWebPresentation()
    private var messagesLeft = ResponseWebCoordinator.messageBudget
    private var lastHeight: CGFloat?
    private var growth: [ContinuousClock.Instant] = []
    private var isSizingStopped = false
    private var pendingAnchor: (top: CGFloat, expires: ContinuousClock.Instant)?
    private var lastLinkOpen: ContinuousClock.Instant?

    func makeContainer() -> ResponseWebContainerView {
        let configuration = ResponseWebEnvironment.makeConfiguration(messageHandler: ResponseScriptMessageProxy(target: self))
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.underPageBackgroundColor = .clear
        webView.allowsBackForwardNavigationGestures = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        #if DEBUG
        webView.isInspectable = true
        #endif
        let scrollView = webView.scrollView
        scrollView.backgroundColor = .clear
        scrollView.isScrollEnabled = false
        scrollView.bounces = false
        scrollView.alwaysBounceVertical = false
        scrollView.alwaysBounceHorizontal = false
        scrollView.showsVerticalScrollIndicator = false
        scrollView.showsHorizontalScrollIndicator = false
        // The transcript owns scroll-to-top and safe areas.
        scrollView.scrollsToTop = false
        scrollView.contentInsetAdjustmentBehavior = .never
        scrollView.minimumZoomScale = 1
        scrollView.maximumZoomScale = 1
        scrollView.bouncesZoom = false

        let container = ResponseWebContainerView(webView: webView)
        container.onAvailableHeightChange = { [weak self] height in self?.availableHeightChanged(height) }
        container.onLayout = { [weak self] in self?.containerDidLayout() }
        self.container = container
        return container
    }

    func present(_ presentation: ResponseWebPresentation) {
        guard presentation != self.presentation else { return }
        let scrolledInside = self.presentation.scrollsInside
        self.presentation = presentation
        guard let scrollView = webView?.scrollView else { return }
        scrollView.isScrollEnabled = presentation.scrollsInside
        scrollView.showsVerticalScrollIndicator = presentation.scrollsInside
        if scrolledInside, !presentation.scrollsInside, mode == .finished {
            scrollView.setContentOffset(.zero, animated: false)
        }
    }

    /// Called on every SwiftUI update; renders only when the reply or its streaming state changed.
    func submit(_ content: String, isStreaming: Bool, animateTyping: Bool = false) {
        if let submitted, submitted.isStreaming == isStreaming, submitted.animateTyping == animateTyping, submitted.content == content { return }
        revision += 1
        submitted = (content, isStreaming, animateTyping, revision)
        guard renderTask == nil else { return }
        renderTask = Task { [weak self] in await self?.renderLatest() }
    }

    func tearDown() {
        renderTask?.cancel()
        renderTask = nil
        generation += 1
        submitted = nil
        guard let webView else { return }
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        webView.configuration.userContentController.removeAllScriptMessageHandlers()
    }

    // MARK: Rendering

    /// Parses and shows the latest submitted content, then anything submitted meanwhile. A streaming reply updates at
    /// most once per interval, however often tokens arrive.
    private func renderLatest() async {
        defer { renderTask = nil }
        while let latest = submitted, !Task.isCancelled {
            if latest.isStreaming, let lastRenderStart {
                let wait = Self.streamingInterval(forLength: latest.content.utf8.count) - lastRenderStart.duration(to: ContinuousClock.now)
                if wait > .zero {
                    try? await Task.sleep(for: wait)
                    continue
                }
            }
            let source = latest.isStreaming ? String(ResponseStreamingText.stablePrefix(of: latest.content)) : latest.content
            if let rendered, rendered.isStreaming == latest.isStreaming, rendered.animateTyping == latest.animateTyping, rendered.source == source {
                if isCurrent(latest) { return }
                continue
            }
            lastRenderStart = ContinuousClock.now
            let isStreaming = latest.isStreaming
            let animateTyping = latest.animateTyping
            let revision = latest.revision
            let parsed = await Task.detached(priority: .userInitiated) {
                ResponseRenderPayload(source: source, isStreaming: isStreaming, animateTyping: animateTyping, revision: revision)
            }.value
            guard !Task.isCancelled, isCurrent(latest) else { continue }
            rendered = (source, isStreaming, animateTyping, revision)
            show(parsed)
            if isCurrent(latest) { return }
        }
    }

    private func isCurrent(_ snapshot: (content: String, isStreaming: Bool, animateTyping: Bool, revision: Int)) -> Bool {
        guard let submitted else { return true }
        return submitted.revision == snapshot.revision
    }

    /// Longer replies take longer to parse and lay out, so they update less often while streaming.
    private static func streamingInterval(forLength length: Int) -> Duration {
        .milliseconds(min(1_000, 300 + length / 60))
    }

    private func show(_ payload: ResponseRenderPayload) {
        self.payload = payload
        if payload.isStreaming || payload.animateTyping, mode == .streaming {
            if isDocumentReady {
                replaceFragment(payload)
            } else {
                pendingFragment = payload
            }
        } else {
            load(payload)
        }
    }

    private func load(_ payload: ResponseRenderPayload) {
        guard let webView else { return }
        let mode: ResponseWebDocument.Mode = payload.isStreaming || payload.animateTyping ? .streaming : .finished
        generation += 1
        self.mode = mode
        allowsPageScripts = mode == .finished
        isDocumentReady = false
        pendingFragment = payload.animateTyping ? payload : nil
        messagesLeft = Self.messageBudget
        lastHeight = nil
        growth.removeAll()
        pendingDocumentLoads += 1
        let document = ResponseWebDocument.html(fragment: payload.html, generation: generation, mode: mode, animateTyping: payload.animateTyping)
        navigation = webView.loadHTMLString(document, baseURL: nil)
    }

    private func replaceFragment(_ payload: ResponseRenderPayload) {
        guard let webView else { return }
        messagesLeft = Self.messageBudget
        growth.removeAll()
        let expected = generation
        webView.callAsyncJavaScript(
            "return globalThis.__hermesResponse ? globalThis.__hermesResponse.render(html, animateTyping, isFinal, revision) : false",
            arguments: ["html": payload.html, "animateTyping": payload.animateTyping, "isFinal": !payload.isStreaming, "revision": payload.revision],
            in: nil,
            in: .defaultClient
        ) { [weak self] result in
            guard let self, self.generation == expected else { return }
            if case .success(let value) = result, value as? Bool == true { return }
            // The document could not take the update in place: load the latest snapshot whole.
            if let payload = self.payload { self.load(payload) }
        }
    }

    private func documentBecameReady() {
        guard !isDocumentReady else { return }
        isDocumentReady = true
        if let pendingFragment {
            self.pendingFragment = nil
            replaceFragment(pendingFragment)
        }
    }

    /// Reported during layout; the SwiftUI state change waits for the next turn.
    private func availableHeightChanged(_ height: CGFloat) {
        Task { @MainActor [weak self] in self?.onEvent?(.availableHeight(height)) }
    }

    // MARK: Messages

    fileprivate func receive(_ message: WKScriptMessage) {
        guard message.name == ResponseWebScripts.handlerName,
              message.world === WKContentWorld.defaultClient,
              message.frameInfo.isMainFrame,
              let webView, message.webView === webView,
              let body = message.body as? [String: Any],
              body["generation"] as? Int == generation,
              let type = body["type"] as? String,
              messagesLeft > 0
        else { return }
        messagesLeft -= 1
        switch type {
        case "ready":
            documentBecameReady()
        case "size":
            if let height = body["height"] as? Double { receiveHeight(height) }
        case "link":
            if let href = body["href"] as? String { openLink(href) }
        case "anchor":
            if let top = body["top"] as? Double { revealAnchor(at: top) }
        case "typingFinished":
            guard let revision = body["revision"] as? Int,
                  let submitted, submitted.animateTyping, !submitted.isStreaming,
                  submitted.revision == revision, completedTypingRevision != revision else { return }
            completedTypingRevision = revision
            onEvent?(.typingFinished)
        case "contentVisible":
            onEvent?(.contentVisible)
        default:
            break
        }
    }

    private func receiveHeight(_ value: Double) {
        guard !isSizingStopped, value.isFinite, value >= 0 else { return }
        let height = CGFloat(min(value, Double(Self.maximumContentHeight))).rounded(.up)
        guard height != lastHeight else { return }
        if let lastHeight, height > lastHeight, submitted?.animateTyping != true {
            // Content whose height follows its frame (viewport units, for one) grows on every resize. Stop following
            // it rather than resize forever.
            let now = ContinuousClock.now
            growth.append(now)
            growth.removeAll { now - $0 > Self.growthWindow }
            if growth.count >= Self.maximumGrowthSteps {
                isSizingStopped = true
                onEvent?(.sizingStopped)
                return
            }
        }
        lastHeight = height
        onEvent?(.contentHeight(height))
    }

    private func openLink(_ href: String) {
        guard let container, container.window != nil else { return }
        let now = ContinuousClock.now
        if let lastLinkOpen, now - lastLinkOpen < Self.linkInterval { return }
        lastLinkOpen = now
        ResponseLinkPresenter.handle(ResponseLinkPolicy.action(for: href), from: container)
    }

    // MARK: Fragments

    private func revealAnchor(at value: Double) {
        guard mode == .finished, value.isFinite, value >= 0, let webView else { return }
        let top = CGFloat(value)
        if presentation.scrollsInside {
            let scrollView = webView.scrollView
            let end = max(0, scrollView.contentSize.height - scrollView.bounds.height)
            scrollView.setContentOffset(CGPoint(x: scrollView.contentOffset.x, y: min(top, end)), animated: !UIAccessibility.isReduceMotionEnabled)
            scrollTranscript(toShow: 0)
        } else if presentation.isCollapsed, top > presentation.visibleHeight - 44 {
            pendingAnchor = (top: top, expires: ContinuousClock.now + .seconds(2))
            onEvent?(.expansionNeeded)
        } else {
            scrollTranscript(toShow: top)
        }
    }

    private func containerDidLayout() {
        guard let anchor = pendingAnchor, let container else { return }
        guard ContinuousClock.now < anchor.expires else {
            pendingAnchor = nil
            return
        }
        guard container.bounds.height > anchor.top else { return }
        pendingAnchor = nil
        Task { @MainActor [weak self] in self?.scrollTranscript(toShow: anchor.top) }
    }

    /// Scrolls the transcript so the point `top` of the reply sits near the top of the screen.
    private func scrollTranscript(toShow top: CGFloat) {
        guard let container, let scrollView = container.enclosingScrollView else { return }
        let target = container.convert(CGPoint(x: 0, y: top), to: scrollView).y
        let inset = scrollView.adjustedContentInset
        let lowest = -inset.top
        let highest = max(lowest, scrollView.contentSize.height + inset.bottom - scrollView.bounds.height)
        let offset = min(max(target - inset.top - 16, lowest), highest)
        scrollView.setContentOffset(CGPoint(x: scrollView.contentOffset.x, y: offset), animated: !UIAccessibility.isReduceMotionEnabled)
    }
}

// MARK: - Navigation

extension ResponseWebCoordinator: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        preferences: WKWebpagePreferences,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy, WKWebpagePreferences) -> Void
    ) {
        let decision = ResponseNavigationPolicy.decide(
            url: navigationAction.request.url,
            targetsMainFrame: navigationAction.targetFrame?.isMainFrame == true,
            isExpectingDocument: pendingDocumentLoads > 0,
            currentURL: webView.url,
            requestsDownload: navigationAction.shouldPerformDownload
        )
        switch decision {
        case .loadDocument:
            pendingDocumentLoads = max(0, pendingDocumentLoads - 1)
            preferences.allowsContentJavaScript = allowsPageScripts
            decisionHandler(.allow, preferences)
        case .followFragment:
            decisionHandler(.allow, preferences)
        case .cancel:
            decisionHandler(.cancel, preferences)
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationResponsePolicy) -> Void
    ) {
        decisionHandler(navigationResponse.isForMainFrame && navigationResponse.canShowMIMEType ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        pendingDocumentLoads = 0
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // The instrumentation reports readiness itself; this covers a document where it could not run.
        if navigation === self.navigation { documentBecameReady() }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        isDocumentReady = false
        renderTask?.cancel()
        renderTask = nil
        onEvent?(.renderingFailed(plainText: payload?.plainText ?? ""))
    }
}

// MARK: - Page UI requests

extension ResponseWebCoordinator: WKUIDelegate {
    /// No popups or new windows. Link taps reach the app through the trusted instrumentation instead.
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        nil
    }

    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void
    ) {
        decisionHandler(.deny)
    }

    func webView(
        _ webView: WKWebView,
        requestDeviceOrientationAndMotionPermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void
    ) {
        decisionHandler(.deny)
    }

    @available(iOS 27.0, *)
    func webView(
        _ webView: WKWebView,
        requestGeolocationPermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void
    ) {
        decisionHandler(.deny)
    }

    @available(iOS 18.4, *)
    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void
    ) {
        completionHandler(nil)
    }

    /// Long-press on a link: the app's own menu, without a page preview, which would load the site.
    func webView(
        _ webView: WKWebView,
        contextMenuConfigurationForElement elementInfo: WKContextMenuElementInfo,
        completionHandler: @escaping @MainActor @Sendable (UIContextMenuConfiguration?) -> Void
    ) {
        completionHandler(ResponseLinkPresenter.contextMenu(for: elementInfo.linkURL, from: webView))
    }
}

// MARK: - Script messages

/// The user content controller retains its handlers; this keeps it from retaining the coordinator and its web view.
@MainActor
private final class ResponseScriptMessageProxy: NSObject, WKScriptMessageHandler {
    private weak var target: ResponseWebCoordinator?

    init(target: ResponseWebCoordinator) {
        self.target = target
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.receive(message)
    }
}

/// One parsed snapshot of a reply, made off the main actor.
struct ResponseRenderPayload: Sendable {
    let html: String
    let plainText: String
    let isStreaming: Bool
    let animateTyping: Bool
    let revision: Int

    init(source: String, isStreaming: Bool, animateTyping: Bool = false, revision: Int = 0) {
        let content = ResponseContent(raw: source)
        html = content.html
        plainText = content.plainText
        self.isStreaming = isStreaming
        self.animateTyping = animateTyping
        self.revision = revision
    }
}
