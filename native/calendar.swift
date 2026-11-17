// calendar.swift — on-device Calendar context bridge using Apple's EventKit.
//
// Reads active and upcoming meetings already synced to macOS Calendar (iCloud,
// Exchange/Outlook, Google, or .ics subscriptions) with zero external API keys.
//
// Usage:
//   calendar --check                      permission status only, never prompts
//   calendar [--window-minutes 90]        events overlapping now..+window (no prompt)
//   calendar --request                    prompts for Calendar access if undecided

import EventKit
import Foundation

@main
struct CalendarCLI {
    static func emit(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let line = String(data: data, encoding: .utf8)
        else { return }
        print(line)
    }

    static func statusName(_ status: EKAuthorizationStatus) -> String {
        switch status {
        case .fullAccess: return "authorized"
        case .writeOnly: return "writeOnly"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "notDetermined"
        @unknown default: return "authorized"
        }
    }

    static func optionInt(_ flag: String, default fallback: Int) -> Int {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: flag), index + 1 < args.count,
              let parsed = Int(args[index + 1]), parsed > 0
        else { return fallback }
        return parsed
    }

    static func cleanNotes(_ notes: String?) -> String {
        guard let notes, !notes.isEmpty else { return "" }
        let boilerplate = [
            "microsoft teams", "join the meeting", "meeting id:", "passcode:",
            "dial in by phone", "zoom.us/j/", "webex.com", "___", "---",
        ]
        var kept: [String] = []
        for rawLine in notes.components(separatedBy: .newlines) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { continue }
            let low = line.lowercased()
            if boilerplate.contains(where: { low.contains($0) }) { continue }
            if low.hasPrefix("http://") || low.hasPrefix("https://") { continue }
            kept.append(line)
            if kept.count >= 8 { break }
        }
        return String(kept.joined(separator: "\n").prefix(500))
    }

    static func main() async {
        let args = CommandLine.arguments
        let currentStatus = EKEventStore.authorizationStatus(for: .event)

        if args.contains("--check") {
            let name = statusName(currentStatus)
            emit(["status": name, "authorized": name == "authorized"])
            exit(0)
        }

        let store = EKEventStore()
        var authorized = (currentStatus == .fullAccess)

        if !authorized && args.contains("--request") && currentStatus == .notDetermined {
            authorized = (try? await store.requestFullAccessToEvents()) ?? false
        }

        guard authorized else {
            let finalStatus = EKEventStore.authorizationStatus(for: .event)
            emit(["status": statusName(finalStatus), "authorized": false, "events": []])
            exit(0)
        }

        let windowMinutes = optionInt("--window-minutes", default: 90)
        let now = Date()
        let start = now.addingTimeInterval(-15 * 60)
        let end = now.addingTimeInterval(Double(windowMinutes) * 60)
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let formatter = ISO8601DateFormatter()

        let events = store.events(matching: predicate)
            .filter { $0.status != .canceled }
            .sorted { $0.startDate < $1.startDate }
            .prefix(5)
            .map { event -> [String: Any] in
                let attendees = (event.attendees ?? [])
                    .compactMap { $0.name?.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .filter { !$0.isEmpty && !$0.contains("@") }
                let isCurrent = event.startDate <= now.addingTimeInterval(5 * 60) && event.endDate >= now
                return [
                    "title": event.title ?? "Untitled event",
                    "start": formatter.string(from: event.startDate),
                    "end": formatter.string(from: event.endDate),
                    "allDay": event.isAllDay,
                    "current": isCurrent,
                    "attendees": Array(attendees.prefix(20)),
                    "notes": cleanNotes(event.notes),
                ]
            }

        emit(["status": "authorized", "authorized": true, "events": events])
    }
}
