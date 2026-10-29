// audiotap.swift — Core Audio Process Tap spike
//
// Captures audio from a target app (by bundle-ID substring) without any
// virtual audio driver, using the macOS 14.2+ process-tap API.
//
// Usage:
//   audiotap --list                     enumerate audio-producing processes
//   audiotap --bundle teams             tap all processes matching "teams"
//   audiotap --bundle teams --probe 5   tap for 5s and report level stats
//
// Raw PCM (Float32, interleaved, tap-native rate) goes to stdout.
// All diagnostics go to stderr so stdout stays a clean audio pipe.

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

// MARK: - Property helpers

func addr(
    _ selector: AudioObjectPropertySelector,
    _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(
        mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func propertySize(_ object: AudioObjectID, _ address: inout AudioObjectPropertyAddress) -> UInt32? {
    var size: UInt32 = 0
    let status = AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size)
    return status == noErr ? size : nil
}

/// Reads a fixed-layout property (structs, scalars) into a value of type T.
func readProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, default def: T)
    -> T?
{
    var address = addr(selector)
    var size = UInt32(MemoryLayout<T>.size)
    var value = def
    let status = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0)
    }
    return status == noErr ? value : nil
}

/// Reads a variable-length array property (e.g. object lists).
func readArrayProperty<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, of: T.Type)
    -> [T]
{
    var address = addr(selector)
    guard let size = propertySize(object, &address), size > 0 else { return [] }
    let count = Int(size) / MemoryLayout<T>.stride
    let buffer = UnsafeMutablePointer<T>.allocate(capacity: count)
    defer { buffer.deallocate() }
    var mutableSize = size
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &mutableSize, buffer)
    guard status == noErr else { return [] }
    return Array(UnsafeBufferPointer(start: buffer, count: count))
}

/// Reads a CFString property and returns it as a Swift String.
func readStringProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = addr(selector)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var cfStr: CFString? = nil
    let status = withUnsafeMutablePointer(to: &cfStr) {
        AudioObjectGetPropertyData(object, &address, 0, nil, &size, $0)
    }
    guard status == noErr, let result = cfStr else { return nil }
    return result as String
}

/// Optional mirror for diagnostics, set by --log. Needed because LaunchServices
/// discards stderr when the bundle is started with `open`.
var logFile: FileHandle?

func log(_ message: String) {
    let line = "[audiotap] \(message)\n".data(using: .utf8)!
    FileHandle.standardError.write(line)
    logFile?.write(line)
}

func fourCC(_ code: OSStatus) -> String {
    let bytes = [24, 16, 8, 0].map { UInt8(truncatingIfNeeded: code >> $0) }
    let printable = bytes.allSatisfy { $0 >= 32 && $0 < 127 }
    return printable ? "'\(String(bytes: bytes, encoding: .ascii) ?? "?")'" : "\(code)"
}

/// Default target. Teams emits audio from helper processes whose bundle IDs all
/// contain this substring.
let TARGET_APP_DEFAULT = "teams"

// MARK: - Process discovery

struct AudioProcess {
    let objectID: AudioObjectID
    let pid: pid_t
    let bundleID: String
}

func discoverAudioProcesses() -> [AudioProcess] {
    let objectIDs = readArrayProperty(
        AudioObjectID(kAudioObjectSystemObject),
        kAudioHardwarePropertyProcessObjectList,
        of: AudioObjectID.self)

    return objectIDs.map { id in
        AudioProcess(
            objectID: id,
            pid: readProperty(id, kAudioProcessPropertyPID, default: pid_t(-1)) ?? -1,
            bundleID: readStringProperty(id, kAudioProcessPropertyBundleID) ?? "")
    }
}

/// Resolves the executable name for a PID, so helper processes with an empty
/// bundle ID can still be identified and matched.
func processName(for pid: pid_t) -> String {
    guard pid > 0 else { return "" }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/ps")
    process.arguments = ["-p", "\(pid)", "-o", "comm="]
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    guard (try? process.run()) != nil else { return "" }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
}

// MARK: - Commands

func commandList() {
    let processes = discoverAudioProcesses()
    log("found \(processes.count) audio process objects")
    for process in processes {
        let name = process.bundleID.isEmpty ? processName(for: process.pid) : process.bundleID
        let label = name.isEmpty ? "<unknown>" : name
        print("objectID=\(process.objectID)\tpid=\(process.pid)\t\(label)")
    }
}

