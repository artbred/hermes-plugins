import AVFoundation
import Observation

@MainActor
@Observable
final class ReplyPlayer {
    private(set) var messageID: String?
    private(set) var isPlaying = false
    @ObservationIgnored var onError: ((String) -> Void)?
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private var delegate: ReplyPlayerDelegate?
    @ObservationIgnored private var generation: UUID?
    @ObservationIgnored private var observers: [any NSObjectProtocol] = []
    /// Identifies the request holding the audio session or waiting for it to activate. Stopping,
    /// pausing or a newer request replaces it, so a superseded activation never starts audio.
    @ObservationIgnored private var sessionRequest: UUID?

    /// Nothing is loaded, playing, or waiting for the audio session.
    var isIdle: Bool { player == nil && sessionRequest == nil }

    init() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] notification in
            let type = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            if type == AVAudioSession.InterruptionType.began.rawValue {
                MainActor.assumeIsolated { self?.pause() }
            }
        })
        observers.append(center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] notification in
            let reason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            if reason == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue {
                MainActor.assumeIsolated { self?.pause() }
            }
        })
        observers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.stop() }
        })
    }

    /// Plays `url` once the audio session is active. Returns without playing when `stop()`, `pause()`
    /// or a newer `play` superseded this request while the session was activating.
    func play(url: URL, messageID: String) async throws {
        stop()
        guard let request = try await acquireSession(), sessionRequest == request else { return }
        do {
            let player = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<AVAudioPlayer, any Error>) in
                AudioSession.queue.async {
                    do {
                        let prepared = try AVAudioPlayer(contentsOf: url)
                        guard prepared.prepareToPlay() else { throw PlaybackError.cannotPlay }
                        continuation.resume(returning: prepared)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
            guard sessionRequest == request else { return }
            let generation = UUID()
            let delegate = ReplyPlayerDelegate(generation: generation) { [weak self] id, succeeded in
                guard let self, self.generation == id else { return }
                self.stop()
                if !succeeded { self.onError?(PlaybackError.cannotPlay.localizedDescription) }
            }
            player.delegate = delegate
            guard player.play() else { throw PlaybackError.cannotPlay }
            self.delegate = delegate
            self.generation = generation
            self.player = player
            self.messageID = messageID
            isPlaying = true
        } catch {
            guard sessionRequest == request else { return }
            releaseSession()
            throw error
        }
    }

    /// Pauses playback, or cancels a play or resume still waiting for the audio session.
    func pause() {
        if let player, isPlaying {
            player.pause()
            isPlaying = false
        }
        releaseSession()
    }

    func resume() async throws {
        guard let player, sessionRequest == nil else { return }  // Already playing or resuming.
        guard let request = try await acquireSession(), sessionRequest == request, self.player === player else { return }
        if player.currentTime >= player.duration { player.currentTime = 0 }
        guard player.play() else {
            releaseSession()
            throw PlaybackError.cannotPlay
        }
        isPlaying = true
    }

    func stop() {
        generation = nil
        player?.stop()
        player = nil
        delegate = nil
        messageID = nil
        isPlaying = false
        releaseSession()
    }

    /// Activates the session for a new request; `nil` means it was superseded meanwhile.
    /// The caller keeps this identity across any further asynchronous preparation.
    private func acquireSession() async throws -> UUID? {
        let request = UUID()
        sessionRequest = request
        do {
            try await AudioSession.activate(for: .playback)
        } catch {
            guard sessionRequest == request else { return nil }
            sessionRequest = nil
            throw error
        }
        return sessionRequest == request ? request : nil
    }

    private func releaseSession() {
        guard sessionRequest != nil else { return }
        sessionRequest = nil
        AudioSession.deactivate()
    }
}

private enum PlaybackError: LocalizedError {
    case cannotPlay
    var errorDescription: String? { "The audio could not be played. Try again." }
}

private final class ReplyPlayerDelegate: NSObject, AVAudioPlayerDelegate, @unchecked Sendable {
    private let generation: UUID
    private let onFinish: @MainActor @Sendable (UUID, Bool) -> Void

    init(generation: UUID, onFinish: @escaping @MainActor @Sendable (UUID, Bool) -> Void) {
        self.generation = generation
        self.onFinish = onFinish
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let id = generation, callback = onFinish
        Task { @MainActor in callback(id, flag) }
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: (any Error)?) {
        let id = generation, callback = onFinish
        Task { @MainActor in callback(id, false) }
    }
}
