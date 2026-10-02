import AVFoundation
import Observation

/// A finished recording, ready to become a chat message.
struct Recording: Sendable {
    let id: String
    let url: URL
    let startedAt: Date
    let duration: TimeInterval
}

enum RecorderError: LocalizedError {
    case permissionDenied
    case cannotStart(String)

    var errorDescription: String? {
        switch self {
        case .permissionDenied:
            "Microphone access is off. Allow it for Hermes Voice in Settings › Privacy & Security › Microphone."
        case .cannotStart(let reason):
            "Could not start recording: \(reason)"
        }
    }
}

/// AAC (.m4a) mono 16 kHz recorder with metering. Keeps recording in the background
/// (`UIBackgroundModes: audio`); an interruption ends the take and hands it over so no audio is lost.
@MainActor
@Observable
final class Recorder {
    private(set) var isRecording = false
    private(set) var elapsed: TimeInterval = 0
    /// Recent normalised input levels (0…1), oldest first.
    private(set) var levels: [Float] = Array(repeating: 0, count: Recorder.levelCount)

    /// Called when a take ends without the user asking (interruption, encoder error, media reset).
    @ObservationIgnored var onUnexpectedStop: ((Recording) -> Void)?

    static let levelCount = 48

    @ObservationIgnored private var recorder: AVAudioRecorder?
    @ObservationIgnored private var take: (id: String, url: URL, startedAt: Date)?
    /// Identifies the take whose audio session is still activating; `stop()`/`discard()` clear it.
    @ObservationIgnored private var pendingStart: UUID?
    @ObservationIgnored private var meterTask: Task<Void, Never>?
    @ObservationIgnored private let delegate = RecorderDelegate()
    @ObservationIgnored private var observers: [any NSObjectProtocol] = []

    init() {
        delegate.onFinish = { [weak self] url, successfully in
            self?.recorderFinished(url: url, successfully: successfully)
        }
        let center = NotificationCenter.default
        observers.append(center.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] notification in
            let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard raw.flatMap(AVAudioSession.InterruptionType.init(rawValue:)) == .began else { return }
            MainActor.assumeIsolated { self?.finishUnexpectedly(reason: "interruption") }
        })
        observers.append(center.addObserver(
            forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.finishUnexpectedly(reason: "media services reset") }
        })
    }

    static func requestPermission() async -> Bool {
        switch AVAudioApplication.shared.recordPermission {
        case .granted: true
        case .denied: false
        default: await AVAudioApplication.requestRecordPermission()
        }
    }

    /// Starts a take once the audio session is active. Returns `false` when `stop()` or `discard()`
    /// cancelled the take while the session was activating.
    func start(id: String, url: URL) async throws -> Bool {
        guard !isRecording, pendingStart == nil else { return false }
        let request = UUID()
        pendingStart = request
        do {
            try await AudioSession.activate(for: .recording)
        } catch {
            guard pendingStart == request else { return false }
            pendingStart = nil
            throw RecorderError.cannotStart(error.localizedDescription)
        }
        guard pendingStart == request else { return false }
        let recorder: AVAudioRecorder
        do {
            recorder = try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<AVAudioRecorder, any Error>) in
                AudioSession.queue.async {
                    do {
                        let settings: [String: Any] = [
                            AVFormatIDKey: kAudioFormatMPEG4AAC,
                            AVSampleRateKey: 16_000,
                            AVNumberOfChannelsKey: 1,
                            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
                        ]
                        let prepared = try AVAudioRecorder(url: url, settings: settings)
                        guard prepared.prepareToRecord() else {
                            prepared.deleteRecording()
                            throw RecorderError.cannotStart("the microphone is unavailable")
                        }
                        continuation.resume(returning: prepared)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        } catch {
            guard pendingStart == request else { return false }
            pendingStart = nil
            AudioSession.deactivate()
            if let recordingError = error as? RecorderError { throw recordingError }
            throw RecorderError.cannotStart(error.localizedDescription)
        }
        guard pendingStart == request else {
            recorder.deleteRecording()
            return false
        }
        pendingStart = nil
        recorder.delegate = delegate
        recorder.isMeteringEnabled = true
        guard recorder.record() else {
            recorder.deleteRecording()
            AudioSession.deactivate()
            throw RecorderError.cannotStart("the microphone is unavailable")
        }
        self.recorder = recorder
        take = (id, url, .now)
        elapsed = 0
        levels = Array(repeating: 0, count: Self.levelCount)
        isRecording = true
        startMetering()
        Log.audio.info("Recording \(id, privacy: .public)")
        return true
    }

    /// Ends the take and returns it. A take still waiting for the audio session is cancelled.
    func stop() -> Recording? {
        guard let recorder, let take else {
            cancelPendingStart()
            return nil
        }
        let duration = max(recorder.currentTime, elapsed)
        self.recorder = nil  // before stop(): the delegate callback then knows it was us
        recorder.stop()
        reset()
        return Recording(id: take.id, url: take.url, startedAt: take.startedAt, duration: duration)
    }

    /// Ends the take and deletes the file. A take still waiting for the audio session is cancelled.
    func discard() {
        guard let recorder else {
            cancelPendingStart()
            return
        }
        self.recorder = nil
        recorder.stop()
        recorder.deleteRecording()
        reset()
    }

    // MARK: Private

    private func finishUnexpectedly(reason: String) {
        guard isRecording, let recording = stop() else { return }
        Log.audio.notice("Recording ended by \(reason, privacy: .public); sending what was captured")
        onUnexpectedStop?(recording)
    }

    private func recorderFinished(url: URL, successfully: Bool) {
        // Only reacts when the system stopped the current recorder (e.g. encoder error or disk full).
        guard let recorder, recorder.url == url else { return }
        finishUnexpectedly(reason: successfully ? "system" : "recorder error")
    }

    private func reset() {
        meterTask?.cancel()
        meterTask = nil
        take = nil
        isRecording = false
        AudioSession.deactivate()
    }

    private func cancelPendingStart() {
        guard pendingStart != nil else { return }
        pendingStart = nil
        // Queued behind the pending activation, so the session never stays active for a cancelled take.
        AudioSession.deactivate()
    }

    private func startMetering() {
        meterTask?.cancel()
        meterTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                self.sampleMeter()
                try? await Task.sleep(for: .milliseconds(60))
            }
        }
    }

    private func sampleMeter() {
        guard let recorder else { return }
        recorder.updateMeters()
        let decibels = recorder.averagePower(forChannel: 0)
        let level = max(0, min(1, (decibels + 50) / 50))
        levels.removeFirst()
        levels.append(level)
        elapsed = recorder.currentTime
    }
}

