// speechtest.swift — proves the macOS 26 on-device Speech API works here.
//
// Validates, in order: availability, locale support, asset installation (the
// model downloads from Apple's CDN, which is reachable on this network unlike
// HuggingFace), and actual transcription of a known audio file.
//
// Usage: speechtest [--file <audio>] [--locale en-US]

import AVFoundation
import Foundation
import Speech

func log(_ message: String) {
    FileHandle.standardError.write("[speechtest] \(message)\n".data(using: .utf8)!)
}

func option(_ flag: String) -> String? {
    let args = CommandLine.arguments
    guard let index = args.firstIndex(of: flag), index + 1 < args.count else { return nil }
    return args[index + 1]
}

@main
struct SpeechTest {
    static func main() async {
        let localeID = option("--locale") ?? "en-US"
        let locale = Locale(identifier: localeID)

        log("SpeechTranscriber.isAvailable = \(SpeechTranscriber.isAvailable)")
        guard SpeechTranscriber.isAvailable else {
            log("RESULT: FAIL — SpeechTranscriber unavailable on this system")
            exit(1)
        }

        let supported = await SpeechTranscriber.supportedLocales
        let installed = await SpeechTranscriber.installedLocales
        log("supported locales: \(supported.count), installed: \(installed.count)")
        log("installed: \(installed.map(\.identifier).sorted().prefix(8).joined(separator: ", "))")

        guard let resolved = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            log("RESULT: FAIL — locale \(localeID) not supported")
            exit(2)
        }
        log("resolved locale: \(resolved.identifier)")

        // `.transcription` reports only finalized results. The progressive
        // presets additionally emit volatile partials that restate the whole
        // utterance as it grows, which would duplicate text in the transcript.
        let transcriber = SpeechTranscriber(locale: resolved, preset: .transcription)

        var status = await AssetInventory.status(forModules: [transcriber])
        log("asset status: \(status)")

        if status != .installed {
            log("requesting asset installation (downloads from Apple's CDN)…")
            do {
                if let request = try await AssetInventory.assetInstallationRequest(
                    supporting: [transcriber])
                {
                    try await request.downloadAndInstall()
                    log("asset installation completed")
                } else {
                    log("no installation request returned — assets may already be present")
                }
            } catch {
                log("RESULT: FAIL — asset installation failed: \(error)")
                exit(3)
            }
            status = await AssetInventory.status(forModules: [transcriber])
            log("asset status after install: \(status)")
        }

        guard let path = option("--file") else {
            log("RESULT: SUCCESS — API available and assets installed (no --file to transcribe)")
            exit(0)
        }

        await transcribe(file: path, using: transcriber)
    }

    static func transcribe(file path: String, using transcriber: SpeechTranscriber) async {
        let url = URL(fileURLWithPath: path)
        guard let audioFile = try? AVAudioFile(forReading: url) else {
            log("RESULT: FAIL — could not open \(path)")
            exit(4)
        }
        log("input: \(audioFile.length) frames @ \(audioFile.fileFormat.sampleRate) Hz")

        // The analyzer dictates its own preferred format; converting up front
        // avoids it rejecting mismatched input.
        guard
            let target = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: [transcriber])
        else {
            log("RESULT: FAIL — no compatible audio format")
            exit(5)
        }
        log("analyzer format: \(target.sampleRate) Hz, \(target.channelCount)ch")

        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        // Collect results concurrently with feeding audio, since the analyzer
        // back-pressures on its input while producing output.
        let collector = Task {
            var finals: [String] = []
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters)
                    if !text.isEmpty { finals.append(text) }
                }
            } catch {
                log("result stream error: \(error)")
            }
            return finals
        }

        do {
            try await analyzer.start(inputSequence: stream)

            guard
                let converter = AVAudioConverter(from: audioFile.processingFormat, to: target),
                let readBuffer = AVAudioPCMBuffer(
                    pcmFormat: audioFile.processingFormat, frameCapacity: 8192)
            else {
                log("RESULT: FAIL — could not build converter")
                exit(6)
            }

            while audioFile.framePosition < audioFile.length {
                readBuffer.frameLength = 0
                try audioFile.read(into: readBuffer)
                if readBuffer.frameLength == 0 { break }

                let ratio = target.sampleRate / audioFile.processingFormat.sampleRate
                let capacity = AVAudioFrameCount(Double(readBuffer.frameLength) * ratio) + 1024
                guard
                    let outBuffer = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity)
                else { break }

                var consumed = false
                var conversionError: NSError?
                converter.convert(to: outBuffer, error: &conversionError) { _, status in
                    if consumed {
                        status.pointee = .noDataNow
                        return nil
                    }
                    consumed = true
                    status.pointee = .haveData
                    return readBuffer
                }
                if let conversionError {
                    log("conversion error: \(conversionError)")
                    break
                }
                if outBuffer.frameLength > 0 {
                    continuation.yield(AnalyzerInput(buffer: outBuffer))
                }
            }

            continuation.finish()
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        } catch {
            continuation.finish()
            log("RESULT: FAIL — analysis error: \(error)")
            exit(7)
        }

        let finals = await collector.value
        let combined = finals.joined(separator: " ").trimmingCharacters(in: .whitespaces)
        log("segments: \(finals.count)")
        print(combined)

        if combined.isEmpty {
            log("RESULT: EMPTY — pipeline ran but produced no text")
            exit(8)
        }
        log("RESULT: SUCCESS — transcribed \(combined.count) characters on-device")
        exit(0)
    }
}
