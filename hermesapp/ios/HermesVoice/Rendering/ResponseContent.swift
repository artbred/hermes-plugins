import Foundation
import Markdown
import SwiftSoup

/// The display and text forms of one assistant reply.
///
/// Replies are requested as HTML (``MobileResponseFormat``). Replies that arrive as Markdown or plain text — older
/// chats, or a model that ignored the format — are parsed as GitHub Flavored Markdown and converted to the same HTML
/// surface. Both are then reduced to a self-contained fragment: document metadata, external resources, frames,
/// embeds, forms, credential inputs and unsafe URLs are removed, while the inline CSS and JavaScript of an HTML reply
/// stay for local interactivity. This is the first of two boundaries; the web view still enforces its content
/// security policy and navigation guards.
struct ResponseContent: Sendable {
    /// Sanitized HTML fragment to display.
    let html: String
    /// Human text for speech, copying, titles and context, read from the HTML without running scripts: no markup,
    /// scripts, styles or metadata.
    let plainText: String
    /// Speech-only text: ``plainText`` minus secondary asides. Italics are the reply convention for side details —
    /// shown muted gray, never spoken — so emphasis here is excluded while copy, titles and context keep the words.
    let spokenText: String
    /// Whether the reply was HTML rather than Markdown or plain text.
    let isHTML: Bool

    init(raw: String) {
        let source = raw.trimmingCharacters(in: Self.edgeWhitespace)
        let fragment: SanitizedFragment
        if source.isEmpty {
            fragment = SanitizedFragment(html: "", plainText: "", spokenText: "")
            isHTML = false
        } else if ReplyMarkup.opensWithHTML(source) {
            fragment = SanitizedFragment(authoredHTML: source)
            isHTML = true
        } else {
            let document = Markdown.Document(parsing: source, options: [.disableSmartOpts])
            var renderer = MarkdownHTMLRenderer()
            renderer.visit(document)
            fragment = SanitizedFragment(markup: renderer.html, policy: .markdown, fallback: source)
            isHTML = false
        }
        html = fragment.html
        plainText = fragment.plainText
        spokenText = fragment.spokenText
    }

    /// Whitespace plus the byte-order mark some tools prepend.
    private static let edgeWhitespace = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "\u{FEFF}"))
}

// MARK: - Format detection

/// How a reply's raw text announces its format.
private enum ReplyMarkup {
    /// HTML, SVG and MathML elements a reply may open with.
    private static let elementNames: Set<String> = [
        "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base", "bdi", "bdo", "big", "blockquote",
        "body", "br", "button", "canvas", "caption", "center", "cite", "code", "col", "colgroup", "data", "datalist",
        "dd", "del", "details", "dfn", "dialog", "div", "dl", "dt", "em", "embed", "fieldset", "figcaption", "figure",
        "font", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html", "i",
        "iframe", "img", "input", "ins", "kbd", "label", "legend", "li", "link", "main", "map", "mark", "math", "menu",
        "meta", "meter", "nav", "noscript", "object", "ol", "optgroup", "option", "output", "p", "picture", "pre",
        "progress", "q", "rp", "rt", "ruby", "s", "samp", "script", "search", "section", "select", "slot", "small",
        "source", "span", "strike", "strong", "style", "sub", "summary", "sup", "svg", "table", "tbody", "td",
        "template", "textarea", "tfoot", "th", "thead", "time", "title", "tr", "track", "tt", "u", "ul", "var",
        "video", "wbr", "xmp",
    ]

    /// Whether the reply opens with HTML: a comment, doctype, or known or custom element tag. Markdown autolinks
    /// (`<https://…>`), `<3` and other prose starting with `<` stay Markdown.
    static func opensWithHTML(_ source: String) -> Bool {
        var scalars = source.unicodeScalars.makeIterator()
        guard let first = scalars.next(), first == "<" else { return false }
        guard var scalar = scalars.next() else { return true } // the first streamed byte of a tag
        if scalar == "!" { return true }
        var name = String.UnicodeScalarView()
        while ReplyASCII.isLetter(scalar) || ReplyASCII.isDigit(scalar) || scalar == "-" {
            name.append(scalar)
            guard let next = scalars.next() else {
                // Still streaming inside the first tag name: markup if it can become one.
                let partial = String(name).lowercased()
                return elementNames.contains { $0.hasPrefix(partial) } || isCustomElementName(partial)
            }
            scalar = next
        }
        guard scalar == ">" || scalar == "/" || ReplyASCII.isWhitespace(scalar) else { return false }
        let tagName = String(name).lowercased()
        return elementNames.contains(tagName) || isCustomElementName(tagName)
    }

    private static func isCustomElementName(_ name: String) -> Bool {
        name.unicodeScalars.first.map(ReplyASCII.isLetter) == true && name.contains("-")
    }
}

// MARK: - Sanitized fragment

/// A sanitized display fragment and the human text it shows.
private struct SanitizedFragment {
    let html: String
    let plainText: String
    let spokenText: String

    init(html: String, plainText: String, spokenText: String) {
        self.html = html
        self.plainText = plainText
        self.spokenText = spokenText
    }

