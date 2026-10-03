import Foundation
import SwiftSoup
import Testing
@testable import HermesVoice

@Suite("Response content")
struct ResponseContentTests {
    @Test("Speech and copy retain semantic reading order without executable or decorative text")
    func humanReadingOrder() {
        let content = ResponseContent(raw: """
        <article dir="rtl"><h3>مرحبا</h3><p>Line one<br>Line two&nbsp;end</p>
        <ol start="3"><li>Third</li><li>Fourth<ul><li>Nested &amp; kept</li></ul></li></ol>
        <table><tr><th>Name</th><th></th><th>Qty</th></tr><tr><td>Tea</td><td>—</td><td>2</td></tr></table>
        <pre>
          indented code
            deeper
        </pre>
        <p><span aria-hidden="true">decoration</span>Rated <a href="https://example.com/review">five stars</a></p>
        <style>body { color: red }</style><script>window.hidden = 'script-only';</script>
        <template><p>Template copy</p></template></article>
        """)
        #expect(content.plainText == "مرحبا\n\nLine one\nLine two\u{00A0}end\n\n3. Third\n4. Fourth\n  • Nested & kept\n\nName\t\tQty\nTea\t—\t2\n\n  indented code\n    deeper\n\nRated five stars")
    }

    @Test("Document redirects, base URLs and remote loading metadata cannot survive normalization")
    func documentLoadingCapabilities() throws {
        let content = ResponseContent(raw: """
        <!doctype html><html><head><meta http-equiv="refresh" content="0;url=https://evil.example">
        <base href="https://evil.example"><title>Internal title</title>
        <link rel="stylesheet" href="https://cdn.example/app.css"><link rel="dns-prefetch" href="//leak.example">
        <style>h1 { font-size: 1.4em }</style></head><body><h1>Trip</h1><p>Two days in Kyoto.</p></body></html>
        """)
        let document = try parsed(content)
        #expect(try elements(document, "meta, base, title, link").isEmpty)
        #expect(content.plainText == "Trip\n\nTwo days in Kyoto.")
    }

