import Testing
import UIKit
import WebKit
@testable import HermesVoice

@MainActor
@Suite("Rich reply typing", .serialized)
struct ResponseTypingTests {
    @Test("A fast final reply reveals visible text before completing, without an initial full-content flash")
    func fastFinalStartsTypingInAnEmptyDocument() async throws {
        let window = try #require(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .flatMap(\.windows).first { $0.isKeyWindow })
        let coordinator = ResponseWebCoordinator()
        let container = coordinator.makeContainer()
        container.frame = CGRect(x: 0, y: 0, width: window.bounds.width, height: 260)
        window.addSubview(container)
        defer {
            coordinator.tearDown()
            container.removeFromSuperview()
        }
        var visible = false
        var finished = false
        coordinator.onEvent = { event in
            switch event {
            case .contentVisible: visible = true
            case .typingFinished: finished = true
            default: break
            }
        }
        coordinator.present(ResponseWebPresentation(visibleHeight: 260))
        let answer = "A fast final answer must begin visibly and continue through several frames instead of leaving the waiting dots indefinitely. Unicode 👩🏽‍💻 and é stay intact as the response reaches its final characters."
        coordinator.submit("<p>\(answer)</p>", isStreaming: false, animateTyping: true)
        try #require(await eventually(timeout: .seconds(15)) { visible })
        let partial = try await container.webView.callAsyncJavaScript(
            "return document.getElementById('hermes-root').textContent;",
            arguments: [:], in: nil, contentWorld: .defaultClient
        ) as? String
        let prefix = try #require(partial)
        #expect(!prefix.isEmpty)
        #expect(prefix.count < answer.count)
        #expect(answer.hasPrefix(prefix))
        #expect(!finished)
        try #require(await eventually(timeout: .seconds(15)) { finished })
        let completed = try await container.webView.callAsyncJavaScript(
            "return document.getElementById('hermes-root').textContent;",
            arguments: [:], in: nil, contentWorld: .defaultClient
        ) as? String
        #expect(completed == answer)
    }
}