    /// HTML the model wrote. A trailing `<` or `</` is a tag still streaming in, which the parser would otherwise show
    /// as text until the rest arrives; trailing whitespace, as a fenced reply's final newline, never renders.
    init(authoredHTML source: String) {
        var markup = Substring(source)
        while markup.last?.isWhitespace == true {
            markup.removeLast()
        }
        if markup.hasSuffix("</") {
            markup = markup.dropLast(2)
        } else if markup.hasSuffix("<") {
            markup = markup.dropLast()
        }
        self.init(markup: String(markup), policy: .authoredHTML, fallback: source)
    }

    init(markup: String, policy: ReplyHTMLSanitizer.Policy, fallback: String) {
        if let rendered = try? Self.render(markup, policy: policy) {
            self = rendered
        } else {
            // SwiftSoup throws only on API misuse. Keep the reply readable as inert text rather than lose it.
            self = SanitizedFragment(html: "<p style=\"white-space: pre-wrap\">\(ReplyHTMLEscaping.text(fallback))</p>", plainText: fallback, spokenText: fallback)
        }
    }

    private static func render(_ markup: String, policy: ReplyHTMLSanitizer.Policy) throws -> SanitizedFragment {
        let document = try SwiftSoup.parse(markup)
        document.outputSettings().prettyPrint(pretty: false)
        guard let body = try content(of: document) else { return SanitizedFragment(html: "", plainText: "", spokenText: "") }
        try normalizeNesting(in: body)
        try ReplyHTMLSanitizer(policy: policy).sanitizeChildren(of: body, inForeignContent: false)
        var text = PlainTextWriter()
        text.writeChildren(of: body)
        var speech = PlainTextWriter(excludingEmphasis: true)
        speech.writeChildren(of: body)
        // SwiftSoup 2.9.6 serializes the DOM, without reusing pre-sanitized source ranges.
        let bytes = try body.htmlUTF8()
        return SanitizedFragment(html: String(decoding: bytes, as: UTF8.self), plainText: text.finished, spokenText: speech.finished)
    }

    /// Bound native recursive walks without dropping deeply wrapped answer text. Raw-text nodes are converted or
    /// removed by the sanitizer; templates past the bound are inert, not substantive response content.
    private static func normalizeNesting(in body: SwiftSoup.Element) throws {
        let rawText: Set<String> = ["script", "style", "textarea", "xmp", "plaintext", "listing", "iframe", "noscript", "noembed"]
        var pending: [(SwiftSoup.Element, Int)] = [(body, 0)]
        while let (element, depth) = pending.popLast() {
            let name = element.tagNameNormal()
            if rawText.contains(name) { continue }
            if depth >= 128, name == "template" {
                try element.remove()
                continue
            }
            let children = element.children()
            let flatten = depth >= 128 && !children.isEmpty()
            if flatten { try element.unwrap() }
            for child in children.reversed() {
                pending.append((child, flatten ? depth : depth + 1))
            }
        }
    }

    /// The body with any head `<style>`, `<script>` and `<template>` moved to its start. The parser hoists a fragment's
    /// leading CSS and JavaScript into `<head>` just as a document declares them there; the rest of a head is metadata.
    private static func content(of document: SwiftSoup.Document) throws -> SwiftSoup.Element? {
        guard let root = document.children().first(where: { $0.tagNameNormal() == "html" }),
              let body = root.children().first(where: { $0.tagNameNormal() == "body" })
        else { return nil }
        var hoisted: [Node] = []
        for head in root.children() where head.tagNameNormal() == "head" {
            for element in head.children() where ["style", "script", "template"].contains(element.tagNameNormal()) {
                hoisted.append(element)
            }
        }
        if !hoisted.isEmpty {
            try body.insertChildren(0, hoisted)
        }
        return body
    }
}

// MARK: - Sanitizer

/// Removes from the parsed DOM whatever would let an inline reply load, navigate, submit or ask for credentials. Only
/// the sanitized tree is serialized, never spans of the input text.
private struct ReplyHTMLSanitizer {
    enum Policy {
        /// HTML the model wrote: its inline CSS and JavaScript stay for local interactivity.
        case authoredHTML
        /// HTML converted from Markdown, including raw HTML the Markdown carried: fully inert.
        case markdown
    }

    let policy: Policy

    private enum Disposition { case keep, remove, unwrap, alternativeText, preformatted, sourceText }

