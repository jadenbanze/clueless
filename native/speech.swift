// speech.swift — on-device transcription and JSON transport for the helper.
//
// Uses the macOS 26 Speech framework (SpeechAnalyzer + SpeechTranscriber), which
// runs entirely on-device and needs no model download. faster-whisper was the
// original plan but its weights come from HuggingFace, which the corporate
// network blocks; Apple's asset CDN is reachable.

import AVFoundation
import Foundation
import Speech

// MARK: - JSON output

/// Emits one JSON object per line, either to a Unix socket or to stdout.
///
/// The socket exists because the system-audio instance is started through
/// LaunchServices (`open -n`), which discards stdout entirely. Writing to a
/// socket gives both launch paths the same transport.
final class JsonLineSink {
    private let fd: Int32
    private let lock = NSLock()

    init?(socketPath: String, timeoutSeconds: Double = 10) {
        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            log("could not create socket")
            return nil
        }

        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8)
        guard pathBytes.count < MemoryLayout.size(ofValue: address.sun_path) else {
            log("socket path too long: \(socketPath)")
            Darwin.close(descriptor)
            return nil
        }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: pathBytes)
        }

        // The listener may still be starting up, so retry briefly.
        let deadline = Date().addingTimeInterval(timeoutSeconds)
        var connected = false
        while Date() < deadline {
            let result = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                }
            }
            if result == 0 { connected = true; break }
            Thread.sleep(forTimeInterval: 0.2)
        }

        guard connected else {
            log("could not connect to \(socketPath) within \(timeoutSeconds)s")
            Darwin.close(descriptor)
            return nil
        }
        self.fd = descriptor
        log("connected to \(socketPath)")
    }

    init(stdoutOnly: Void) {
        self.fd = 1
    }

    func send(_ object: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(object),
            let data = try? JSONSerialization.data(withJSONObject: object),
            var line = String(data: data, encoding: .utf8)
        else { return }
        line += "\n"
        let bytes = Array(line.utf8)
        lock.lock()
        defer { lock.unlock() }
        var offset = 0
        while offset < bytes.count {
            let written = bytes.withUnsafeBytes { buffer -> Int in
                write(fd, buffer.baseAddress!.advanced(by: offset), bytes.count - offset)
            }
            if written <= 0 { return }  // peer went away; drop silently
            offset += written
        }
    }

    func close() {
        if fd > 2 { Foundation.close(fd) }
    }
}

// MARK: - Transcription engine

/// Wraps one SpeechTranscriber for one audio source.
///
/// Audio arrives from a realtime IOProc, so buffers are queued under a lock and
/// drained by a dedicated thread. Conversion and analysis therefore never run on
/// the audio thread.
final class TranscriptionEngine {
    private let transcriber: SpeechTranscriber
    private let analyzer: SpeechAnalyzer
    private let analyzerFormat: AVAudioFormat
    private let converter: AVAudioConverter?
    private let sourceFormat: AVAudioFormat
    private let source: String
    private let sink: JsonLineSink

    private let continuation: AsyncStream<AnalyzerInput>.Continuation
    private var resultsTask: Task<Void, Never>?
    private var drainThread: Thread?

    private var pending: [AVAudioPCMBuffer] = []
    private let pendingLock = NSLock()
    private var running = true
    private var flushTask: Task<Void, Never>?
    /// Frames handed to the analyzer so far, used to express flush position as
    /// a CMTime. Guarded because the drain thread and flush task both read it.
    private var fedFrames: Int = 0
    /// Frames since the last flush, and how many of the most recent are silence.
    private var speechFrames: Int = 0
    private var silentFrames: Int = 0
    private let framesLock = NSLock()

    /// `sourceFormat` must describe the buffers passed to `feed`.
    init?(
        source: String, sourceFormat: AVAudioFormat, locale: Locale, sink: JsonLineSink
    ) async {
        self.source = source
        self.sourceFormat = sourceFormat
        self.sink = sink

        guard let resolved = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            log("locale \(locale.identifier) unsupported for transcription")
            return nil
        }