/// A live process tap wrapped in an aggregate device, ready for an IOProc.
/// Shared by the probe, raw-stream and transcribe modes so the TCC-sensitive
/// setup exists in exactly one place.
struct SystemTap {
    let tapID: AudioObjectID
    let aggregateID: AudioObjectID
    let format: AudioStreamBasicDescription
    let layout: [Int]
    let sourceNames: [String]
    let micUID: String?
}

func destroySystemTap(_ tap: SystemTap) {
    AudioHardwareDestroyAggregateDevice(tap.aggregateID)
    AudioHardwareDestroyProcessTap(tap.tapID)
}

/// Builds the tap and aggregate. Returns nil after logging the reason on
/// failure. `match` is a bundle-ID/executable substring; `global` taps
/// everything (diagnostic only).
func createSystemTap(
    match: String, global: Bool, makePrivate: Bool, includeMic: Bool, name: String
) -> SystemTap? {
    let description: CATapDescription

    if global {
        log("GLOBAL tap (all system audio)")
        description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    } else {
        let all = discoverAudioProcesses()
        // `match` may be a comma-separated list so one binary serves Teams, Webex,
        // FaceTime and others without a rebuild per app.
        let needles = match.lowercased().split(separator: ",").map {
            $0.trimmingCharacters(in: .whitespaces)
        }.filter { !$0.isEmpty }
        let targets = all.filter {
            // Exclude Core Audio driver-service hosts: they carry the vendor
            // name (e.g. MSTeamsAudioDevice.driver) but carry no app audio.
            guard !$0.bundleID.contains("Core-Audio-Driver-Service") else { return false }
            let bundle = $0.bundleID.lowercased()
            let name = processName(for: $0.pid).lowercased()
            return needles.contains { bundle.contains($0) || name.contains($0) }
        }
        guard !targets.isEmpty else {
            log("no audio processes matched '\(match)'. Is the app running and producing audio?")
            log("run with --list to see candidates")
            return nil
        }
        for target in targets {
            log(
                "target pid=\(target.pid) bundleID=\(target.bundleID.isEmpty ? "-" : target.bundleID)"
            )
        }
        description = CATapDescription(stereoMixdownOfProcesses: targets.map { $0.objectID })
    }

    description.name = name
    description.isPrivate = makePrivate
    description.muteBehavior = .unmuted  // never alter what the user hears

    var tapID = AudioObjectID(kAudioObjectUnknown)
    let createStatus = AudioHardwareCreateProcessTap(description, &tapID)
    guard createStatus == noErr, tapID != kAudioObjectUnknown else {
        log("AudioHardwareCreateProcessTap failed: \(fourCC(createStatus))")
        log("this usually means missing Audio Recording permission (TCC)")
        return nil
    }
    log("created tap objectID=\(tapID)")

    guard let tapUID = readStringProperty(tapID, kAudioTapPropertyUID) else {
        log("could not read tap UID")
        AudioHardwareDestroyProcessTap(tapID)
        return nil
    }

    guard
        let format = readProperty(
            tapID, kAudioTapPropertyFormat, default: AudioStreamBasicDescription())
    else {
        log("could not read tap format")
        AudioHardwareDestroyProcessTap(tapID)
        return nil
    }
    log(
        "tap format: \(format.mSampleRate)Hz channels=\(format.mChannelsPerFrame) "
            + "bits=\(format.mBitsPerChannel) flags=\(format.mFormatFlags)")

    guard let outputUID = defaultOutputDeviceUID() else {
        log("could not resolve default output device UID")
        AudioHardwareDestroyProcessTap(tapID)
        return nil
    }

    var subDevices: [[String: Any]] = [[kAudioSubDeviceUIDKey: outputUID]]
    var micUID: String?
    if includeMic {
        // Adding the mic as an aggregate sub-device does NOT itself trigger a
        // TCC request — Core Audio just hands back zeroed buffers, so the
        // permission must be requested explicitly first.
        if requestMicrophoneAccess(), let uid = defaultInputDeviceUID(), uid != outputUID {
            micUID = uid
            subDevices.append([
                kAudioSubDeviceUIDKey: uid, kAudioSubDeviceDriftCompensationKey: true,
            ])
            log("microphone sub-device: \(uid)")
        } else {
            log("WARNING: continuing WITHOUT mic — system audio only")
        }
    }

    let aggregateDescription: [String: Any] = [
        kAudioAggregateDeviceNameKey: "Clueless Aggregate",
        kAudioAggregateDeviceUIDKey: UUID().uuidString,
        kAudioAggregateDeviceMainSubDeviceKey: outputUID,
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceIsStackedKey: false,
        kAudioAggregateDeviceTapAutoStartKey: true,
        kAudioAggregateDeviceSubDeviceListKey: subDevices,
        kAudioAggregateDeviceTapListKey: [
            [kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: tapUID]
        ],
    ]

    var aggregateID = AudioObjectID(kAudioObjectUnknown)
    let aggStatus = AudioHardwareCreateAggregateDevice(
        aggregateDescription as CFDictionary, &aggregateID)
    guard aggStatus == noErr, aggregateID != kAudioObjectUnknown else {
        log("AudioHardwareCreateAggregateDevice failed: \(fourCC(aggStatus))")
        AudioHardwareDestroyProcessTap(tapID)
        return nil
    }
    log("created aggregate device objectID=\(aggregateID)")

    // The aggregate exposes one input buffer per source with input channels:
    // the output device contributes none, so the layout is [mic?, tap].
    let layout = inputBufferLayout(aggregateID)
    log("aggregate input buffer layout (channels per buffer): \(layout)")

    let micBufferIndex: Int? = (micUID != nil && layout.count > 1) ? 0 : nil
    let systemBufferIndex: Int = micBufferIndex == nil ? 0 : 1
    var sourceNames = [String](repeating: "unknown", count: max(layout.count, 1))
    if let micIndex = micBufferIndex, micIndex < sourceNames.count {
        sourceNames[micIndex] = "mic"
    }
    if systemBufferIndex < sourceNames.count { sourceNames[systemBufferIndex] = "system" }
    log(
        "source mapping: "
            + sourceNames.enumerated().map { "\($0.offset)=\($0.element)" }
            .joined(separator: " "))

    return SystemTap(
        tapID: tapID, aggregateID: aggregateID, format: format, layout: layout,
        sourceNames: sourceNames, micUID: micUID)
}