    /// Metadata, external resources, frames, embeds and media.
    private static let removedElements: Set<String> = [
        "head", "base", "basefont", "bgsound", "link", "meta", "iframe", "frame", "frameset", "object", "embed",
        "applet", "param", "portal", "fencedframe", "noscript", "noembed", "noframes", "video", "audio", "source",
        "track", "keygen", "isindex",
    ]
    /// Elements whose content is welcome but whose own behavior (submission, source selection) is not.
    private static let unwrappedElements: Set<String> = ["form", "picture", "html", "body"]
    private static let removedInputTypes: Set<String> = ["password", "file", "hidden", "image"]
    private static let textEntryInputTypes: Set<String> = ["", "text", "search", "email", "tel", "url", "number"]
    private static let removedScriptTypes: Set<String> = ["importmap", "speculationrules", "webbundle", "application/ld+json"]
    private static let animationElements: Set<String> = ["animate", "set", "animatemotion", "animatetransform", "animatecolor"]
    /// Form submission, new windows, pings, downloads, remote alternates, focus stealing and AutoFill hints.
    private static let removedAttributes: Set<String> = [
        "action", "formaction", "formmethod", "formenctype", "formtarget", "formnovalidate", "form", "method",
        "enctype", "target", "ping", "download", "srcset", "imagesrcset", "srcdoc", "autofocus", "autocomplete",
        "http-equiv", "manifest", "background", "poster", "lowsrc", "dynsrc", "longdesc", "codebase", "classid",
        "archive", "xml:base", "attributionsrc",
    ]

    func sanitizeChildren(of parent: SwiftSoup.Element, inForeignContent foreign: Bool) throws {
        for node in parent.getChildNodes() {
            if let element = node as? SwiftSoup.Element {
                try sanitize(element, inForeignContent: foreign)
            } else if let data = node as? DataNode {
                // Only <script> and <style> hold raw data. Inside SVG or MathML a browser tokenizes it as markup, so it
                // is serialized as escaped text there; elsewhere it round-trips exactly as parsed.
                if foreign {
                    try data.replaceWith(TextNode(data.getWholeData(), nil))
                }
            } else if !(node is TextNode) {
                try node.remove() // comments, doctypes and processing instructions
            }
        }
    }

    private func sanitize(_ element: SwiftSoup.Element, inForeignContent foreign: Bool) throws {
        let name = element.tagNameNormal()
        switch disposition(of: element, named: name, inForeignContent: foreign) {
        case .remove:
            try element.remove()
        case .unwrap:
            try sanitizeChildren(of: element, inForeignContent: foreign)
            try element.unwrap()
        case .alternativeText:
            let alternative = Self.attribute("alt", of: element).trimmingCharacters(in: .whitespacesAndNewlines)
            if alternative.isEmpty {
                try element.remove()
            } else {
                try element.replaceWith(TextNode(alternative, nil))
            }
        case .preformatted:
            try element.tagName("pre")
            try sanitizeAttributes(of: element, named: "pre")
            try sanitizeChildren(of: element, inForeignContent: foreign)
        case .sourceText:
            // Browsers read these contents as text, SwiftSoup as markup. Replacing that markup with its source text
            // keeps what a browser displays and leaves nothing for it to reinterpret.
            let source = try element.html()
            element.empty()
            if name == "textarea" {
                try element.appendText(SwiftSoup.Parser.unescapeEntities(source, false))
                try sanitizeAttributes(of: element, named: name)
            } else {
                try element.appendText(source)
                try element.tagName("pre")
                try sanitizeAttributes(of: element, named: "pre")
            }
        case .keep:
            try sanitizeAttributes(of: element, named: name)
            try sanitizeChildren(of: element, inForeignContent: foreign || name == "svg" || name == "math")
        }
    }

    private func disposition(of element: SwiftSoup.Element, named name: String, inForeignContent foreign: Bool) -> Disposition {
        guard Self.isPlainName(name, isAttribute: false), !Self.unwrappedElements.contains(name) else { return .unwrap }
        if Self.removedElements.contains(name) { return .remove }
        switch name {
        case "title":
            return foreign ? .keep : .remove // an SVG title is the graphic's accessible name
        case "script":
            return keepsScript(element) ? .keep : .remove
        case "style", "template":
            return policy == .authoredHTML ? .keep : .remove
        case "xmp", "textarea":
            return .sourceText
        case "listing", "plaintext":
            return .preformatted
        case "input":
            return Self.removedInputTypes.contains(Self.inputType(of: element)) ? .remove : .keep
        case "img":
            let source = Self.attribute("src", of: element)
            return ReplyURLPolicy.isBlank(source) || ReplyURLPolicy.isEmbeddableImage(source) ? .keep : .alternativeText
        default:
            guard Self.animationElements.contains(name) else { return .keep }
            // SMIL could otherwise animate a link's href to a script URL.
            let target = Self.attribute("attributename", of: element).trimmingCharacters(in: .whitespaces).lowercased()
            return target == "href" || target == "xlink:href" ? .remove : .keep
        }
    }

    /// Inline scripts of HTML replies only. External, import-map, speculation-rule and metadata scripts go, as does
    /// script text containing `<!--`, which can leave the tokenizer in an escaped state that swallows the markup after
    /// a reply cut off mid-stream.
    private func keepsScript(_ script: SwiftSoup.Element) -> Bool {
        guard policy == .authoredHTML, !script.hasAttr("src"), !script.hasAttr("href"), !script.hasAttr("xlink:href") else {
            return false
        }
        let type = Self.attribute("type", of: script).split(separator: ";").first?.trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        return !Self.removedScriptTypes.contains(type) && !script.data().contains("<!--")
    }

