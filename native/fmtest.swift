// fmtest.swift — is Apple's on-device model good enough to replace Ollama?
//
// Foundation Models runs on the Neural Engine, is always resident, and needs no
// separate process. That would remove model loading, GPU contention and ~5 GB of
// disk from this project. The question is whether a ~3B system model follows the
// grounding rules well enough.
//
// Usage: fmtest [--bench]

import Foundation
import FoundationModels

@main
struct FMTest {
    static func main() async {
        let model = SystemLanguageModel.default

        switch model.availability {
        case .available:
            print("availability: AVAILABLE")
        case .unavailable(let reason):
            print("availability: UNAVAILABLE (\(reason))")
            exit(2)
        @unknown default:
            print("availability: UNKNOWN")
            exit(2)
        }

        let instructions = """
        You are a meeting assistant whispering to one participant.

        The two sources have very different reliability.
        THE TRANSCRIPT IS RELIABLE. It is a verbatim record of what was said. Use
        it freely, including to infer an answer that was implied rather than stated.
        THE REFERENCE MATERIAL IS UNRELIABLE. It is retrieved by similarity and is
        frequently irrelevant. Use it only if it directly answers the question.

        Rules:
        - Be decisive and brief: 1-3 sentences. No preamble, no headings.
        - If neither source answers it, reply exactly: "No info in the loaded context."
        - Never invent ticket IDs, names, dates or code behaviour.
        - Never guess who is speaking; "me" and "them" are labels, not people.
        """

        let transcript = """
        them: We are blocked on the Kafka authentication failure for the restaurant profile service.
        me: Shweta is investigating the JWT processing error.
        them: Do we have a decision on read-only access before Friday?
        me: No, that still needs sign-off.
        """

        let cases: [(String, String)] = [
            ("What is blocking the release?", "kafka"),
            ("Who is investigating the JWT error?", "shweta"),
            ("What decision is needed before Friday?", "sign"),
            ("How do I bake sourdough bread?", "no info"),
        ]

        var correct = 0
        var totalMs = 0

        for (question, expectation) in cases {
            let session = LanguageModelSession(instructions: instructions)
            let prompt = """
            ### Question
            \(question)

            ### Recent transcript
            \(transcript)

            ### Reference material
            (no matching context found)

            Answer the question in the required style.
            """

            let started = Date()
            var answer = ""
            do {
                let response = try await session.respond(to: prompt)
                answer = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
            } catch {
                answer = "<error: \(error)>"
            }
            let ms = Int(Date().timeIntervalSince(started) * 1000)
            totalMs += ms

            let hit = answer.lowercased().contains(expectation)
            if hit { correct += 1 }
            print("\nQ: \(question)")
            print("A (\(ms)ms): \(answer.replacingOccurrences(of: "\n", with: " "))")
            print("   expected '\(expectation)' -> \(hit ? "OK" : "MISS")")
        }

        print("\n=== \(correct)/\(cases.count) correct, avg \(totalMs / cases.count)ms ===")
        exit(correct == cases.count ? 0 : 1)
    }
}