func commandTap(
    match: String, probeSeconds: Double?, global: Bool, makePrivate: Bool, includeMic: Bool
) -> Int32 {
    guard
        let tap = createSystemTap(
            match: match, global: global, makePrivate: makePrivate, includeMic: includeMic,
            name: "CluelessTap")
    else { return 2 }
    defer { destroySystemTap(tap) }

    let isProbe = probeSeconds != nil

    // In capture mode stdout carries a single JSON header line describing the
    // layout, then length-prefixed frames. Python reads the header to know how
    // to split "me" from "them".
    if !isProbe {
        let header: [String: Any] = [
            "sampleRate": tap.format.mSampleRate,
            "bytesPerSample": MemoryLayout<Float>.size,
            "buffers": tap.layout.enumerated().map {
                ["index": $0.offset, "channels": $0.element, "source": tap.sourceNames[$0.offset]]
            },
        ]
        if let data = try? JSONSerialization.data(withJSONObject: header),
            let line = String(data: data, encoding: .utf8)
        {
            print(line)
            fflush(stdout)
        }
    }

    // Per-source statistics, used by --probe to verify each stream independently.
    var peaks = [Float](repeating: 0, count: max(tap.layout.count, 1))
    var sumSquares = [Double](repeating: 0, count: max(tap.layout.count, 1))
    var counts = [Int](repeating: 0, count: max(tap.layout.count, 1))

    var procID: AudioDeviceIOProcID?
    let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, tap.aggregateID, nil) {
        _, inInputData, _, _, _ in
        let bufferList = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: inInputData))

        for (index, buffer) in bufferList.enumerated() {
            guard let raw = buffer.mData, buffer.mDataByteSize > 0 else { continue }

            if isProbe {
                guard index < counts.count else { continue }
                let count = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
                let samples = raw.bindMemory(to: Float.self, capacity: count)
                for position in 0..<count {
                    let value = abs(samples[position])
                    if value > peaks[index] { peaks[index] = value }
                    sumSquares[index] += Double(value) * Double(value)
                }
                counts[index] += count
            } else {
                // Frame: [uint8 bufferIndex][uint32 LE byteCount][payload].
                // write(2) avoids allocating on the realtime audio thread,
                // which Data(bytes:count:) would do on every callback.
                let byteCount = UInt32(buffer.mDataByteSize)
                var frameHeader = [UInt8](repeating: 0, count: 5)
                frameHeader[0] = UInt8(truncatingIfNeeded: index)
                frameHeader[1] = UInt8(truncatingIfNeeded: byteCount)
                frameHeader[2] = UInt8(truncatingIfNeeded: byteCount >> 8)
                frameHeader[3] = UInt8(truncatingIfNeeded: byteCount >> 16)
                frameHeader[4] = UInt8(truncatingIfNeeded: byteCount >> 24)
                _ = frameHeader.withUnsafeBytes { write(1, $0.baseAddress, 5) }
                _ = write(1, raw, Int(byteCount))
            }
        }
    }
    guard ioStatus == noErr, let procID else {
        log("AudioDeviceCreateIOProcIDWithBlock failed: \(fourCC(ioStatus))")
        return 4
    }
    defer { AudioDeviceDestroyIOProcID(tap.aggregateID, procID) }

    let startStatus = AudioDeviceStart(tap.aggregateID, procID)
    guard startStatus == noErr else {
        log("AudioDeviceStart failed: \(fourCC(startStatus))")
        return 5
    }
    defer { AudioDeviceStop(tap.aggregateID, procID) }

    if let seconds = probeSeconds {
        log("probing for \(seconds)s — play audio in the target app now")
        Thread.sleep(forTimeInterval: seconds)

        var anySamples = false
        var anyAudio = false
        for index in 0..<counts.count where counts[index] > 0 {
            anySamples = true
            let rms = (sumSquares[index] / Double(counts[index])).squareRoot()
            let name = index < tap.sourceNames.count ? tap.sourceNames[index] : "buffer\(index)"
            log("PROBE [\(name)] samples=\(counts[index]) peak=\(peaks[index]) rms=\(rms)")
            if peaks[index] > 0 { anyAudio = true }
        }

        if !anySamples {
            log("RESULT: FAIL — tap produced no samples at all")
            return 6
        }
        if !anyAudio {
            log("RESULT: SILENT — tap is wired up but captured pure silence")
            log("(was the target app actually playing audio during the probe?)")
            return 7
        }
        log("RESULT: SUCCESS — captured real audio")
        return 0
    }

    log("streaming raw Float32 PCM to stdout — Ctrl-C to stop")
    signal(SIGINT) { _ in exit(0) }
    signal(SIGTERM) { _ in exit(0) }
    while true { Thread.sleep(forTimeInterval: 1.0) }
}