    private func sanitizeAttributes(of element: SwiftSoup.Element, named name: String) throws {
        for attribute in element.getAttributes()?.asList() ?? [] {
            let key = attribute.getKey()
            if !keeps(key.lowercased(), value: attribute.getValue(), on: name) {
                try element.removeAttr(key)
            }
        }
        if name == "textarea" || (name == "input" && Self.textEntryInputTypes.contains(Self.inputType(of: element))) {
            try element.attr("autocomplete", "off") // no password or contact AutoFill into model-made fields
        }
    }

    private func keeps(_ key: String, value: String, on name: String) -> Bool {
        guard Self.isPlainName(key, isAttribute: true) else { return false }
        if key.hasPrefix("on") { return policy == .authoredHTML } // event handlers are inline JavaScript
        if Self.removedAttributes.contains(key) { return false }
        switch key {
        case "href", "xlink:href":
            switch name {
            case "a", "area": return ReplyURLPolicy.isNavigable(value)
            case "image", "feimage": return ReplyURLPolicy.isEmbeddableImage(value)
            default: return ReplyURLPolicy.isFragment(value)
            }
        case "src":
            return name == "img" && (ReplyURLPolicy.isBlank(value) || ReplyURLPolicy.isEmbeddableImage(value))
        case "cite":
            return ReplyURLPolicy.isNavigable(value)
        default:
            return true
        }
    }

    /// ASCII letters, digits and `-_.`, plus `:` in attribute names such as `xlink:href`. Oddities the parser accepts,
    /// like `a"b` or `x<y`, are dropped rather than trusted to reserialize identically.
    private static func isPlainName(_ name: String, isAttribute: Bool) -> Bool {
        guard let first = name.unicodeScalars.first, ReplyASCII.isLetter(first) || (isAttribute && first == "_") else { return false }
        return name.unicodeScalars.allSatisfy { scalar in
            ReplyASCII.isLetter(scalar) || ReplyASCII.isDigit(scalar) || scalar == "-" || scalar == "_" || scalar == "."
                || (isAttribute && scalar == ":")
        }
    }

    private static func inputType(of input: SwiftSoup.Element) -> String {
        attribute("type", of: input).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    private static func attribute(_ key: String, of element: SwiftSoup.Element) -> String {
        (try? element.attr(key)) ?? ""
    }
}

/// URL checks mirror the URL parser's preprocessing — leading C0 controls and spaces stripped, ASCII tabs and newlines
/// ignored anywhere — so an obfuscated `java&#9;script:` is judged as the `javascript:` WebKit would see.
private enum ReplyURLPolicy {
    private static let navigableSchemes: Set<String> = ["https", "http", "mailto", "tel"]
    private static let imageMediaTypes: Set<String> = [
        "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/svg+xml",
        "image/x-icon", "image/vnd.microsoft.icon",
    ]

    /// Destinations a person may choose to open: web pages, mail, phone and same-document anchors.
    static func isNavigable(_ value: String) -> Bool {
        let url = significantPrefix(of: value)
        return url.hasPrefix("#") || scheme(of: url).map { navigableSchemes.contains($0) } == true
    }

    static func isFragment(_ value: String) -> Bool {
        significantPrefix(of: value).hasPrefix("#")
    }

    static func isBlank(_ value: String) -> Bool {
        significantPrefix(of: value).isEmpty
    }

    /// Inline raster or SVG image data. SVG loaded as an image can neither run script nor fetch.
    static func isEmbeddableImage(_ value: String) -> Bool {
        let url = significantPrefix(of: value)
        guard url.hasPrefix("data:") else { return false }
        let mediaType = url.dropFirst(5).prefix { $0 != ";" && $0 != "," }
        return imageMediaTypes.contains(mediaType.trimmingCharacters(in: .whitespaces))
    }

    /// The lowercased start of a URL as WebKit parses it, long enough for scheme and media type checks.
    private static func significantPrefix(of value: String) -> String {
        var prefix = String.UnicodeScalarView()
        var count = 0
        for scalar in value.unicodeScalars {
            if (prefix.isEmpty && scalar.value <= 0x20) || scalar == "\t" || scalar == "\n" || scalar == "\r" { continue }
            prefix.append(scalar)
            count += 1
            if count == 64 { break }
        }
        return String(prefix).lowercased()
    }

    private static func scheme(of url: String) -> String? {
        guard let colon = url.firstIndex(of: ":") else { return nil }
        let scheme = url[..<colon]
        guard let first = scheme.unicodeScalars.first, ReplyASCII.isLetter(first),
              scheme.unicodeScalars.allSatisfy({ ReplyASCII.isLetter($0) || ReplyASCII.isDigit($0) || $0 == "+" || $0 == "-" || $0 == "." })
        else { return nil }
        return String(scheme)
    }
}

// MARK: - Plain text

/// Writes the visible human text of sanitized HTML the way `innerText` lays it out, without CSS or scripts: blocks
/// become line breaks, list items get markers, table cells are tab separated and preformatted text keeps its spacing.
private struct PlainTextWriter {
    private var text = ""
    /// When true, `em`/`i` asides are skipped: italics are the reply convention for side details.
    private let excludingEmphasis: Bool
    private var pendingBreaks = 0
    private var pendingSpace = false
    private var mayInsertSpace = false
    private var pendingMarker: String?
    private var preformattedDepth = 0
    /// Numbering of the lists being written, innermost last; `nil` for unordered lists.
    private var lists: [Numbering?] = []
    private var rowCellCounts: [Int] = []