        // SpeechTranscriber, not DictationTranscriber.
        //
        // DictationTranscriber was more accurate on a single finished file — it
        // rendered "Shweta" and "read only" correctly where this one produces
        // "Shwada" and "red-only". But it is unusable for continuous streaming:
        //
        //   .longDictation             never emits until finalized, so a live
        //                              meeting yields audio and no transcript.
        //   .progressiveLongDictation  emits volatile partials that restate the
        //                              growing utterance ("Block", "Blocking",
        //                              "Blocking the", …).
        //   .longDictation + periodic  each flush re-decodes a sliding window,
        //   finalize()                 giving overlapping fragments and words cut
        //                              mid-phrase ("the cough authentication
        //                              failure" for "the Kafka authentication
        //                              failure").
        //
        // SpeechTranscriber streams clean finalized segments with punctuation.
        // The domain-vocabulary errors are corrected deterministically on the
        // Python side (see SPEECH_CORRECTIONS in config.py), which is verifiable
        // in a way that mid-stream re-decoding is not.
        let transcriber = SpeechTranscriber(locale: resolved, preset: .transcription)
        self.transcriber = transcriber

        // Fetch the on-device model if this locale has not been used before.
        var status = await AssetInventory.status(forModules: [transcriber])
        if status != .installed {
            log("installing speech assets for \(resolved.identifier)…")
            do {
                if let request = try await AssetInventory.assetInstallationRequest(
                    supporting: [transcriber])
                {
                    try await request.downloadAndInstall()
                }
            } catch {
                log("asset installation failed: \(error)")
                return nil
            }
            status = await AssetInventory.status(forModules: [transcriber])
        }
        log("speech assets for \(resolved.identifier): \(status)")