/// `AVAudioRecorderDelegate` lives on a separate NSObject so `Recorder` can stay a plain observable class.
private final class RecorderDelegate: NSObject, AVAudioRecorderDelegate, @unchecked Sendable {
    // Set once in Recorder.init, before any callback can fire.
    nonisolated(unsafe) var onFinish: (@MainActor (URL, Bool) -> Void)?

    func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder, successfully flag: Bool) {
        deliver(recorder.url, flag)
    }

    func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder, error: (any Error)?) {
        deliver(recorder.url, false)
    }

    private func deliver(_ url: URL, _ successfully: Bool) {
        let onFinish = onFinish
        Task { @MainActor in onFinish?(url, successfully) }
    }
}

/// The app's only owner of `AVAudioSession`, shared by `Recorder` and `ReplyPlayer`.
/// Category changes and (de)activation are blocking IPC calls, so they run on one serial queue
/// instead of the main thread. Requests execute in main-actor call order: a deactivation can
/// neither overtake an activation requested before it nor affect one requested after it.
enum AudioSession {
    enum Purpose: Sendable { case recording, playback }

    static let queue = DispatchQueue(label: "com.artbred.hermesapp.audio-session", qos: .userInitiated)

    /// Configures and activates the session for `purpose`, resuming once it is active.
    @MainActor
    static func activate(for purpose: Purpose) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            // Enqueued before this call suspends, which keeps the queue in call order.
            queue.async {
                do {
                    let session = AVAudioSession.sharedInstance()
                    switch purpose {
                    case .recording: try session.setCategory(.record, mode: .default, options: [.allowBluetoothHFP])
                    case .playback: try session.setCategory(.playback, mode: .spokenAudio)
                    }
                    try session.setActive(true)
                    continuation.resume()
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    /// Deactivates the session once every earlier request has run, letting other apps' audio resume.
    @MainActor
    static func deactivate() {
        queue.async {
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }
}
