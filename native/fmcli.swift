// fmcli.swift — bridge from Python to Apple's on-device Foundation Model.
//
// Reads one JSON request from stdin and streams the answer to stdout as
// newline-delimited JSON:
//
//   {"delta":"partial text"}      repeated as tokens arrive
//   {"done":true,"ms":123}        once complete
//   {"error":"..."}               on failure
//
// Why a subprocess rather than a long-running server: generation averages under
// a second, so process startup (~40ms) is noise, and it keeps the failure mode
// simple — a crash costs one answer, not the session.
//
// Privacy: Foundation Models runs entirely on-device on the Neural Engine. No
// network calls, no model download, nothing leaves the machine.

import Foundation
import FoundationModels

struct Request: Decodable {
    let instructions: String
    let prompt: String
    /// 0 means "no limit".
    let maxTokens: Int?
}

func emit(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object),
        let line = String(data: data, encoding: .utf8)
    else { return }
    print(line)
    fflush(stdout)
}

@main
struct FMCLI {
    static func main() async {
        let input = FileHandle.standardInput.readDataToEndOfFile()
        guard let request = try? JSONDecoder().decode(Request.self, from: input) else {
            emit(["error": "invalid request JSON"])
            exit(64)
        }

        let model = SystemLanguageModel.default
        guard case .available = model.availability else {
            // Report precisely so the caller can fall back rather than fail.
            emit(["error": "model unavailable: \(model.availability)"])
            exit(2)
        }

        var options = GenerationOptions()
        if let maxTokens = request.maxTokens, maxTokens > 0 {
            options.maximumResponseTokens = maxTokens
        }

        let session = LanguageModelSession(instructions: request.instructions)
        let started = Date()

        do {
            let stream = session.streamResponse(to: request.prompt)
            var last = ""
            for try await partial in stream {
                let text = partial.content
                // The stream reports the full text so far, so emit only the
                // increment to keep the wire protocol append-only.
                if text.count > last.count, text.hasPrefix(last) {
                    emit(["delta": String(text.dropFirst(last.count))])
                    last = text
                } else if text != last {
                    // Non-prefix change: send a reset so the client can replace.
                    emit(["reset": text])
                    last = text
                }
            }
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            emit(["done": true, "ms": ms, "text": last])
        } catch {
            emit(["error": "\(error)"])
            exit(1)
        }
    }
}
