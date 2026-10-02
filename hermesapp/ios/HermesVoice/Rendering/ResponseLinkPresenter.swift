import SafariServices
import UIKit

/// Carries out what ``ResponseLinkPolicy`` allows for a link a person tapped or long-pressed in a reply.
///
/// Pages open in `SFSafariViewController`, the system's in-app browser: it runs out of process with its own website
/// data, never the app's API credentials or cookies, and the person returns with Done. Hermes never hands a reply's
/// link to another app or to Safari itself; only the browser's own button can, when the person chooses it. Everything
/// is presented from the top of the window rather than from the reply, so a page stays open when a streaming reply is
/// replaced by its final message.
@MainActor
enum ResponseLinkPresenter {
    static func handle(_ action: ResponseLinkPolicy.Action, from view: UIView) {
        switch action {
        case .openInApp(let url):
            openPage(url, from: view)
        case .blocked(let link):
            explain(link, from: view)
        case .ignore:
            break
        }
    }

    /// The long-press menu for a link: open and copy, never a live page preview, which would load the site unasked.
    static func contextMenu(for url: URL?, from view: UIView) -> UIContextMenuConfiguration? {
        guard let url else { return nil }
        let title: String
        var actions: [UIMenuElement] = []
        switch ResponseLinkPolicy.action(for: url.absoluteString) {
        case .openInApp(let page):
            title = page.host(percentEncoded: false) ?? page.absoluteString
            actions.append(UIAction(title: "Open Link", image: UIImage(systemName: "safari")) { [weak view] _ in
                guard let view else { return }
                openPage(page, from: view)
            })
            actions.append(copyAction(page.absoluteString))
        case .blocked(let link):
            guard let text = link.copyableText else { return nil }
            title = link.destination
            actions.append(copyAction(text))
        case .ignore:
            return nil
        }
        return UIContextMenuConfiguration(identifier: nil, previewProvider: nil) { _ in
            UIMenu(title: title, children: actions)
        }
    }

    private static func openPage(_ url: URL, from view: UIView) {
        guard let presenter = presenter(for: view) else { return }
        let configuration = SFSafariViewController.Configuration()
        configuration.entersReaderIfAvailable = false
        configuration.barCollapsingEnabled = true
        let browser = SFSafariViewController(url: url, configuration: configuration)
        browser.dismissButtonStyle = .done
        browser.delegate = browserDelegate
        presenter.present(browser, animated: true)
    }

    private static func explain(_ link: ResponseLinkPolicy.BlockedLink, from view: UIView) {
        guard let presenter = presenter(for: view) else { return }
        let message = switch link.reason {
        case .insecure: "Hermes opens only secure (https) web pages.\n\n\(link.destination)"
        case .otherApp: "Hermes doesn’t open other apps from a reply.\n\n\(link.destination)"
        case .unsafe: "This link’s address isn’t safe to open."
        }
        let alert = UIAlertController(title: "Link Not Opened", message: message, preferredStyle: .alert)
        if let text = link.copyableText {
            alert.addAction(UIAlertAction(title: "Copy", style: .default) { _ in
                UIPasteboard.general.string = text
            })
        }
        alert.addAction(UIAlertAction(title: "OK", style: .cancel))
        presenter.present(alert, animated: true)
    }

    private static func copyAction(_ text: String) -> UIAction {
        UIAction(title: "Copy", image: UIImage(systemName: "doc.on.doc")) { _ in
            UIPasteboard.general.string = text
        }
    }

    /// The topmost controller that can present, or `nil` while a browser or notice is already showing.
    private static func presenter(for view: UIView) -> UIViewController? {
        guard var top = view.window?.rootViewController else { return nil }
        while let presented = top.presentedViewController, !presented.isBeingDismissed {
            top = presented
        }
        guard !(top is SFSafariViewController), !(top is UIAlertController) else { return nil }
        return top
    }

    private static let browserDelegate = BrowserDelegate()

    @MainActor
    private final class BrowserDelegate: NSObject, @preconcurrency SFSafariViewControllerDelegate {
        func safariViewControllerDidFinish(_ controller: SFSafariViewController) {
            controller.dismiss(animated: true)
        }
    }
}
