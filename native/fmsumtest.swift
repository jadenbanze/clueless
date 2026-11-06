// fmsumtest.swift — can the on-device system model summarise a meeting?
//
// Summarisation is the harder task for a ~3B model: it must follow a fixed
// section structure, stay grounded, and not invent owners. If this holds, the
// summary path can move off Ollama too.

import Foundation
import FoundationModels

@main
struct FMSumTest {
    static func main() async {
        guard case .available = SystemLanguageModel.default.availability else {
            print("model unavailable")
            exit(2)
        }

        let instructions = """
        You summarise a work meeting from its transcript.

        Produce markdown with exactly these sections:

        ## Summary
        2-4 sentences on what the meeting was about.

        ## Decisions
        Bullets. Only decisions actually made. Write "None recorded" if there are none.

        ## Action items
        Bullets formatted "owner — action". The owner must be a person's name that
        was actually said aloud. If no name was given, write "Unassigned — action".
        Never use the transcript speaker labels "me" or "them" as an owner.

        ## Open questions
        Bullets. Unresolved issues or things someone needs to follow up.

        Rules:
        - Only use what is in the transcript. Do not infer facts that were not said.
        - If a section has nothing, write "None recorded".
        """

        let transcript = """
        them: The objective is to create a more reliable predictable process around vulnerability scanning.
        them: This diagram shows the architecture of how the Snyk scan agent integrates with our development workflow.
        them: It executes the Snyk scans and generates a vulnerability report.
        them: The first step is to discover critical and high severity vulnerabilities.
        them: We are not going to analyse medium or low vulnerabilities.
        me: Who owns the remediation step?
        them: Nobody yet, it is unassigned. We need someone to own it before the release.
        them: Also we decided to keep the agent read-only for now, it will not push changes.
        """

        let session = LanguageModelSession(instructions: instructions)
        let started = Date()
        var text = ""
        do {
            let response = try await session.respond(to: "### Transcript\n\(transcript)")
            text = response.content
        } catch {
            print("error: \(error)")
            exit(1)
        }
        let ms = Int(Date().timeIntervalSince(started) * 1000)

        print(text)
        print("\n--- \(ms)ms ---")

        let lowered = text.lowercased()
        var failures = 0
        func check(_ name: String, _ ok: Bool) {
            print("  [\(ok ? "PASS" : "FAIL")] \(name)")
            if !ok { failures += 1 }
        }
        print("\nchecks:")
        for section in ["summary", "decisions", "action items", "open questions"] {
            check("has '\(section)'", lowered.contains(section))
        }
        check("mentions Snyk", lowered.contains("snyk"))
        check("captures the read-only decision", lowered.contains("read-only") || lowered.contains("read only"))
        check("does not use 'them —' as an owner", !lowered.contains("them —") && !lowered.contains("them -"))

        exit(failures == 0 ? 0 : 1)
    }
}
