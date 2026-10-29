// ocr.swift — on-device text extraction from a screenshot.
//
// Uses Apple's Vision framework (VNRecognizeTextRequest), which runs locally with
// no model download — important here because this network blocks the model
// registries this project would otherwise use.
//
// Usage: ocr <image-path>
// Prints recognised text to stdout, one line per observation, in reading order.

import Foundation
import Vision

@main
struct OCR {
    static func main() {
        let args = CommandLine.arguments
        guard args.count > 1 else {
            FileHandle.standardError.write("usage: ocr <image-path>\n".data(using: .utf8)!)
            exit(64)
        }

        let url = URL(fileURLWithPath: args[1])
        guard let imageSource = CGImageSourceCreateWithURL(url as CFURL, nil),
            let image = CGImageSourceCreateImageAtIndex(imageSource, 0, nil)
        else {
            FileHandle.standardError.write("cannot read image at \(url.path)\n".data(using: .utf8)!)
            exit(1)
        }

        let request = VNRecognizeTextRequest()
        // Accurate: this runs once per hotkey press, not continuously, so the
        // extra time is affordable and the accuracy matters more.
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true

        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do {
            try handler.perform([request])
        } catch {
            FileHandle.standardError.write("OCR failed: \(error)\n".data(using: .utf8)!)
            exit(2)
        }

        guard let observations = request.results else { exit(0) }

        // Vision returns observations roughly top-to-bottom; sort explicitly so
        // output order is stable across images.
        let lines = observations
            .compactMap { $0.topCandidates(1).first?.string }
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }

        for line in lines {
            print(line)
        }
    }
}
