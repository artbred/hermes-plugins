import Foundation
import Testing
@testable import HermesVoice

@Suite("Rendered reply boundaries")
struct ResponseWebPolicyTests {
    @Test("A tapped https link opens in the app, whatever its case or surrounding whitespace")
    func httpsOpensInApp() throws {
        let page = try #require(URL(string: "https://example.com/docs?q=1#usage"))
        #expect(ResponseLinkPolicy.action(for: "https://example.com/docs?q=1#usage") == .openInApp(page))
        let shouted = try #require(URL(string: "HTTPS://Example.com/a"))
        #expect(ResponseLinkPolicy.action(for: "  HTTPS://Example.com/a\n") == .openInApp(shouted))
    }

    @Test("Web links that would drop encryption or disguise their host do not open")
    func misleadingWebLinks() {
        #expect(ResponseLinkPolicy.action(for: "http://example.com/a") == .blocked(.init(
            reason: .insecure, destination: "http://example.com/a", copyableText: "http://example.com/a"
        )))
        for address in ["https://bank.example@evil.example/login", "https://user:pass@example.com", "https:///no-host", "https:relative"] {
            guard case .blocked(let link) = ResponseLinkPolicy.action(for: address) else {
                Issue.record("\(address) must not open")
                continue
            }
            #expect(link.reason == .unsafe)
            #expect(link.copyableText == nil)
        }
    }

    @Test("Mail, phone and app links never leave Hermes; a contact address can be copied instead")
    func otherAppsStayClosed() {
        #expect(ResponseLinkPolicy.action(for: "mailto:hi@example.com?subject=Plan") == .blocked(.init(
            reason: .otherApp, destination: "mailto:hi@example.com?subject=Plan", copyableText: "hi@example.com"
        )))
        #expect(ResponseLinkPolicy.action(for: "tel:+1%20555%200100") == .blocked(.init(
            reason: .otherApp, destination: "tel:+1%20555%200100", copyableText: "+1 555 0100"
        )))
        #expect(ResponseLinkPolicy.action(for: "hermesvoice://send?text=hi") == .blocked(.init(
            reason: .otherApp, destination: "hermesvoice://send?text=hi", copyableText: nil
        )))
    }

    @Test("Script links do nothing; data, file and blob documents are blocked without a copy")
    func documentAddresses() {
        #expect(ResponseLinkPolicy.action(for: "javascript:alert(1)") == .ignore)
        #expect(ResponseLinkPolicy.action(for: " JavaScript:void(0)") == .ignore)
        for address in ["data:text/html,<script>alert(1)</script>", "file:///etc/hosts", "blob:null/5b2c", "view-source:https://example.com"] {
            guard case .blocked(let link) = ResponseLinkPolicy.action(for: address) else {
                Issue.record("\(address) must be blocked")
                continue
            }
            #expect(link.reason == .unsafe)
            #expect(link.copyableText == nil)
        }
        let long = "https://example.com/" + String(repeating: "a", count: ResponseLinkPolicy.maximumLength)
        guard case .blocked = ResponseLinkPolicy.action(for: long) else {
            Issue.record("An oversized address must not open")
            return
        }
    }

    @Test("The reply document loads only when the renderer asked, then follows only its own fragments")
    func navigation() {
        let blank = URL(string: "about:blank")
        func decide(_ url: URL?, mainFrame: Bool = true, expecting: Bool = false, download: Bool = false) -> ResponseNavigationPolicy.Decision {
            ResponseNavigationPolicy.decide(url: url, targetsMainFrame: mainFrame, isExpectingDocument: expecting, currentURL: blank, requestsDownload: download)
        }
        #expect(decide(blank, expecting: true) == .loadDocument)
        #expect(decide(blank) == .cancel) // a page script replacing the reply
        #expect(decide(URL(string: "about:blank#notes")) == .followFragment)
        #expect(decide(URL(string: "about:srcdoc#notes")) == .cancel)
        #expect(decide(blank, mainFrame: false, expecting: true) == .cancel)
        #expect(decide(blank, expecting: true, download: true) == .cancel)
        for address in ["https://example.com/#notes", "http://example.com", "data:text/html,hi", "file:///etc/hosts", "javascript:alert(1)", "blob:null/5b2c", "hermesvoice://send", "about:srcdoc"] {
            #expect(decide(URL(string: address), expecting: true) == .cancel, "\(address)")
        }
    }

    @Test("A streaming snapshot holds back half-received entities and Markdown markers until they complete")
    func streamingSnapshot() {
        #expect(ResponseStreamingText.stablePrefix(of: "<p>Fish &amp; chips &am") == "<p>Fish &amp; chips ")
        #expect(ResponseStreamingText.stablePrefix(of: "<p>Sum: &#x1F4") == "<p>Sum: ")
        #expect(ResponseStreamingText.stablePrefix(of: "Use **bold") == "Use ")
        #expect(ResponseStreamingText.stablePrefix(of: "Run `npm i") == "Run ")
        #expect(ResponseStreamingText.stablePrefix(of: "See [the docs](https://exa") == "See ")
        #expect(ResponseStreamingText.stablePrefix(of: "Chart: ![sales") == "Chart: ")
        #expect(ResponseStreamingText.stablePrefix(of: "Intro\n```swi") == "Intro\n")
        #expect(ResponseStreamingText.stablePrefix(of: "Visit <https://exa") == "Visit ")
    }

    @Test("Complete text streams through unchanged; only the line still being written is held back")
    func streamingSnapshotKeepsCompleteText() {
        for text in ["if a < b and x<5 then 3 > 2", "- [ ] Buy milk\n- [x] Call", "Fish &amp; chips", "<p>2 ** 3 is `8"] {
            #expect(ResponseStreamingText.stablePrefix(of: text) == text[...])
        }
        #expect(ResponseStreamingText.stablePrefix(of: "**Done** and `x`\nNext line with **bold") == "**Done** and `x`\nNext line with ")
    }
}
