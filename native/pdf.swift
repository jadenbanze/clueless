// pdf.swift — fast on-device PDF text extractor using Apple's PDFKit.
//
// Zero external dependencies and zero network access: reads local lecture
// slides, research papers, or clinical guidelines and emits one JSON line per
// non-empty page:
//   {"page": 1, "text": "..."}
//
// Usage: pdf <pdf-path> [--max-pages 300]

import Foundation
import PDFKit

@main
struct PDFExtractor {
    static func optionInt(_ flag: String, default fallback: Int) -> Int {
        let args = CommandLine.arguments
        guard let index = args.firstIndex(of: flag), index + 1 < args.count,
              let parsed = Int(args[index + 1]), parsed > 0
        else { return fallback }
        return parsed
    }

    static func main() {
        let args = CommandLine.arguments
        guard let path = args.dropFirst().first(where: { !$0.hasPrefix("-") }) else {
            FileHandle.standardError.write("usage: pdf <pdf-path> [--max-pages N]\n".data(using: .utf8)!)
            exit(64)
        }

        let url = URL(fileURLWithPath: path)
        guard let document = PDFDocument(url: url) else {
            FileHandle.standardError.write("cannot open PDF at \(url.path)\n".data(using: .utf8)!)
            exit(1)
        }

        let maxPages = min(document.pageCount, optionInt("--max-pages", default: 300))
        for index in 0..<maxPages {
            guard let page = document.page(at: index),
                  let raw = page.string?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !raw.isEmpty
            else { continue }

            let payload: [String: Any] = ["page": index + 1, "text": raw]
            if let data = try? JSONSerialization.data(withJSONObject: payload),
               let line = String(data: data, encoding: .utf8)
            {
                print(line)
            }
        }
    }
}