/// `kAudioHardwarePropertyDefaultOutputDevice` returns an AudioObjectID, not a
/// string, so resolve the UID in two hops.
func defaultOutputDeviceUID() -> String? {
    guard
        let deviceID = readProperty(
            AudioObjectID(kAudioObjectSystemObject),
            kAudioHardwarePropertyDefaultOutputDevice,
            default: AudioObjectID(kAudioObjectUnknown)),
        deviceID != kAudioObjectUnknown
    else { return nil }
    return readStringProperty(deviceID, kAudioDevicePropertyDeviceUID)
}

/// Blocks until the microphone authorization state is known, prompting if this
/// is the first request. Returns true only if capture is actually permitted.
func requestMicrophoneAccess() -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
        return true
    case .notDetermined:
        log("requesting microphone permission…")
        // This resolves instantly when the parent process already holds the
        // grant (the child inherits it), and is refused just as fast when it
        // does not. The run loop is pumped rather than blocked so that a real
        // prompt can be drawn in the cases where one is offered.
        var result: Bool?
        AVCaptureDevice.requestAccess(for: .audio) { result = $0 }
        let deadline = Date().addingTimeInterval(120)
        while result == nil, Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
        let granted = result ?? false
        log("microphone permission \(granted ? "granted" : "denied")")
        return granted
    case .denied:
        log("microphone permission previously denied")
        return false
    case .restricted:
        log("microphone access restricted by policy")
        return false
    @unknown default:
        return false
    }
}