    init(excludingEmphasis: Bool = false) {
        self.excludingEmphasis = excludingEmphasis
    }

    private struct Numbering {
        var next: Int
        let step: Int
    }

    /// Code, styling, templates, form controls, graphics and ruby annotations carry no reply text.
    private static let silentElements: Set<String> = [
        "script", "style", "template", "noscript", "title", "head", "svg", "canvas", "button", "input", "select",
        "option", "optgroup", "datalist", "textarea", "progress", "meter", "rt", "rp", "iframe", "object", "embed",
        "video", "audio",
    ]
    private static let paragraphBlocks: Set<String> = [
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "figure", "details", "section", "article", "header",
        "footer", "main", "aside", "nav", "address", "fieldset", "dl", "hgroup", "search", "table",
    ]
    private static let lineBlocks: Set<String> = [
        "div", "summary", "figcaption", "legend", "caption", "dt", "dd", "dialog", "center", "form",
    ]

    var finished: String {
        var result = Substring(text)
        while result.first?.isNewline == true { result.removeFirst() }
        while result.last?.isWhitespace == true { result.removeLast() }
        return String(result)
    }

    mutating func writeChildren(of element: SwiftSoup.Element) {
        for (index, node) in element.getChildNodes().enumerated() {
            if let textNode = node as? TextNode {
                if preformattedDepth > 0 {
                    let value = textNode.getWholeText()
                    let dropsInitialLineFeed = index == 0 && element.tagNameNormal() == "pre" && value.first == "\n"
                    writePreformatted(dropsInitialLineFeed ? String(value.dropFirst()) : value)
                } else {
                    writeCollapsible(textNode.getWholeText())
                }
            } else if let child = node as? SwiftSoup.Element {
                write(child)
            }
        }
    }

    private mutating func write(_ element: SwiftSoup.Element) {
        let name = element.tagNameNormal()
        let hidden = attribute("aria-hidden", of: element).trimmingCharacters(in: .whitespaces).lowercased() == "true"
        guard !hidden, !Self.silentElements.contains(name) else { return }
        switch name {
        case "br":
            writeLineBreak()
        case "blockquote":
            // Secondary depth: shown gray on screen, never spoken, copied or titled.
            return
        case "em", "i":
            // Italic asides: shown muted gray; spoken only in the full-text pass.
            if excludingEmphasis { return }
            writeChildren(of: element)
        case "img":
            writeCollapsible(attribute("alt", of: element))
        case "hr":
            requireBreaks(2)
        case "ul", "ol", "menu":
            writeList(element, ordered: name == "ol")
        case "li":
            writeListItem(element)
        case "tr":
            requireBreaks(1)
            rowCellCounts.append(0)
            writeChildren(of: element)
            rowCellCounts.removeLast()
            requireBreaks(1)
        case "td", "th":
            if let cells = rowCellCounts.last {
                if cells > 0 { writeSeparator("\t") }
                rowCellCounts[rowCellCounts.count - 1] = cells + 1
            }
            writeChildren(of: element)
        case "pre":
            requireBreaks(2)
            preformattedDepth += 1
            writeChildren(of: element)
            preformattedDepth -= 1
            requireBreaks(2)
        default:
            let breaks = Self.paragraphBlocks.contains(name) ? 2 : Self.lineBlocks.contains(name) ? 1 : 0
            requireBreaks(breaks)
            writeChildren(of: element)
            requireBreaks(breaks)
        }
    }

    private mutating func writeList(_ list: SwiftSoup.Element, ordered: Bool) {
        var numbering: Numbering?
        if ordered {
            let reversed = list.hasAttr("reversed")
            let start = Int32(attribute("start", of: list).trimmingCharacters(in: .whitespaces))
            let initial = start.map(Int.init) ?? (reversed ? list.children().filter { $0.tagNameNormal() == "li" }.count : 1)
            numbering = Numbering(next: initial, step: reversed ? -1 : 1)
        }
        let breaks = lists.isEmpty ? 2 : 1
        requireBreaks(breaks)
        lists.append(numbering)
        writeChildren(of: list)
        lists.removeLast()
        requireBreaks(breaks)
    }

    private mutating func writeListItem(_ item: SwiftSoup.Element) {
        requireBreaks(1)
        var marker = "•"
        if let index = lists.indices.last, var numbering = lists[index] {
            if let value = Int32(attribute("value", of: item).trimmingCharacters(in: .whitespaces)) {
                numbering.next = Int(value)
            }
            marker = "\(numbering.next)."
            numbering.next += numbering.step
            lists[index] = numbering
        }
        pendingMarker = String(repeating: "  ", count: max(lists.count - 1, 0)) + marker + " "
        writeChildren(of: item)
        pendingMarker = nil // an item without text leaves no marker
        requireBreaks(1)
    }