        guard
            let target = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: [transcriber])
        else {
            log("no compatible analyzer format")
            return nil
        }
        self.analyzerFormat = target
        self.converter = AVAudioConverter(from: sourceFormat, to: target)
        if self.converter == nil {
            log("ERROR: no AVAudioConverter from \(sourceFormat) to \(target)")
            return nil
        }
        log(
            "engine[\(source)]: source \(sourceFormat.sampleRate)Hz/\(sourceFormat.channelCount)ch"
                + " -> analyzer \(target.sampleRate)Hz/\(target.channelCount)ch")

        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        self.continuation = continuation
        self.analyzer = SpeechAnalyzer(modules: [transcriber])
        do {
            try await analyzer.start(inputSequence: stream)
        } catch {
            log("analyzer start failed: \(error)")
            return nil
        }

        resultsTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await result in self.transcriber.results {
                    let text = String(result.text.characters).trimmingCharacters(in: .whitespaces)
                    guard !text.isEmpty else { continue }
                    self.sink.send([
                        "type": "turn",
                        "speaker": self.source,
                        "text": text,
                        "ts": Date().timeIntervalSince1970,
                    ])
                }
            } catch {
                log("results stream error: \(error)")
            }
        }

        startDraining()
        // No flush cadence: SpeechTranscriber emits finalized segments on its
        // own. The flusher below is retained only for the DictationTranscriber
        // experiments documented above and is not started.
    }

    /// Finalizes analysis when the speaker pauses.
    ///
    /// `.longDictation` buffers until finalized, so without a flush a live
    /// meeting yields healthy audio buffers and no transcript at all.
    ///
    /// The trigger is a pause in speech, not a timer. Flushing on a timer
    /// re-decodes a sliding window and emits overlapping fragments — measured
    /// as "Is blocking the restaurant profile release…", then "failure is
    /// blocking the restaurant profile release…" — because each flush covers
    /// audio that the previous one already reported. Waiting for silence gives
    /// one segment per utterance.
    ///
    /// A maximum duration bounds the case of continuous speech with no pause.
    private func startFlushing() {
        flushTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 400_000_000)
                guard let self, self.running else { return }

                let rate = self.analyzerFormat.sampleRate
                self.framesLock.lock()
                let silence = self.silentFrames
                let speech = self.speechFrames
                let position = self.fedFrames
                self.framesLock.unlock()

                guard speech > 0, position > 0 else { continue }

                // 0.4s catches normal inter-sentence pauses (synthesised speech
                // pauses for only ~0.3s); the 10s ceiling guarantees output
                // during continuous speech with no pause at all.
                let pausedLongEnough = Double(silence) / rate >= 0.4
                let speakingTooLong = Double(speech) / rate >= 10.0
                guard pausedLongEnough || speakingTooLong else { continue }

                let time = CMTime(
                    value: CMTimeValue(position), timescale: CMTimeScale(rate))
                do {
                    try await self.analyzer.finalize(through: time)
                    self.framesLock.lock()
                    self.speechFrames = 0
                    self.silentFrames = 0
                    self.framesLock.unlock()
                } catch {
                    log("finalize failed: \(error)")
                }
            }
        }
    }

    /// Called from the realtime IOProc: copies samples and returns immediately.
    func feed(bufferList: UnsafeMutableAudioBufferListPointer) {
        let list = bufferList
        for buffer in list {
            guard let raw = buffer.mData, buffer.mDataByteSize > 0 else { continue }
            guard
                let copy = AVAudioPCMBuffer(
                    pcmFormat: sourceFormat,
                    frameCapacity: AVAudioFrameCount(
                        Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
                        * (sourceFormat.channelCount > 1 ? 1 : 1))
                )
            else { continue }

            // Interleaved float32: byte count / (4 * channels) frames.
            let frames = Int(buffer.mDataByteSize)
                / (MemoryLayout<Float>.size * Int(sourceFormat.channelCount))
            copy.frameLength = AVAudioFrameCount(max(0, frames))
            if let destination = copy.mutableAudioBufferList.pointee.mBuffers.mData {
                memcpy(destination, raw, Int(buffer.mDataByteSize))
            }

            pendingLock.lock()
            pending.append(copy)
            // Bound memory if the drain thread falls behind.
            if pending.count > 400 { pending.removeFirst(pending.count - 400) }
            pendingLock.unlock()
        }
    }

    /// Copies and queues a buffer produced by AVAudioEngine. The engine reuses
    /// its buffer after the tap callback returns, so a deep copy is required.
    func feed(pcmBuffer: AVAudioPCMBuffer) {
        guard
            let copy = AVAudioPCMBuffer(
                pcmFormat: pcmBuffer.format, frameCapacity: pcmBuffer.frameLength)
        else { return }
        copy.frameLength = pcmBuffer.frameLength

        let source = UnsafeMutableAudioBufferListPointer(pcmBuffer.mutableAudioBufferList)
        let destination = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for (from, to) in zip(source, destination) {
            guard let sourceData = from.mData, let destinationData = to.mData else { continue }
            memcpy(destinationData, sourceData, Int(from.mDataByteSize))
        }

        pendingLock.lock()
        pending.append(copy)
        if pending.count > 400 { pending.removeFirst(pending.count - 400) }
        pendingLock.unlock()
    }

    private func startDraining() {
        let thread = Thread { [weak self] in
            guard let self else { return }
            while self.running {
                self.pendingLock.lock()
                let batch = self.pending
                self.pending.removeAll(keepingCapacity: true)
                self.pendingLock.unlock()

                for buffer in batch {
                    self.emit(buffer)
                }
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
        thread.name = "stt-drain-\(source)"
        thread.start()
        drainThread = thread
    }

    /// Diagnostics: distinguishes "no audio arrived" from "audio arrived but
    /// was not transcribed", which otherwise look identical from outside.
    private var emittedFrames: Int = 0
    private var convertedBuffers: Int = 0
    private var silencedBuffers: Int = 0

    /// Debounced speaking state. The overlay uses this to show that audio is
    /// arriving while the transcriber is still assembling a sentence — without
    /// it the panel looks frozen for several seconds mid-utterance.
    private var isSpeaking = false
    private var lastSpeechAt = Date.distantPast

    /// Absolute floor below which a buffer is never speech.
    private static let minimumRms: Float = 0.0025

    /// Multiple of the measured noise floor that counts as speech.
    ///
    /// A fixed threshold does not work. Measured in a quiet room, ambient noise
    /// alone produced "a car?" and "I've never." within 12 seconds, because the
    /// microphone's noise floor sits above any constant low value that would
    /// still admit soft speech. The gate therefore tracks the noise floor and
    /// requires signal to exceed it by a margin.
    private static let speechMultipleOfNoise: Float = 2.5

    /// Running estimate of the ambient level, adapted only on quiet buffers so
    /// that speech never drags the estimate upward.
    private var noiseFloor: Float = 0.002

    private func emit(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        emittedFrames += Int(buffer.frameLength)
        convertedBuffers += 1

        let now = Date()
        var level: Float = 0
        var energy: Double = 0
        if let channels = buffer.floatChannelData {
            let count = Int(buffer.frameLength)
            for index in 0..<count {
                let sample = channels[0][index]
                level = max(level, abs(sample))
                energy += Double(sample) * Double(sample)
            }
            if count > 0 {
                let rms = Float((energy / Double(count)).squareRoot())
                let threshold = max(Self.minimumRms, noiseFloor * Self.speechMultipleOfNoise)

                if rms < threshold {
                    // Quiet buffer: adapt the estimate toward it. Adapting only
                    // here keeps loud speech from inflating the floor.
                    noiseFloor = noiseFloor * 0.92 + rms * 0.08
                    silencedBuffers += 1
                    // Track silence so the flusher knows when speech has paused.
                    // Expressed in analyzer frames to match fedFrames.
                    let converted = Int(
                        Double(count) * (analyzerFormat.sampleRate / sourceFormat.sampleRate))
                    framesLock.lock()
                    silentFrames += converted
                    framesLock.unlock()

                    if isSpeaking, now.timeIntervalSince(lastSpeechAt) > 0.6 {
                        isSpeaking = false
                        sink.send(["type": "activity", "speaker": source, "speaking": false])
                    }
                    return
                }
            }
        }

        if convertedBuffers == 1 || convertedBuffers % 100 == 0 {
            log(
                "engine[\(source)] buffers=\(convertedBuffers) frames=\(emittedFrames)"
                    + " peak=\(level) gated=\(silencedBuffers) floor=\(noiseFloor)")
        }

        // Announce transitions only, debounced by 600ms of silence so a normal
        // pause between words does not flicker the indicator.
        lastSpeechAt = now
        if !isSpeaking {
            isSpeaking = true
            sink.send(["type": "activity", "speaker": source, "speaking": true])
        }

        let ratio = analyzerFormat.sampleRate / sourceFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: capacity)
        else { return }

        var consumed = false
        var error: NSError?
        converter.convert(to: output, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        if error != nil { return }
        if output.frameLength > 0 {
            continuation.yield(AnalyzerInput(buffer: output))
            framesLock.lock()
            fedFrames += Int(output.frameLength)
            speechFrames += Int(output.frameLength)
            silentFrames = 0  // speech resumed, so the pause is over
            framesLock.unlock()
        }
    }

    func finish() async {
        running = false
        drainThread?.cancel()
        flushTask?.cancel()
        // Flush anything still queued before closing the stream.
        pendingLock.lock()
        let remaining = pending
        pending.removeAll()
        pendingLock.unlock()
        remaining.forEach(emit)

        continuation.finish()
        do {
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        } catch {
            log("finalize failed: \(error)")
        }
        resultsTask?.cancel()
    }
}