func defaultInputDeviceUID() -> String? {
    guard
        let deviceID = readProperty(
            AudioObjectID(kAudioObjectSystemObject),
            kAudioHardwarePropertyDefaultInputDevice,
            default: AudioObjectID(kAudioObjectUnknown)),
        deviceID != kAudioObjectUnknown
    else { return nil }
    return readStringProperty(deviceID, kAudioDevicePropertyDeviceUID)
}

/// Channel counts of each buffer in a device's input stream configuration.
/// The aggregate device presents the microphone and the tap as separate
/// buffers, so this is what lets the two sources be told apart downstream.
func inputBufferLayout(_ device: AudioObjectID) -> [Int] {
    var address = addr(
        kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
    guard let size = propertySize(device, &address), size > 0 else { return [] }

    let raw = UnsafeMutableRawPointer.allocate(
        byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    var mutableSize = size
    guard
        AudioObjectGetPropertyData(device, &address, 0, nil, &mutableSize, raw) == noErr
    else { return [] }

    let list = UnsafeMutableAudioBufferListPointer(
        raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.map { Int($0.mNumberChannels) }
}

// MARK: - Call detection

/// Reports whether a matching app is currently producing audio.
///
/// Used to start capture when a call begins and stop it when the call ends,
/// instead of recording an empty room. CoreAudio exposes per-process
/// IsRunningOutput, which is true while an app is playing audio — the honest
/// signal for "in a call", and it needs no special permission beyond the
/// process list already being read.
func commandDetect(match: String) -> Int32 {
    let needles = match.lowercased().split(separator: ",").map {
        $0.trimmingCharacters(in: .whitespaces)
    }.filter { !$0.isEmpty }
    var active = false
    var names: [String] = []
    var matched = 0

    for process in discoverAudioProcesses() {
        guard !process.bundleID.contains("Core-Audio-Driver-Service") else { continue }
        let bundle = process.bundleID.lowercased()
        let name = processName(for: process.pid).lowercased()
        guard needles.contains(where: { bundle.contains($0) || name.contains($0) }) else { continue }
        matched += 1

        if readProperty(process.objectID, kAudioProcessPropertyIsRunningOutput, default: UInt32(0)) == 1 {
            active = true
            names.append(process.bundleID.isEmpty ? "\(process.pid)" : process.bundleID)
        }
    }

    // Machine-readable line for the caller; diagnostics to stderr.
    print("active=\(active ? 1 : 0) matched=\(matched) processes=\(names.joined(separator: ","))")
    return active ? 0 : 1
}

// MARK: - Transcribe mode

/// Streams finalized transcript turns as JSON lines, one object per line.
///
/// Two sources are supported and they are deliberately separate processes:
///
///   --source system : process tap. Must be started through LaunchServices
///                     (`open -n`) so it runs under its own bundle identity and
///                     uses that identity's AudioCapture grant.
///   --source mic    : plain input capture. Must be spawned as a child of the
///                     app that holds the Microphone grant (Electron), since it
///                     inherits permissions from its parent.
///
/// Neither path can obtain both permissions, which is why the app runs one
/// instance of each.
func commandTranscribe(
    source: String, socketPath: String?, match: String, global: Bool, localeID: String
) async -> Int32 {
    let sink: JsonLineSink
    if let socketPath {
        guard let socketSink = JsonLineSink(socketPath: socketPath) else { return 8 }
        sink = socketSink
    } else {
        sink = JsonLineSink(stdoutOnly: ())
    }

    let locale = Locale(identifier: localeID)

    switch source {
    case "system":
        guard
            let tap = createSystemTap(
                match: match, global: global, makePrivate: true, includeMic: false,
                name: "CluelessTranscribe")
        else { return 2 }

        // The tap delivers interleaved packed float32.
        guard
            let sourceFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: tap.format.mSampleRate,
                channels: tap.format.mChannelsPerFrame,
                interleaved: true)
        else {
            log("could not build AVAudioFormat from tap format")
            destroySystemTap(tap)
            return 2
        }

        guard
            let engine = await TranscriptionEngine(
                source: "them", sourceFormat: sourceFormat, locale: locale, sink: sink)
        else {
            destroySystemTap(tap)
            return 9
        }

        var procID: AudioDeviceIOProcID?
        let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, tap.aggregateID, nil) {
            _, inInputData, _, _, _ in
            // Realtime thread: queue only, never convert or analyze here.
            let list = UnsafeMutableAudioBufferListPointer(
                UnsafeMutablePointer(mutating: inInputData))
            engine.feed(bufferList: list)
        }
        guard ioStatus == noErr, let procID else {
            log("AudioDeviceCreateIOProcIDWithBlock failed: \(fourCC(ioStatus))")
            destroySystemTap(tap)
            return 4
        }

        guard AudioDeviceStart(tap.aggregateID, procID) == noErr else {
            log("AudioDeviceStart failed")
            AudioDeviceDestroyIOProcID(tap.aggregateID, procID)
            destroySystemTap(tap)
            return 5
        }

        sink.send(["type": "ready", "source": "them", "locale": locale.identifier])
        log("transcribing system audio — Ctrl-C to stop")

        signal(SIGINT) { _ in exit(0) }
        signal(SIGTERM) { _ in exit(0) }
        while true { Thread.sleep(forTimeInterval: 1.0) }

    case "mic":
        guard requestMicrophoneAccess() else {
            log("microphone permission unavailable")
            sink.send(["type": "error", "message": "microphone permission denied"])
            return 10
        }

        let audioEngine = AVAudioEngine()
        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            log("microphone reported a zero sample rate")
            return 10
        }

        guard
            let engine = await TranscriptionEngine(
                source: "me", sourceFormat: format, locale: locale, sink: sink)
        else { return 9 }

        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            engine.feed(pcmBuffer: buffer)
        }
        do {
            try audioEngine.start()
        } catch {
            log("could not start microphone capture: \(error)")
            return 10
        }

        sink.send(["type": "ready", "source": "me", "locale": locale.identifier])
        log("transcribing microphone — Ctrl-C to stop")

        signal(SIGINT) { _ in exit(0) }
        signal(SIGTERM) { _ in exit(0) }
        while true { Thread.sleep(forTimeInterval: 1.0) }

    default:
        log("unknown --source '\(source)' (expected 'system' or 'mic')")
        return 64
    }
}