    /// Text in normal flow: runs of HTML whitespace collapse to one space, dropped at line starts and ends.
    private mutating func writeCollapsible(_ value: String) {
        for scalar in value.unicodeScalars {
            switch scalar {
            case " ", "\t", "\n", "\r", "\u{0C}":
                pendingSpace = true
            default:
                beginContent()
                if pendingSpace && mayInsertSpace { text.append(" ") }
                pendingSpace = false
                text.unicodeScalars.append(scalar)
                mayInsertSpace = true
            }
        }
    }

    private mutating func writePreformatted(_ value: String) {
        guard !value.isEmpty else { return }
        beginContent()
        text += value
        pendingSpace = false
        mayInsertSpace = value.last?.isNewline != true
    }

    private mutating func writeLineBreak() {
        beginContent()
        text += "\n"
        pendingSpace = false
        mayInsertSpace = false
    }

    private mutating func writeSeparator(_ separator: String) {
        beginContent()
        text += separator
        pendingSpace = false
        mayInsertSpace = false
    }

    private mutating func requireBreaks(_ count: Int) {
        guard count > 0 else { return }
        pendingBreaks = max(pendingBreaks, count)
        pendingSpace = false
    }

    /// Emits pending line breaks (counting newlines already written) and a pending list marker before content.
    private mutating func beginContent() {
        if pendingBreaks > 0 {
            if !text.isEmpty {
                let existing = text.reversed().prefix(while: \.isNewline).count
                text += String(repeating: "\n", count: max(pendingBreaks - existing, 0))
            }
            pendingBreaks = 0
            pendingSpace = false
            mayInsertSpace = false
        }
        if let marker = pendingMarker {
            text += marker
            pendingMarker = nil
            pendingSpace = false
            mayInsertSpace = false
        }
    }

    private func attribute(_ key: String, of element: SwiftSoup.Element) -> String {
        (try? element.attr(key)) ?? ""
    }
}

// MARK: - Markdown

/// Prints swift-markdown's GFM syntax tree, parsed by cmark-gfm, as HTML. Text is escaped here; raw HTML passes through
/// GFM's tag filter and then the same sanitizer as HTML replies.
private struct MarkdownHTMLRenderer: MarkupWalker {
    private(set) var html = ""
    /// Tightness of the lists being printed, innermost last. Tight lists print item paragraphs without `<p>`.
    private var tightLists: [Bool] = []
    /// Created on first use: GFM's autolink extension is off in swift-markdown, so bare addresses are found here.
    private lazy var linkDetector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue)

    mutating func visitBlockQuote(_ blockQuote: BlockQuote) {
        html += "<blockquote>\n"
        descendInto(blockQuote)
        html += "</blockquote>\n"
    }

    mutating func visitCodeBlock(_ codeBlock: CodeBlock) {
        html += "<pre><code"
        if let language = Self.languageName(codeBlock.language) {
            html += " class=\"language-\(ReplyHTMLEscaping.attribute(language))\""
        }
        html += ">\(ReplyHTMLEscaping.text(codeBlock.code))</code></pre>\n"
    }

    mutating func visitHeading(_ heading: Heading) {
        let level = min(max(heading.level, 1), 6)
        html += "<h\(level)>"
        descendInto(heading)
        html += "</h\(level)>\n"
    }

    mutating func visitThematicBreak(_ thematicBreak: ThematicBreak) {
        html += "<hr>\n"
    }

    mutating func visitHTMLBlock(_ htmlBlock: HTMLBlock) {
        html += GFMTagFilter.apply(to: htmlBlock.rawHTML)
    }

    mutating func visitParagraph(_ paragraph: Paragraph) {
        if tightLists.last == true, paragraph.parent is ListItem {
            descendInto(paragraph)
        } else {
            html += "<p>"
            descendInto(paragraph)
            html += "</p>\n"
        }
    }

    mutating func visitOrderedList(_ orderedList: OrderedList) {
        let start = orderedList.startIndex == 1 ? "" : " start=\"\(orderedList.startIndex)\""
        html += "<ol\(start)\(Self.taskListClass(orderedList))>\n"
        printItems(of: orderedList)
        html += "</ol>\n"
    }

    mutating func visitUnorderedList(_ unorderedList: UnorderedList) {
        html += "<ul\(Self.taskListClass(unorderedList))>\n"
        printItems(of: unorderedList)
        html += "</ul>\n"
    }

    mutating func visitListItem(_ listItem: ListItem) {
        switch listItem.checkbox {
        case nil: html += "<li>"
        case .unchecked?: html += "<li class=\"task-list-item\"><input type=\"checkbox\" disabled> "
        case .checked?: html += "<li class=\"task-list-item\"><input type=\"checkbox\" disabled checked> "
        }
        descendInto(listItem)
        html += "</li>\n"
    }

