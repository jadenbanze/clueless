// dictationtest.swift — does a custom language model fix domain mishearings?
//
// The stock transcriber renders "Shweta" as "Shwada" and "read only" as
// "red only", and the error propagates into answers and summaries. Custom
// language models attach to DictationTranscriber (not SpeechTranscriber), via
// ContentHint.customizedLanguage(modelConfiguration:).
//
// Usage: dictationtest <audio-file> [--no-custom-lm]
//
// Prints the transcript, so the same file can be compared with and without the
// custom model.

import AVFoundation
import Foundation
import Speech

func log(_ message: String) {
    FileHandle.standardError.write("[dictation] \(message)\n".data(using: .utf8)!)
}

@main
struct DictationTest {
    /// Vocabulary the transcriber must get right. Seeded from the project's own
    /// language: people, ticket keys, and terms that appear in Jira and the repos.
    static let vocabulary: [String] = [
        "Shweta", "Kafka", "JWT", "RFMTSFM", "read-only", "read only",
        "restaurant profile", "restaurant profile service", "authentication failure",
        "Confluent Kafka", "sign-off", "IRSA", "Firehose", "JWT processing error",
        "rcp-restaurant-profile-api", "rcp-restaurant-profile-ui",
        "schema registry", "consumer group", "release blocker",
    ]

    static func optionValue(_ flag: String) -> String? {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: flag), index + 1 < args.count else { return nil }
        return args[index + 1]
    }

    /// Builds and compiles a custom language model, returning its location.
    static func buildCustomLanguageModel(locale: Locale) async -> URL? {
        let directory = FileManager.default.temporaryDirectory
        let assetURL = directory.appendingPathComponent("mc-vocabulary.bin")
        let compiledURL = directory.appendingPathComponent("mc-vocabulary-compiled.bin")

        do {
            let data = SFCustomLanguageModelData(
                locale: locale, identifier: "com.local.clueless", version: "1.0"
            ) {
                // Weight each phrase so it is preferred over the generic acoustic
                // interpretation, without overriding clearly articulated speech.
                for phrase in vocabulary {
                    SFCustomLanguageModelData.PhraseCount(phrase: phrase, count: 8)
                }
            }
            try await data.export(to: assetURL)
            log("exported vocabulary asset (\(vocabulary.count) phrases)")
        } catch {
            log("export failed: \(error)")
            return nil
        }

        do {
            let configuration = SFSpeechLanguageModel.Configuration(languageModel: compiledURL)
            try await SFSpeechLanguageModel.prepareCustomLanguageModel(
                for: assetURL, configuration: configuration)
            log("compiled custom language model")
            return compiledURL
        } catch {
            log("compile failed: \(error)")
            return nil
        }
    }

    static func main() async {
        let args = CommandLine.arguments
        guard let audioPath = args.dropFirst().first(where: { !$0.hasPrefix("-") }) else {
            log("usage: dictationtest <audio-file> [--no-custom-lm]")
            exit(64)
        }

        let locale = Locale(identifier: optionValue("--locale") ?? "en-US")

        var hints: Set<DictationTranscriber.ContentHint> = []
        if !args.contains("--no-custom-lm") {
            if let compiled = await buildCustomLanguageModel(locale: locale) {
                let configuration = SFSpeechLanguageModel.Configuration(languageModel: compiled)
                hints.insert(.customizedLanguage(modelConfiguration: configuration))
            }
        } else {
            log("custom language model disabled")
        }

        // The full initializer is required; the locale+preset convenience form
        // cannot carry content hints.
        let transcriber = DictationTranscriber(
            locale: locale,
            contentHints: hints,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [])
        log("content hints: \(hints.count)")

        guard
            let target = await SpeechAnalyzer.bestAvailableAudioFormat(
                compatibleWith: [transcriber])
        else {
            log("no compatible format")
            exit(5)
        }

        let url = URL(fileURLWithPath: audioPath)
        guard let file = try? AVAudioFile(forReading: url) else {
            log("cannot open \(audioPath)")
            exit(4)
        }

        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        let collector = Task { () -> [String] in
            var results: [String] = []
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters).trimmingCharacters(in: .whitespaces)
                    if !text.isEmpty { results.append(text) }
                }
            } catch {
                log("results error: \(error)")
            }
            return results
        }

        do {
            try await analyzer.start(inputSequence: stream)
            guard
                let converter = AVAudioConverter(from: file.processingFormat, to: target),
                let readBuffer = AVAudioPCMBuffer(
                    pcmFormat: file.processingFormat, frameCapacity: 8192)
            else {
                log("converter setup failed")
                exit(6)
            }

            while file.framePosition < file.length {
                readBuffer.frameLength = 0
                try file.read(into: readBuffer)
                if readBuffer.frameLength == 0 { break }

                let ratio = target.sampleRate / file.processingFormat.sampleRate
                let capacity = AVAudioFrameCount(Double(readBuffer.frameLength) * ratio) + 1024
                guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity)
                else { break }

                var consumed = false
                var conversionError: NSError?
                converter.convert(to: out, error: &conversionError) { _, status in
                    if consumed {
                        status.pointee = AVAudioConverterInputStatus.noDataNow
                        return nil
                    }
                    consumed = true
                    status.pointee = AVAudioConverterInputStatus.haveData
                    return readBuffer
                }
                if conversionError != nil { break }
                if out.frameLength > 0 { continuation.yield(AnalyzerInput(buffer: out)) }
            }

            continuation.finish()
            try await analyzer.finalizeAndFinishThroughEndOfInput()
        } catch {
            continuation.finish()
            log("analysis failed: \(error)")
            exit(7)
        }

        let results = await collector.value
        print(results.joined(separator: " "))
        exit(0)
    }
}
