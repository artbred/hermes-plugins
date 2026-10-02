import Foundation
import Synchronization
@testable import HermesVoice

struct CapturedRequest: Sendable {
    let request: URLRequest
    let body: Data
    var method: String { request.httpMethod ?? "" }
    var path: String { request.url?.path() ?? "" }
    var query: String? { request.url?.query() }
}

enum StubResponse: Sendable {
    case http(status: Int, body: Data, headers: [String: String] = ["Content-Type": "application/json"])
    case failure(URLError.Code)
    case stream(chunks: [Data], finish: Bool)
    case deferred(DeferredStubResponse)
    case never

    static func json(_ status: Int, _ body: String) -> StubResponse {
        .http(status: status, body: Data(body.utf8))
    }
}

/// Tests release an in-flight response explicitly, without timing-dependent sleeps.
final class DeferredStubResponse: Sendable {
    private struct State: Sendable {
        var response: StubResponse?
        var receiver: (@Sendable (StubResponse) -> Void)?
    }
    private let state = Mutex(State())

    func resolve(_ response: StubResponse) {
        let receiver = state.withLock { value in
            value.response = response
            let receiver = value.receiver
            value.receiver = nil
            return receiver
        }
        receiver?(response)
    }

    fileprivate func receive(_ receiver: @escaping @Sendable (StubResponse) -> Void) {
        let response = state.withLock { value in
            if value.response == nil { value.receiver = receiver }
            return value.response
        }
        if let response { receiver(response) }
    }
}

/// Unique hosts isolate concurrent tests from one another.
final class StubServer: Sendable {
    let host = "stub-\(UUID().uuidString.lowercased()).test"
    private let state: Mutex<State>

    private struct State {
        var requests: [CapturedRequest] = []
        var cancellations = 0
        var responder: @Sendable (CapturedRequest) -> StubResponse
    }

    init(_ responder: @escaping @Sendable (CapturedRequest) -> StubResponse) {
        state = Mutex(State(responder: responder))
        StubURLProtocol.register(self)
    }

    var requests: [CapturedRequest] { state.withLock { $0.requests } }
    var cancellations: Int { state.withLock { $0.cancellations } }

    func baseURL(path: String = "") -> URL { URL(string: "https://\(host)\(path)")! }

    static func configuration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return configuration
    }

    func client(token: String = "test-token", path: String = "") -> APIClient {
        APIClient(baseURL: baseURL(path: path), token: token, session: URLSession(configuration: Self.configuration()))
    }

    fileprivate func handle(_ request: CapturedRequest) -> StubResponse {
        let responder = state.withLock { state in
            state.requests.append(request)
            return state.responder
        }
        return responder(request)
    }

    fileprivate func cancelled() { state.withLock { $0.cancellations += 1 } }
}

final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    private static let servers = Mutex<[String: StubServer]>([:])

    static func register(_ server: StubServer) { servers.withLock { $0[server.host] = server } }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url, let host = url.host(),
              let server = Self.servers.withLock({ $0[host] }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotFindHost))
            return
        }
        let body = request.httpBody ?? request.httpBodyStream.map(Self.readAll) ?? Data()
        respond(server.handle(CapturedRequest(request: request, body: body)), url: url)
    }

    private func respond(_ result: StubResponse, url: URL) {
        switch result {
        case .http(let status, let data, let headers):
            let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        case .stream(let chunks, let finish):
            let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "text/event-stream"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            for chunk in chunks { client?.urlProtocol(self, didLoad: chunk) }
            if finish { client?.urlProtocolDidFinishLoading(self) }
        case .failure(let code):
            client?.urlProtocol(self, didFailWithError: URLError(code))
        case .deferred(let response):
            response.receive { [weak self] result in self?.respond(result, url: url) }
        case .never:
            break
        }
    }

    override func stopLoading() {
        if let host = request.url?.host(), let server = Self.servers.withLock({ $0[host] }) { server.cancelled() }
    }

    private static func readAll(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16 * 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

func makeTemporaryDirectory() -> URL {
    let url = FileManager.default.temporaryDirectory.appending(path: "HermesVoiceTests-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@MainActor
func eventually(timeout: Duration = .seconds(5), _ condition: () -> Bool) async -> Bool {
    let deadline = ContinuousClock.now + timeout
    while !condition() {
        if ContinuousClock.now >= deadline { return false }
        try? await Task.sleep(for: .milliseconds(10))
    }
    return true
}