    mutating func visitTable(_ table: Table) {
        let alignments = table.columnAlignments
        html += "<table>\n<thead>\n"
        printRow(table.head.cells, cellTag: "th", alignments: alignments)
        html += "</thead>\n"
        if !table.body.isEmpty {
            html += "<tbody>\n"
            for row in table.body.rows {
                printRow(row.cells, cellTag: "td", alignments: alignments)
            }
            html += "</tbody>\n"
        }
        html += "</table>\n"
    }

    mutating func visitInlineCode(_ inlineCode: InlineCode) {
        html += "<code>\(ReplyHTMLEscaping.text(inlineCode.code))</code>"
    }

    mutating func visitEmphasis(_ emphasis: Emphasis) {
        wrap(emphasis, in: "em")
    }

    mutating func visitStrong(_ strong: Strong) {
        wrap(strong, in: "strong")
    }

    mutating func visitStrikethrough(_ strikethrough: Strikethrough) {
        wrap(strikethrough, in: "del")
    }

    mutating func visitText(_ text: Markdown.Text) {
        let string = text.string
        guard string.contains(where: { $0 == "." || $0 == "@" }), !Self.isInsideLink(text), let detector = linkDetector else {
            html += ReplyHTMLEscaping.text(string)
            return
        }
        html += Self.autolinked(string, detector: detector)
    }

    mutating func visitInlineHTML(_ inlineHTML: InlineHTML) {
        html += GFMTagFilter.apply(to: inlineHTML.rawHTML)
    }

    /// A single newline in chat text is a line break.
    mutating func visitSoftBreak(_ softBreak: SoftBreak) {
        html += "<br>\n"
    }

    mutating func visitLineBreak(_ lineBreak: LineBreak) {
        html += "<br>\n"
    }

    mutating func visitLink(_ link: Link) {
        html += "<a href=\"\(ReplyHTMLEscaping.attribute(link.destination ?? ""))\""
        if let title = link.title, !title.isEmpty {
            html += " title=\"\(ReplyHTMLEscaping.attribute(title))\""
        }
        html += ">"
        descendInto(link)
        html += "</a>"
    }

    mutating func visitImage(_ image: Image) {
        html += "<img src=\"\(ReplyHTMLEscaping.attribute(image.source ?? ""))\" alt=\"\(ReplyHTMLEscaping.attribute(image.plainText))\""
        if let title = image.title, !title.isEmpty {
            html += " title=\"\(ReplyHTMLEscaping.attribute(title))\""
        }
        html += ">"
    }

    mutating func visitSymbolLink(_ symbolLink: SymbolLink) {
        html += "<code>\(ReplyHTMLEscaping.text(symbolLink.destination ?? ""))</code>"
    }

    mutating func visitCustomInline(_ customInline: CustomInline) {
        html += ReplyHTMLEscaping.text(customInline.text)
    }

    private mutating func wrap(_ markup: Markup, in tag: String) {
        html += "<\(tag)>"
        descendInto(markup)
        html += "</\(tag)>"
    }

    private mutating func printItems(of list: some ListItemContainer) {
        tightLists.append(!Self.isLoose(list))
        descendInto(list)
        tightLists.removeLast()
    }

    private mutating func printRow(_ cells: some Sequence<Table.Cell>, cellTag: String, alignments: [Table.ColumnAlignment?]) {
        html += "<tr>\n"
        // A zero span marks a cell covered by a neighbor's colspan or rowspan.
        for (column, cell) in cells.enumerated() where cell.colspan > 0 && cell.rowspan > 0 {
            html += "<\(cellTag)"
            if column < alignments.count, let alignment = alignments[column] {
                html += " style=\"text-align: \(Self.cssValue(alignment))\""
            }
            if cell.colspan > 1 {
                html += " colspan=\"\(cell.colspan)\""
            }
            if cell.rowspan > 1 {
                html += " rowspan=\"\(cell.rowspan)\""
            }
            html += ">"
            descendInto(cell)
            html += "</\(cellTag)>\n"
        }
        html += "</tr>\n"
    }

    /// CommonMark list tightness is inferred from adjacent source ranges without copying the AST collections.
    private static func isLoose(_ list: some ListItemContainer) -> Bool {
        var previousItemLastBlock: Markup?
        for item in list.listItems {
            if let previous = previousItemLastBlock, !(previous is ListItemContainer || previous is BlockQuote),
               blankLineSeparates(previous, item) {
                return true
            }
            var previousBlock: Markup?
            for block in item.children {
                if let previous = previousBlock,
                   (previous is Paragraph && block is Paragraph) || blankLineSeparates(previous, block) {
                    return true
                }
                previousBlock = block
            }
            previousItemLastBlock = previousBlock
        }
        return false
    }

    private static func blankLineSeparates(_ first: Markup, _ second: Markup) -> Bool {
        guard let end = first.range?.upperBound.line, let start = second.range?.lowerBound.line else { return false }
        return start - end > 1
    }

