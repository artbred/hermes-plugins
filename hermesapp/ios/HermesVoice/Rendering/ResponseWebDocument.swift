import Foundation
import Network
import WebKit

/// The HTML document around a reply fragment. Its policy tags come before anything from the reply, so no reply markup
/// can precede them, and a policy a reply adds could only restrict it further: browsers enforce every policy present.
@MainActor
enum ResponseWebDocument {
    enum Mode: String {
        /// A provisional snapshot of a reply still streaming in: static HTML, page scripts disabled.
        case streaming = "stream"
        /// The finished reply, with its local scripts.
        case finished = "final"
    }

    /// Nothing may load or connect: only inline CSS and JavaScript for local interactivity, and inline `data:` images
    /// and fonts. No network, frames, workers, plugins, media, base URL or form submission.
    static let contentSecurityPolicy = [
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "font-src data:",
        "media-src 'none'",
        "connect-src 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "worker-src 'none'",
        "object-src 'none'",
        "manifest-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ].joined(separator: "; ")

    private static let head = """
    <head>\
    <meta charset="utf-8">\
    <meta http-equiv="Content-Security-Policy" content="\(contentSecurityPolicy)">\
    <meta http-equiv="x-dns-prefetch-control" content="off">\
    <meta name="referrer" content="no-referrer">\
    <meta name="viewport" content="width=device-width, initial-scale=1, minimum-scale=1, maximum-scale=1">\
    <meta name="color-scheme" content="light dark">\
    <style>\(ResponseWebStyles.stylesheet(.resolve()))</style>\
    </head>
    """

    /// - Parameter generation: Stamped on `<html>` for the trusted instrumentation to include in every message.
    static func html(fragment: String, generation: Int, mode: Mode, animateTyping: Bool = false) -> String {
        // Animated documents start empty. The trusted ready handshake installs the masked fragment before reveal.
        "<!DOCTYPE html><html data-hermes-generation=\"\(generation)\" data-hermes-mode=\"\(mode.rawValue)\">"
            + head
            + "<body><div id=\"hermes-root\" dir=\"auto\">"
            + (animateTyping ? "" : fragment)
            + "</div></body></html>"
    }
}

/// The isolated web environment every reply renders in.
@MainActor
enum ResponseWebEnvironment {
    /// One non-persistent store shared by reply views: nothing a reply does reaches disk, and it holds nothing of the
    /// app's own networking (no API credentials or cookies). Each reply document still gets its own in-memory local
    /// and session storage (``ResponseWebScripts/pageEnvironment``), so replies share no state through it.
    static let dataStore: WKWebsiteDataStore = {
        let store = WKWebsiteDataStore.nonPersistent()
        // Replies never need the network, and the content security policy does not govern every connection WebKit can
        // open for a page, such as link preconnect hints. Any such connection is sent to a closed loopback port.
        var proxy = ProxyConfiguration(httpCONNECTProxy: .hostPort(host: "127.0.0.1", port: 9))
        proxy.allowFailover = false
        store.proxyConfigurations = [proxy]
        return store
    }()

    static func makeConfiguration(messageHandler: any WKScriptMessageHandler) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        configuration.suppressesIncrementalRendering = true
        configuration.dataDetectorTypes = []
        configuration.allowsInlineMediaPlayback = false
        configuration.allowsAirPlayForMediaPlayback = false
        configuration.allowsPictureInPictureMediaPlayback = false
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.preferences.isElementFullscreenEnabled = false
        configuration.preferences.inactiveSchedulingPolicy = .suspend
        configuration.defaultWebpagePreferences.preferredContentMode = .mobile
        if #available(iOS 26.4, *) {
            // Reply scripts are untrusted: run them without JIT in hardened content processes.
            configuration.defaultWebpagePreferences.securityRestrictionMode = .maximizeCompatibility
        }

        let content = configuration.userContentController
        content.addUserScript(WKUserScript(
            source: ResponseWebScripts.pageEnvironment,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false,
            in: .page
        ))
        content.addUserScript(WKUserScript(
            source: ResponseWebScripts.instrumentation,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true,
            in: .defaultClient
        ))
        content.add(messageHandler, contentWorld: .defaultClient, name: ResponseWebScripts.handlerName)
        return configuration
    }
}
