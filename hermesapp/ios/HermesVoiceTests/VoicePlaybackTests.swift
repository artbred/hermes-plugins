import Foundation
import Security
import Synchronization
import Testing
import UIKit
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
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let voice = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: voice.referenceID)
        let store = ChatStore(directory: directory)
        let audio = Self.wave(seconds: 8)
        try audio.write(to: store.audioURL(fileName: "original.wav"))
        try audio.write(to: store.audioURL(fileName: "reply.wav"))
        let user = ChatMessage(role: .user, input: .voice, text: "My original words", stage: .completed, audioFileName: "original.wav", recordingDuration: 8)
        let reply = ChatMessage(role: .assistant, input: input, text: "The assistant reply", stage: .completed, audioFileName: "reply.wav", speechVoice: voice)
        let chat = Chat(messages: [user, reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
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

    @Test("Legacy, wrong-voice and unpaid-model caches regenerate for Listen and automatic replies", arguments: [
        (false, Optional<SpeechVoice>.none), (true, Optional<SpeechVoice>.none),
        (false, Optional(SpeechVoice.defaultVoice)), (true, Optional(SpeechVoice.defaultVoice)),
        (false, Optional(SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef", modelID: "s2.1-pro-free"))),
        (true, Optional(SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef", modelID: "s2.1-pro-free")))
    ])
    func mismatchedCache(automatic: Bool, cachedVoice: SpeechVoice?) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let voice = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: voice.referenceID)
        let audio = Self.wave(seconds: 1).base64EncodedString()
        let server = StubServer { _ in Self.speechResponse(voice: voice, audio: audio) }
        let store = ChatStore(directory: directory)
        try Self.wave(seconds: 1).write(to: store.audioURL(fileName: "old.wav"))
        let recording = ChatMessage(role: .user, input: .voice, text: "Question", stage: .completed, audioFileName: "original.wav")
        try Self.wave(seconds: 1).write(to: store.audioURL(fileName: "original.wav"))
        let reply = ChatMessage(role: .assistant, input: automatic ? .voice : .text, text: "Answer", stage: .completed,
                                audioFileName: "old.wav", speechVoice: cachedVoice, needsSpeech: automatic)
        let chat = Chat(messages: [recording, reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        model.selectedChatID = nil

        if automatic {
            model.scenePhaseChanged(.active)
            try #require(await eventually { store.chat(id: chat.id)?.messages.last?.speechVoice == voice })
            try #require(await eventually { model.synthesizingMessageIDs.isEmpty })
        } else {
            await model.play(reply)
        }
        let saved = try #require(store.chat(id: chat.id)?.messages.last)
        let name = try #require(saved.audioFileName)
        #expect(saved.speechVoice == voice)
        #expect(!saved.needsSpeech)
        #expect(saved.error == nil)
        #expect(name.contains(voice.cacheKey))
        #expect(FileManager.default.fileExists(atPath: store.audioURL(fileName: name).path))
        #expect(!FileManager.default.fileExists(atPath: store.audioURL(fileName: "old.wav").path))
        #expect(store.chat(id: chat.id)?.messages.first == recording)
        #expect(FileManager.default.fileExists(atPath: store.audioURL(fileName: "original.wav").path))
        #expect(ChatStore(directory: directory).chat(id: chat.id)?.messages.last == saved)
        let request = try #require(server.requests.first)
        let body = try #require(JSONSerialization.jsonObject(with: request.body) as? [String: String])
        #expect(body["reference_id"] == voice.referenceID)
        #expect(body["model"] == "s2.1-pro")
        await model.play(saved)
        #expect(server.requests.count == 1)
    }

    @Test("A mismatched acknowledgment leaves the old cache unmodified and exposes a retryable audio error")
    func rejectedAcknowledgmentCanRetry() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let voice = settings.speechVoice
        let other = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        let audio = Self.wave(seconds: 1).base64EncodedString()
        let acknowledge = Mutex(false)
        let server = StubServer { _ in Self.speechResponse(voice: acknowledge.withLock { $0 } ? voice : other, audio: audio) }
        let store = ChatStore(directory: directory)
        try Self.wave(seconds: 1).write(to: store.audioURL(fileName: "old.wav"))
        let reply = ChatMessage(role: .assistant, text: "Answer", stage: .completed, audioFileName: "old.wav", speechVoice: other)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        model.selectedChatID = nil

        await model.play(reply)
        let failed = try #require(store.chat(id: chat.id)?.messages.last)
        #expect(failed.error?.hasPrefix("Audio: ") == true)
        #expect(failed.audioFileName == "old.wav")
        #expect(failed.speechVoice == other)
        #expect(FileManager.default.fileExists(atPath: store.audioURL(fileName: "old.wav").path))
        acknowledge.withLock { $0 = true }
        await model.play(failed)
        #expect(server.requests.count == 2)
        #expect(store.chat(id: chat.id)?.messages.last?.speechVoice == voice)
        #expect(store.chat(id: chat.id)?.messages.last?.error == nil)
    }

    @Test("A voice change discards a pending Listen even if the original voice is selected again", arguments: [false, true])
    func changedVoiceDiscardsPendingListen(changeBack: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let original = settings.speechVoice
        let changed = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        let audio = Self.wave(seconds: 8).base64EncodedString()
        let response = DeferredStubResponse()
        let firstRequest = Mutex(true)
        let server = StubServer { request in
            if firstRequest.withLock({ first in let result = first; first = false; return result }) {
                return .deferred(response)
            }
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: String]
            return Self.speechResponse(voice: SpeechVoice(referenceID: body?["reference_id"] ?? ""), audio: audio)
        }
        let store = ChatStore(directory: directory)
        let reply = ChatMessage(role: .assistant, text: "Answer", stage: .completed)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        let listen = Task { await model.play(reply) }
        defer {
            response.resolve(Self.speechResponse(voice: original, audio: audio))
            listen.cancel()
            model.stopPlayback()
        }
        try #require(await eventually { server.requests.count == 1 })
        try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: changed.referenceID)
        model.settingsChanged()
        if changeBack {
            try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: original.referenceID)
            model.settingsChanged()
        }
        response.resolve(Self.speechResponse(voice: original, audio: audio))
        await listen.value
        #expect(model.playingMessageID == nil)
        #expect(!model.isAudioPlaying)
        #expect(store.chat(id: chat.id)?.messages.last == reply)
        #expect(try FileManager.default.contentsOfDirectory(atPath: store.audioURL(fileName: "unused").deletingLastPathComponent().path).isEmpty)
        model.selectedChatID = nil
        await model.play(reply)
        #expect(server.requests.count == 2)
        #expect(store.chat(id: chat.id)?.messages.last?.speechVoice == settings.speechVoice)
    }

    @Test("A pending automatic reply hands off to the new voice without caching the discarded response")
    func changedVoiceHandsOffAutomaticReply() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let original = settings.speechVoice
        let changed = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        let audio = Self.wave(seconds: 1).base64EncodedString()
        let oldResponse = DeferredStubResponse()
        let newResponse = DeferredStubResponse()
        defer {
            oldResponse.resolve(Self.speechResponse(voice: original, audio: audio))
            newResponse.resolve(Self.speechResponse(voice: changed, audio: audio))
        }
        let server = StubServer { request in
            let body = (try? JSONSerialization.jsonObject(with: request.body)) as? [String: String]
            return .deferred(body?["reference_id"] == original.referenceID ? oldResponse : newResponse)
        }
        let store = ChatStore(directory: directory)
        let reply = ChatMessage(role: .assistant, input: .voice, text: "Answer", stage: .completed, needsSpeech: true)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        model.selectedChatID = nil
        model.scenePhaseChanged(.active)
        try #require(await eventually { server.requests.count == 1 })
        try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: changed.referenceID)
        model.settingsChanged()
        oldResponse.resolve(Self.speechResponse(voice: original, audio: audio))
        try #require(await eventually { server.requests.count == 2 })
        #expect(store.chat(id: chat.id)?.messages.last == reply)
        newResponse.resolve(Self.speechResponse(voice: changed, audio: audio))
        try #require(await eventually { store.chat(id: chat.id)?.messages.last?.speechVoice == changed && model.synthesizingMessageIDs.isEmpty })
        #expect(store.chat(id: chat.id)?.messages.last?.needsSpeech == false)
        #expect(store.chat(id: chat.id)?.messages.last?.error == nil)
        #expect(ChatStore(directory: directory).chat(id: chat.id)?.messages.last?.speechVoice == changed)
        await model.play(reply)
        #expect(server.requests.count == 2)
    }

    @Test("Voice selection stops loaded or paused audio without changing the chat or draft", arguments: [false, true])
    func changedVoiceStopsLoadedAudio(paused: Bool) async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let server = StubServer { _ in .json(500, #"{"error":"Unexpected request"}"#) }
        let store = ChatStore(directory: directory)
        try Self.wave(seconds: 8).write(to: store.audioURL(fileName: "reply.wav"))
        let reply = ChatMessage(role: .assistant, text: "Answer", stage: .completed, audioFileName: "reply.wav", speechVoice: settings.speechVoice)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        defer { model.stopPlayback() }
        model.draft = "Keep this draft"
        await model.play(reply)
        if paused { await model.play(reply) }
        try #require(model.playingMessageID == reply.id)
        try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: "0123456789abcdef0123456789abcdef")
        model.settingsChanged()
        #expect(model.playingMessageID == nil)
        #expect(!model.isAudioPlaying)
        #expect(model.draft == "Keep this draft")
        #expect(model.selectedChatID == chat.id)
        #expect(store.chat(id: chat.id)?.messages == [reply])
        #expect(server.requests.isEmpty)
    }

    @Test("Voice selection cancels cached audio still waiting for audio-session activation")
    func changedVoiceCancelsPendingActivation() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let server = StubServer { _ in .json(500, #"{"error":"Unexpected request"}"#) }
        let store = ChatStore(directory: directory)
        try Self.wave(seconds: 8).write(to: store.audioURL(fileName: "reply.wav"))
        let reply = ChatMessage(role: .assistant, text: "Answer", stage: .completed, audioFileName: "reply.wav", speechVoice: settings.speechVoice)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        let gate = DispatchSemaphore(value: 0)
        AudioSession.queue.async { gate.wait() }
        let listen = Task { await model.play(reply) }
        defer { gate.signal(); listen.cancel(); model.stopPlayback() }
        try #require(await eventually { model.synthesizingMessageIDs.contains(reply.id) })
        try settings.save(serverURL: AppSettings.defaultURL, token: "", voiceID: "0123456789abcdef0123456789abcdef")
        model.settingsChanged()
        gate.signal()
        await listen.value
        #expect(model.playingMessageID == nil)
        #expect(!model.isAudioPlaying)
        #expect(server.requests.isEmpty)
    }

    @Test("A failed cache-metadata commit preserves the previous audio and removes the uncommitted replacement")
    func failedCacheCommitPreservesOldAudio() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let voice = settings.speechVoice
        let audio = Self.wave(seconds: 1).base64EncodedString()
        let server = StubServer { _ in Self.speechResponse(voice: voice, audio: audio) }
        let store = ChatStore(directory: directory)
        let oldVoice = SpeechVoice(referenceID: "0123456789abcdef0123456789abcdef")
        try Self.wave(seconds: 1).write(to: store.audioURL(fileName: "old.wav"))
        let reply = ChatMessage(role: .assistant, text: "Answer", stage: .completed, audioFileName: "old.wav", speechVoice: oldVoice)
        let chat = Chat(messages: [reply], titleGenerated: true)
        try store.save(chat)
        let index = directory.appending(path: "chats.json")
        try FileManager.default.removeItem(at: index)
        try FileManager.default.createDirectory(at: index, withIntermediateDirectories: false)
        let model = AppModel(settings: settings, store: store, client: server.client())
        model.selectedChatID = nil

        await model.play(reply)
        #expect(store.chat(id: chat.id)?.messages == [reply])
        #expect(model.alert?.title == "Could not save audio state")
        #expect(try FileManager.default.contentsOfDirectory(atPath: store.audioURL(fileName: "old.wav").deletingLastPathComponent().path) == ["old.wav"])
        #expect(server.requests.count == 1)
    }

    @Test("An edited reply invalidates voice metadata and a pending synthesis cannot cache its old text")
    func editedReplyDiscardsPendingSpeech() async throws {
        let directory = makeTemporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let service = "HermesVoiceTests-\(UUID().uuidString)"
        defer { Self.clearSettings(service) }
        let settings = AppSettings(service: service)
        let voice = settings.speechVoice
        let audio = Self.wave(seconds: 1).base64EncodedString()
        let response = DeferredStubResponse()
        let firstRequest = Mutex(true)
        let server = StubServer { _ in
            if firstRequest.withLock({ first in let result = first; first = false; return result }) {
                return .deferred(response)
            }
            return Self.speechResponse(voice: voice, audio: audio)
        }
        let store = ChatStore(directory: directory)
        let reply = ChatMessage(role: .assistant, text: "Original answer", stage: .completed, remoteMessageID: "1")
        var chat = Chat(sessionID: "shared", messages: [reply], titleGenerated: true)
        try store.save(chat)
        let model = AppModel(settings: settings, store: store, client: server.client())
        model.selectedChatID = nil
        let listen = Task { await model.play(reply) }
        defer { response.resolve(Self.speechResponse(voice: voice, audio: audio)); listen.cancel() }
        try #require(await eventually { server.requests.count == 1 })
        let edited = try JSONDecoder().decode([RemoteMessage].self, from: Data(#"[{"id":1,"role":"assistant","content":"Edited answer"}]"#.utf8))
        chat.mergeHistory(edited, sessionID: "shared")
        try store.save(chat)
        response.resolve(Self.speechResponse(voice: voice, audio: audio))
        await listen.value
        #expect(store.chat(id: chat.id)?.messages.last?.text == "Edited answer")
        #expect(store.chat(id: chat.id)?.messages.last?.audioFileName == nil)
        #expect(store.chat(id: chat.id)?.messages.last?.speechVoice == nil)
        #expect(store.chat(id: chat.id)?.messages.last?.error == nil)
        await model.play(reply)
        let request = try #require(server.requests.last)
        let body = try #require(JSONSerialization.jsonObject(with: request.body) as? [String: String])
        #expect(body["text"] == "Edited answer")
        #expect(store.chat(id: chat.id)?.messages.last?.speechVoice == voice)
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

    @Test("The microphone-opening touch cannot dismiss a new recording, but a fresh outside tap can")
    func openingTouchDoesNotDiscardRecording() {
        let openingTouch = TimestampedRecordingTouch(timestamp: ProcessInfo.processInfo.systemUptime - 1)
        let discardView = RecordingDiscardView(frame: .zero)
        var discarded = 0
        discardView.discard = { discarded += 1 }
        discardView.touchesBegan([openingTouch], with: nil)
        #expect(discarded == 0)

        let freshTouch = TimestampedRecordingTouch(timestamp: ProcessInfo.processInfo.systemUptime)
        discardView.touchesBegan([freshTouch], with: nil)
        #expect(discarded == 1)
        #expect(discardView.accessibilityActivate())
        #expect(discarded == 2)
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

    private static func clearSettings(_ service: String) {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary)
    }

    private nonisolated static func speechResponse(voice: SpeechVoice, audio: String) -> StubResponse {
        .json(200, "{\"ok\":true,\"provider\":\"fish\",\"reference_id\":\"\(voice.referenceID)\",\"model\":\"\(voice.modelID)\",\"data_url\":\"data:audio/wav;base64,\(audio)\",\"mime_type\":\"audio/wav\"}")
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

@MainActor
private final class TimestampedRecordingTouch: UITouch {
    private let eventTimestamp: TimeInterval

    init(timestamp: TimeInterval) {
        eventTimestamp = timestamp
        super.init()
    }

    override var timestamp: TimeInterval { eventTimestamp }
    override var phase: UITouch.Phase { .began }
}
