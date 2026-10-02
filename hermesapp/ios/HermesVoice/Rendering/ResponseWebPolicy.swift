import Foundation

/// What a tapped link in a rendered reply may do. Only secure web pages open, and only inside Hermes: nothing a reply
/// links to can leave the app, start another app or reach native actions.
enum ResponseLinkPolicy {
    enum Action: Equatable {
        /// Show the page in the in-app Safari view.
        case openInApp(URL)
        /// Tell the person why the link did not open.
        case blocked(BlockedLink)
        /// Script pseudo-links and addresses inside the reply document: nothing to open or explain.
        case ignore
    }

    struct BlockedLink: Equatable {
        enum Reason: Equatable {
            /// A plain `http` page: only encrypted pages open.
            case insecure
            /// Mail, phone and app addresses, which would leave Hermes.
            case otherApp
            /// Document, data and file addresses, credentials in a URL, or an address that does not parse.
            case unsafe
        }

        let reason: Reason
        /// The address as a person may read it, shortened.
        let destination: String
        /// What the person may copy instead: the web address, mail address or phone number. `nil` when unsafe.
        let copyableText: String?
    }

    static let maximumLength = 8_192
    private static let maximumDisplayLength = 300
    private static let contactSchemes: Set<String> = ["mailto", "tel", "sms"]
    private static let documentSchemes: Set<String> = ["data", "blob", "file", "filesystem", "content", "view-source"]

    static func action(for href: String) -> Action {
        let address = href.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.isEmpty else { return .ignore }
        guard address.utf8.count <= maximumLength, let url = URL(string: address), let scheme = url.scheme?.lowercased() else {
            return blocked(.unsafe, address)
        }
        switch scheme {
        case "https":
            // `https://bank.example@evil.example` reads as the first host but loads the second.
            guard let host = url.host(percentEncoded: true), !host.isEmpty,
                  url.user(percentEncoded: true) == nil, url.password(percentEncoded: true) == nil
            else { return blocked(.unsafe, address) }
            return .openInApp(url)
        case "http":
            return blocked(.insecure, address, copying: address)
        case "javascript", "about":
            return .ignore
        case _ where documentSchemes.contains(scheme):
            return blocked(.unsafe, address)
        case _ where contactSchemes.contains(scheme):
            let contact = URLComponents(string: address)?.path ?? ""
            return blocked(.otherApp, address, copying: contact.isEmpty ? address : contact)
        default:
            // Other apps' URL schemes, including Hermes Voice's own.
            return blocked(.otherApp, address)
        }
    }

    private static func blocked(_ reason: BlockedLink.Reason, _ address: String, copying text: String? = nil) -> Action {
        let destination = address.count > maximumDisplayLength ? address.prefix(maximumDisplayLength - 1) + "…" : address
        return .blocked(BlockedLink(reason: reason, destination: destination, copyableText: text))
    }
}

/// Which navigations the reply web view permits: loading the reply document the renderer asked for, and scrolling to
/// that document's own `#fragments`. Everything else — pages the reply's script or markup tries to open, frames,
/// downloads, other schemes — is cancelled.
enum ResponseNavigationPolicy {
    enum Decision: Equatable {
        case loadDocument
        case followFragment
        case cancel
    }

    /// Reply documents load from a string without a base URL, which WebKit addresses as `about:blank`.
    static let documentAddress = "about:blank"

    /// - Parameters:
    ///   - isExpectingDocument: The renderer has started a reply document load that WebKit has not yet asked about.
    ///   - currentURL: The address of the document now shown, if any.
    static func decide(url: URL?, targetsMainFrame: Bool, isExpectingDocument: Bool, currentURL: URL?, requestsDownload: Bool) -> Decision {
        guard targetsMainFrame, !requestsDownload else { return .cancel }
        let address = url?.absoluteString ?? ""
        if isExpectingDocument, address.isEmpty || address.lowercased() == documentAddress {
            return .loadDocument
        }
        guard url?.scheme?.lowercased() == "about", let hash = address.firstIndex(of: "#") else { return .cancel }
        let current = currentURL?.absoluteString ?? documentAddress
        let currentDocument = current.firstIndex(of: "#").map { current[..<$0] } ?? current[...]
        return address[..<hash].lowercased() == currentDocument.lowercased() ? .followFragment : .cancel
    }
}

/// The part of a still-streaming reply that is safe to show. Half-received constructs would otherwise flash as raw
/// text until their end arrives: a character reference like `&am`, or in Markdown replies an unclosed code span,
/// emphasis, link or code fence on the line being written. Tags are left to ``ResponseContent``, whose HTML parser
/// already drops a tag cut off mid-stream.
enum ResponseStreamingText {
    static func stablePrefix(of text: String) -> Substring {
        var prefix = text[...]
        // A character reference is at most 32 characters (`&CounterClockwiseContourIntegral;`).
        let tail = prefix.suffix(33)
        if let ampersand = tail.lastIndex(of: "&"),
           prefix[prefix.index(after: ampersand)...].allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "#") }) {
            prefix = prefix[..<ampersand]
        }
        // HTML replies open with a tag, after any whitespace or byte-order mark.
        guard prefix.first(where: { !$0.isWhitespace && $0 != "\u{FEFF}" }) != "<" else { return prefix }
        let lineStart = prefix.lastIndex(of: "\n").map(prefix.index(after:)) ?? prefix.startIndex
        if let cut = unfinishedMarkdownStart(in: prefix[lineStart...]) {
            prefix = prefix[..<cut]
        }
        return prefix
    }

    /// Where an unfinished inline construct starts on the line still being written, if one does.
    private static func unfinishedMarkdownStart(in line: Substring) -> Substring.Index? {
        let content = line.drop { $0 == " " || $0 == "\t" }
        if content.hasPrefix("```") || content.hasPrefix("~~~") || (!content.isEmpty && content.allSatisfy { $0 == "`" || $0 == "~" }) {
            return line.startIndex // a code fence being opened or closed
        }
        var cuts: [Substring.Index] = []
        for marker in ["`", "**", "__", "~~"] {
            let ranges = line.ranges(of: marker)
            if ranges.count % 2 == 1, let last = ranges.last {
                cuts.append(last.lowerBound)
            }
        }
        if let open = line.lastIndex(of: "[") {
            let label = line[line.index(after: open)...]
            let isOpen: Bool
            if let close = label.firstIndex(of: "]") {
                let destination = label[label.index(after: close)...]
                isOpen = destination.hasPrefix("(") && !destination.contains(")")
            } else {
                isOpen = true
            }
            if isOpen {
                let image = open > line.startIndex && line[line.index(before: open)] == "!"
                cuts.append(image ? line.index(before: open) : open)
            }
        }
        if let open = line.lastIndex(of: "<"), !line[open...].contains(">"),
           let next = line[line.index(after: open)...].first, next.isLetter || next == "/" {
            cuts.append(open) // an autolink or inline tag
        }
        return cuts.min()
    }
}