// MARK: - Entry point

let args = CommandLine.arguments

func optionValue(_ flag: String) -> String? {
    guard let index = args.firstIndex(of: flag), index + 1 < args.count else { return nil }
    return args[index + 1]
}

// When launched via LaunchServices (`open -a`), stderr is discarded, so allow
// diagnostics to be mirrored to a file instead.
if let logPath = optionValue("--log") {
    _ = FileManager.default.createFile(atPath: logPath, contents: nil)
    logFile = FileHandle(forWritingAtPath: logPath)
}

if args.contains("--list") {
    commandList()
    exit(0)
}

let isGlobal = args.contains("--global")
let bundleMatch = optionValue("--bundle")

// Detect mode: is the target app currently producing audio?
if args.contains("--detect") {
    exit(commandDetect(match: bundleMatch ?? TARGET_APP_DEFAULT))
}

// Transcribe mode streams JSON turns instead of raw audio.
if args.contains("--transcribe") {
    let source = optionValue("--source") ?? "system"
    let code = await commandTranscribe(
        source: source,
        socketPath: optionValue("--socket"),
        match: bundleMatch ?? TARGET_APP_DEFAULT,
        global: isGlobal,
        localeID: optionValue("--locale") ?? "en-US")
    exit(code)
}

guard isGlobal || bundleMatch != nil else {
    log(
        "usage: audiotap --list"
            + " | audiotap --bundle <substring> [--probe <sec>] [--mic] [--public] [--log <path>]"
            + " | audiotap --global [--probe <sec>] [--mic]"
            + " | audiotap --transcribe --source system|mic [--socket <path>] [--locale en-US]")
    exit(64)
}

let probe = optionValue("--probe").flatMap(Double.init)
exit(
    commandTap(
        match: bundleMatch ?? "", probeSeconds: probe, global: isGlobal,
        makePrivate: !args.contains("--public"), includeMic: args.contains("--mic")))

