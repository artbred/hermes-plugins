import Foundation
import Testing
@testable import HermesVoice

@MainActor
@Suite("Voice and reply audio boundaries", .serialized)
struct VoicePlaybackTests {
    @Test("Recorded voice never plays or synthesizes, whether its original file is available or missing", arguments: [false, true])
    func originalRecordingCannotPlay(isStored: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = StubServer { _ in .json(500, #"{"error":"Unexpected network request"}"#) }
        let store = ChatStore(directory: directory)
        let name = "original.wav"
        let original = Self.wave(seconds: 8)
        if isStored { try original.write(to: store.audioURL(fileName: name)) }
        let message = ChatMessage(role: .user, input: .voice, text: "My original words", stage: .completed, audioFileName: name)
        let chat = Chat(messages: [message], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        defer { model.stopPlayback() }

        await model.play(message)
        #expect(model.playingMessageID == nil)
        #expect(!model.isAudioPlaying)
        #expect(model.synthesizingMessageIDs.isEmpty)
        #expect(model.alert == nil)
        #expect(server.requests.isEmpty)
        #expect(store.chat(id: chat.id)?.messages[0] == message)
        #expect(ChatStore(directory: directory).chat(id: chat.id)?.messages[0] == message)
        #expect(FileManager.default.fileExists(atPath: store.audioURL(fileName: name).path) == isStored)
        if isStored {
            #expect(try Data(contentsOf: store.audioURL(fileName: name)) == original)
        }
    }

    @Test("Cached assistant audio supports pause and resume while recorded input cannot interrupt it", arguments: [MessageInput.text, .voice])
    func assistantPlaybackRemainsAvailable(input: MessageInput) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let server = StubServer { _ in .json(500, #"{"error":"Unexpected network request"}"#) }
        let store = ChatStore(directory: directory)
        let audio = Self.wave(seconds: 8)
        try audio.write(to: store.audioURL(fileName: "original.wav"))
        try audio.write(to: store.audioURL(fileName: "reply.wav"))
        let user = ChatMessage(role: .user, input: .voice, text: "My original words", stage: .completed, audioFileName: "original.wav", recordingDuration: 8)
        let reply = ChatMessage(role: .assistant, input: input, text: "The assistant reply", stage: .completed, audioFileName: "reply.wav")
        let chat = Chat(messages: [user, reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(store: store, client: server.client())
        defer { model.stopPlayback() }

        await model.play(reply)
        #expect(model.playingMessageID == reply.id)
        #expect(model.isAudioPlaying)
        await model.play(user)
        #expect(model.playingMessageID == reply.id)
        #expect(model.isAudioPlaying)
        await model.play(reply)
        #expect(model.playingMessageID == reply.id)
        #expect(!model.isAudioPlaying)
        await model.play(user)
        #expect(model.playingMessageID == reply.id)
        #expect(!model.isAudioPlaying)
        await model.play(reply)
        #expect(model.playingMessageID == reply.id)
        #expect(model.isAudioPlaying)
        #expect(model.alert == nil)
        #expect(server.requests.isEmpty)
        #expect(store.chat(id: chat.id)?.messages == [user, reply])
        #expect(try Data(contentsOf: store.audioURL(fileName: "original.wav")) == audio)
        #expect(try Data(contentsOf: store.audioURL(fileName: "reply.wav")) == audio)
    }

    @Test("Stopping or pausing while the audio session activates never starts stale playback")
    func staleActivationDoesNotPlay() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("reply.wav")
        try Self.wave(seconds: 4).write(to: url)
        let player = ReplyPlayer()
        defer { player.stop() }
        let gate = DispatchSemaphore(value: 0)

        AudioSession.queue.async { gate.wait() }  // Keeps the next activation pending.
        let stopped = Task { try await player.play(url: url, messageID: "stopped") }
        await Task.yield()
        player.stop()
        gate.signal()
        try await stopped.value
        let playsAfterStop = player.isPlaying || player.messageID != nil
        #expect(!playsAfterStop)

        try await player.play(url: url, messageID: "reply")
        player.pause()
        AudioSession.queue.async { gate.wait() }
        let resumed = Task { try await player.resume() }
        await Task.yield()
        player.pause()
        gate.signal()
        try await resumed.value
        let playsAfterPause = player.isPlaying
        let loaded = player.messageID
        #expect(!playsAfterPause)
        #expect(loaded == "reply")
    }

    @Test("Discarding a take while the microphone session activates never starts recording")
    func discardWhileStarting() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("take.m4a")
        let recorder = Recorder()
        let gate = DispatchSemaphore(value: 0)

        AudioSession.queue.async { gate.wait() }  // Keeps the activation pending.
        let start = Task { try await recorder.start(id: "take", url: url) }
        await Task.yield()
        recorder.discard()
        gate.signal()
        let started = try await start.value
        let recording = recorder.isRecording
        let fileExists = FileManager.default.fileExists(atPath: url.path)
        #expect(!started)
        #expect(!recording)
        #expect(!fileExists)
    }

    @Test("The old hosted endpoint migrates without changing custom servers", arguments: [
        ("https://notes.sashakuzina.com", "https://hermes.sashakuzina.com"),
        ("https://notes.sashakuzina.com/", "https://hermes.sashakuzina.com/"),
        ("https://notes.sashakuzina.com/p/work", "https://hermes.sashakuzina.com/p/work"),
        ("https://custom.example.com/", "https://custom.example.com/"),
        ("http://192.168.1.10:8642", "http://192.168.1.10:8642"),
        ("https://notes.sashakuzina.com:9443", "https://notes.sashakuzina.com:9443")
    ])
    func endpointMigration(input: String, expected: String) {
        #expect(AppSettings.migratedURL(input) == expected)
    }

    private static func wave(seconds: Int) -> Data {
        let byteCount = seconds * 8_000 * 2
        var data = Data("RIFF".utf8)
        func append<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
        }
        append(UInt32(36 + byteCount)); data.append(Data("WAVEfmt ".utf8))
        append(UInt32(16)); append(UInt16(1)); append(UInt16(1))
        append(UInt32(8_000)); append(UInt32(16_000)); append(UInt16(2)); append(UInt16(16))
        data.append(Data("data".utf8)); append(UInt32(byteCount)); data.append(Data(repeating: 0, count: byteCount))
        return data
    }
}