    @Test("Obfuscated script, file, app and relative destinations remain inert while their labels stay readable")
    func destinationBoundary() throws {
        let content = ResponseContent(raw: ##"<p><a href="javascript:alert(1)">run</a> <a href=" JaVa&#x09;ScRiPt:alert(2)">spaced</a> <a href="data:text/html;base64,PHNjcmlwdD4=">data</a> <a href="hermesvoice://send">app</a> <a href="file:///etc/hosts">file</a> <a href="//evil.example/x">relative</a> <a href="https://example.com/docs" target="_blank" ping="https://track.example/">docs</a> <a href="#notes">notes</a></p>"##)
        let anchors = try elements(parsed(content), "a")
        #expect(try anchors.map { try $0.text() } == ["run", "spaced", "data", "app", "file", "relative", "docs", "notes"])
        #expect(anchors.prefix(6).allSatisfy { !$0.hasAttr("href") })
        #expect(try anchors[6].attr("href") == "https://example.com/docs")
        #expect(!anchors[6].hasAttr("target") && !anchors[6].hasAttr("ping"))
        #expect(try anchors[7].attr("href") == "#notes")
    }

    @Test("Frames, external scripts and remote image requests disappear without losing image descriptions")
    func externalResourceBoundary() throws {
        let content = ResponseContent(raw: """
        <p>Before</p><script src="https://cdn.example/lib.js"></script>
        <iframe src="https://example.com" srcdoc="<p>framed</p>"></iframe>
        <object data="https://example.com/x.pdf">object fallback</object><embed src="https://example.com/x.swf">
        <video src="https://example.com/v.mp4">video fallback</video>
        <img src="https://tracker.example/chart.png" srcset="https://tracker.example/2x.png 2x" alt="Sales chart">
        <p>After</p>
        """)
        #expect(try elements(parsed(content), "script[src], iframe, object, embed, video, img[src], [srcset]").isEmpty)
        #expect(content.plainText == "Before\n\nSales chart\n\nAfter")
    }

    @Test("Local checklists and filtering cannot carry credential inputs or submission capabilities")
    func localControlBoundary() throws {
        let content = ResponseContent(raw: """
        <form action="https://evil.example" method="post"><label>Search <input type="search" autocomplete="username" autofocus></label>
        <input type="password"><input type="hidden" value="secret"><input type="file"><input type="image" src="https://evil.example">
        <button formaction="https://evil.example/send">Filter this list</button></form>
        <ul><li><label><input type="checkbox">Pack charger</label></li></ul>
        """)
        let document = try parsed(content)
        #expect(try elements(document, "form, [action], [formaction], [autofocus], input[type=password], input[type=file], input[type=hidden], input[type=image]").isEmpty)
        #expect(try elements(document, "input[type=search]").first?.attr("autocomplete") == "off")
        #expect(content.plainText == "Search\n\n• Pack charger")
    }

    @Test("Legacy GFM preserves table data, nested tasks, code literals and human text")
    func legacyStructuredContent() throws {
        let content = ResponseContent(raw: """
        # Packing list

        | Item | Qty |
        |:-----|----:|
        | Socks | 3 |
        | Café | 1 |

        - Clothes
          - Socks
        - [x] Passport
        - [ ] Charger

        1. Book train
        2. See [docs](https://example.com/rail)

        ```swift
        let total = items.filter { $0.qty > 1 && $0.name != "<none>" }
        ```
        """)
        let document = try parsed(content)
        #expect(try elements(document, "thead th").map { try $0.text() } == ["Item", "Qty"])
        #expect(try elements(document, "tbody td").map { try $0.text() } == ["Socks", "3", "Café", "1"])
        #expect(try elements(document, "ul li ul li").first?.text() == "Socks")
        let checkboxes = try elements(document, "input[type=checkbox]")
        #expect(checkboxes.count == 2 && checkboxes.allSatisfy { $0.hasAttr("disabled") })
        #expect(checkboxes[0].hasAttr("checked") && !checkboxes[1].hasAttr("checked"))
        #expect(try elements(document, "pre code").first?.text() == "let total = items.filter { $0.qty > 1 && $0.name != \"<none>\" }")
        #expect(content.plainText == "Packing list\n\nItem\tQty\nSocks\t3\nCafé\t1\n\n• Clothes\n  • Socks\n• Passport\n• Charger\n\n1. Book train\n2. See docs\n\nlet total = items.filter { $0.qty > 1 && $0.name != \"<none>\" }")
    }

    @Test("An HTML code example in legacy history stays code rather than executing as an interface")
    func legacyHTMLCodeExample() throws {
        let source = "<section><p>Example</p><script>window.example = 1;</script></section>"
        let content = ResponseContent(raw: "```html\n\(source)\n```")
        let document = try parsed(content)
        #expect(!content.isHTML)
        #expect(try elements(document, "script, section").isEmpty)
        #expect(try elements(document, "pre code").first?.text() == source)
        #expect(content.plainText == source)
    }

    @Test("Bare addresses become links but code examples never become navigation targets")
    func legacyAddressDetection() throws {
        let content = ResponseContent(raw: "See https://example.com/a?b=1&c=2, www.example.org or hi@example.com. Not `https://code.example` or [x](https://y.example).")
        let document = try parsed(content)
        let anchors = try elements(document, "a")
        let hrefs = try anchors.map { try $0.attr("href") }
        #expect(hrefs.contains("https://example.com/a?b=1&c=2"))
        #expect(hrefs.contains("mailto:hi@example.com"))
        #expect(!hrefs.contains("https://code.example"))
        #expect(try elements(document, "code").first?.text() == "https://code.example")
        #expect(content.plainText == "See https://example.com/a?b=1&c=2, www.example.org or hi@example.com. Not https://code.example or x.")
    }

    @Test("Streaming tag prefixes never leak markup as readable response text")
    func streamedTagBoundaries() {
        for partial in ["<", "<d", "<div cla", "<p>Done</p><", "<p>Done</p></"] {
            let content = ResponseContent(raw: partial)
            #expect(content.isHTML)
            #expect(!content.plainText.contains("<"))
        }
        #expect(ResponseContent(raw: "<div><h2>Tok").plainText == "Tok")
        #expect(ResponseContent(raw: "<3 thanks & see you").plainText == "<3 thanks & see you")
    }

    @Test("Foreign-content and raw-text reparsing cannot turn escaped examples into active elements")
    func HTMLTokenizerBoundaries() throws {
        let foreign = ResponseContent(raw: #"<svg><style><img src=x onerror="alert(1)"></style><circle r="4"/></svg>"#)
        #expect(try elements(parsed(foreign), "img").isEmpty)
        let textarea = ResponseContent(raw: #"<textarea><p title="</textarea><iframe src='https://evil.example'>">note</p></textarea>"#)
        #expect(try elements(parsed(textarea), "iframe, p[title]").isEmpty)
        let xmp = ResponseContent(raw: #"<xmp><p title="</xmp><img src=x onerror=alert(1)>">x</p></xmp>"#)
        #expect(try elements(parsed(xmp), "img, p[title], xmp").isEmpty)
    }

    @Test("Large ordered-list values cannot overflow speech extraction")
    func listCounterBounds() {
        let content = ResponseContent(raw: "<ol start=2147483647><li>A</li><li>B</li><li value=9223372036854775807>C</li></ol>")
        #expect(content.plainText == "2147483647. A\n2147483648. B\n2147483649. C")
    }

    @Test("Deeply nested reply wrappers preserve their answer without exhausting native recursion")
    func deeplyNestedAnswer() {
        let source = String(repeating: "<div>", count: 4_096) + "<p>Deep value</p>" + String(repeating: "</div>", count: 4_096)
        let content = ResponseContent(raw: source)
        #expect(content.plainText == "Deep value")
    }

    @Test("Quoted side details stay visible on screen but out of speech, copy and titles")
    func secondaryQuotesUnspoken() throws {
        let content = ResponseContent(raw: "<p>Saved to your watchlist.</p><blockquote><p>Verified live: 24 films with it on top.</p></blockquote>")
        #expect(content.plainText == "Saved to your watchlist.")
        #expect(try elements(parsed(content), "blockquote").count == 1)
    }

    private func parsed(_ content: ResponseContent) throws -> SwiftSoup.Document {
        try SwiftSoup.parse(content.html)
    }

    private func elements(_ document: SwiftSoup.Document, _ selector: String) throws -> [SwiftSoup.Element] {
        Array(try document.select(selector))
    }
}