    /// GFM's autolink extension: bare `http(s)://…`, `www.…` and email addresses become links, as the previous SwiftUI
    /// text view made them tappable. Foundation's data detector finds them; bare domains without `www.` stay text.
    private static func autolinked(_ string: String, detector: NSDataDetector) -> String {
        let text = string as NSString
        var result = ""
        var location = 0
        for match in detector.matches(in: string, range: NSRange(location: 0, length: text.length)) {
            guard let url = match.url, match.range.location >= location else { continue }
            let label = text.substring(with: match.range)
            let lowercased = label.lowercased()
            let isAddress = url.scheme?.lowercased() == "mailto"
                ? label.contains("@")
                : ["http://", "https://", "www."].contains { lowercased.hasPrefix($0) }
            guard isAddress else { continue }
            result += ReplyHTMLEscaping.text(text.substring(with: NSRange(location: location, length: match.range.location - location)))
            result += "<a href=\"\(ReplyHTMLEscaping.attribute(url.absoluteString))\">\(ReplyHTMLEscaping.text(label))</a>"
            location = NSMaxRange(match.range)
        }
        return result + ReplyHTMLEscaping.text(text.substring(from: location))
    }

    private static func isInsideLink(_ markup: Markup) -> Bool {
        var ancestor = markup.parent
        while let current = ancestor {
            if current is Link || current is Image { return true }
            ancestor = current.parent
        }
        return false
    }

    private static func taskListClass(_ list: some ListItemContainer) -> String {
        list.listItems.contains { $0.checkbox != nil } ? " class=\"contains-task-list\"" : ""
    }

    /// The first word of a code fence's info string, reduced to characters a class name needs.
    private static func languageName(_ info: String?) -> String? {
        guard let word = info?.split(whereSeparator: \.isWhitespace).first else { return nil }
        let name = word.filter { $0.isASCII && ($0.isLetter || $0.isNumber || "+#._-".contains($0)) }
        return name.isEmpty ? nil : String(name)
    }

    private static func cssValue(_ alignment: Table.ColumnAlignment) -> String {
        switch alignment {
        case .left: "left"
        case .center: "center"
        case .right: "right"
        }
    }
}

/// GitHub Flavored Markdown's "disallowed raw HTML" extension: raw HTML in Markdown may not open or close these
/// elements, so their `<` is escaped and the tag reads as text. Stricter than cmark-gfm, any `/` after the name counts,
/// because browsers treat `<script/src=…>` as a script tag.
private enum GFMTagFilter {
    private static let names: [[UInt8]] = ["title", "textarea", "style", "xmp", "iframe", "noembed", "noframes", "script", "plaintext"]
        .map { Array($0.utf8) }
    private static let escapedLessThan = Array("&lt;".utf8)

    static func apply(to raw: String) -> String {
        let bytes = Array(raw.utf8)
        guard bytes.contains(UInt8(ascii: "<")) else { return raw }
        var filtered: [UInt8] = []
        filtered.reserveCapacity(bytes.count + 16)
        for index in bytes.indices {
            if bytes[index] == UInt8(ascii: "<"), opensDisallowedTag(bytes, after: index) {
                filtered.append(contentsOf: escapedLessThan)
            } else {
                filtered.append(bytes[index])
            }
        }
        return String(decoding: filtered, as: UTF8.self)
    }

    private static func opensDisallowedTag(_ bytes: [UInt8], after lessThan: Int) -> Bool {
        var start = lessThan + 1
        if start < bytes.count, bytes[start] == UInt8(ascii: "/") { start += 1 }
        return names.contains { name in
            let end = start + name.count
            guard end <= bytes.count, zip(bytes[start..<end], name).allSatisfy({ ($0 | 0x20) == $1 }) else { return false }
            return end == bytes.count || [0x09, 0x0A, 0x0C, 0x0D, 0x20, UInt8(ascii: "/"), UInt8(ascii: ">")].contains(bytes[end])
        }
    }
}

private enum ReplyHTMLEscaping {
    static func text(_ value: String) -> String {
        escape(value, quotes: false)
    }

    static func attribute(_ value: String) -> String {
        escape(value, quotes: true)
    }

    private static func escape(_ value: String, quotes: Bool) -> String {
        guard value.utf8.contains(where: { $0 == 0x26 || $0 == 0x3C || $0 == 0x3E || (quotes && $0 == 0x22) }) else {
            return value
        }
        var escaped = ""
        escaped.reserveCapacity(value.utf8.count + 16)
        for scalar in value.unicodeScalars {
            switch scalar {
            case "&": escaped += "&amp;"
            case "<": escaped += "&lt;"
            case ">": escaped += "&gt;"
            case "\"" where quotes: escaped += "&quot;"
            default: escaped.unicodeScalars.append(scalar)
            }
        }
        return escaped
    }
}

/// ASCII character classes of HTML and URL syntax.
private enum ReplyASCII {
    static func isLetter(_ scalar: Unicode.Scalar) -> Bool {
        ("a"..."z").contains(scalar) || ("A"..."Z").contains(scalar)
    }

    static func isDigit(_ scalar: Unicode.Scalar) -> Bool {
        ("0"..."9").contains(scalar)
    }

    static func isWhitespace(_ scalar: Unicode.Scalar) -> Bool {
        scalar == " " || scalar == "\t" || scalar == "\n" || scalar == "\r" || scalar == "\u{0C}"
    }
}
