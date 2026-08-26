import CoreMedia
import Foundation

/// Result of ``RecorderSession/stop(failure:)``.
public struct StopOutcome: Sendable {
    public let summary: RecorderSummary
    /// Non-nil when the recording did not end cleanly; the `error` event has been emitted.
    public let error: RecorderError?
}

/// Drives one recording: start → (events) → stop. Emits protocol events through the sink.
public actor RecorderSession {
    private let sink: any EventSink
    private let queue = DispatchSerialQueue(label: "jp.btajp.narumi.recorder.samples")
    private var engine: CaptureEngine?
    private var writers: TrackWriterSet?
    private var outputDir: URL?
    private var startedAt: Date?
    private var failureHandler: (@Sendable (RecorderError) -> Void)?

    public init(sink: any EventSink) {
        self.sink = sink
    }

    /// Called (once) when the stream or a writer fails while recording. The caller should then
    /// invoke ``stop(failure:)`` with the error to finalize whatever was written.
    public func setFailureHandler(_ handler: @escaping @Sendable (RecorderError) -> Void) {
        failureHandler = handler
    }

    public var isRecording: Bool { engine != nil }

    /// Start capturing into `outputDir` and emit the `started` event.
    public func start(outputDir: URL, options: RecordOptions) async throws {
        guard engine == nil else {
            throw RecorderError(.captureFailed, "already recording")
        }
        do {
            try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        } catch {
            throw RecorderError(.invalidArgument, "cannot create output dir \(outputDir.path): \(RecorderError.describe(error))")
        }
        let microphone = await Permissions.requestMicrophoneAccess()
        guard microphone == .granted else {
            throw RecorderError(.permissionDenied, "microphone permission \(microphone.rawValue)")
        }

        let engine = CaptureEngine(queue: queue)
        let queue = self.queue
        let failure: @Sendable (RecorderError) -> Void = { [weak self] error in
            guard let self else { return }
            Task { await self.handleFailure(error) }
        }
        let (info, writers) = try await engine.start(options: options, onStopped: failure) { dimensions in
            TrackWriterSet(
                outputDir: outputDir,
                includeVideo: options.includeVideo,
                videoDimensions: dimensions,
                framesPerSecond: CaptureEngine.framesPerSecond,
                queue: queue,
                onFailure: failure
            )
        }
        let now = Date()
        self.engine = engine
        self.writers = writers
        self.outputDir = outputDir
        self.startedAt = now

        sink.emit(.started(StartedEvent(
            startedAt: Timestamps.iso8601(now),
            tracks: .standard(includeVideo: options.includeVideo)
        )))
        var detail = "capturing display \(info.display.id) (\(info.display.name))"
        if let dims = info.videoDimensions {
            detail += " at \(dims.width)x\(dims.height)@\(CaptureEngine.framesPerSecond)fps"
        } else {
            detail += " audio only"
        }
        sink.emit(.log(LogEvent(message: detail)))
    }

    private var reportedFailure: RecorderError?

    private func handleFailure(_ error: RecorderError) {
        guard reportedFailure == nil else {
            return
        }
        reportedFailure = error
        failureHandler?(error)
    }

    /// Stop capturing, finalize every track, write `recorder.json` and emit `stopped` whenever the
    /// audio tracks were finalized, followed by `error` when `failure` is given or finalization
    /// failed (`error` alone when no usable track exists).
    public func stop(failure: RecorderError? = nil) async -> StopOutcome {
        guard let engine, let writers, let outputDir, let startedAt else {
            let error = RecorderError(.captureFailed, "not recording")
            sink.emit(.error(ErrorEvent(error)))
            let summary = RecorderSummary(
                startedAt: "", stoppedAt: Timestamps.iso8601(Date()), durationSec: 0, tracks: nil,
                error: ErrorEvent(error))
            return StopOutcome(summary: summary, error: error)
        }
        var outcomeError = failure ?? reportedFailure

        let (endTime, stopError) = await engine.stop()
        if let stopError {
            sink.emit(.log(LogEvent(message: "stopCapture: \(stopError.message)")))
        }

        let finished = await writers.finish(endTime: endTime)
        if outcomeError == nil {
            outcomeError = finished.error
        }
        var tracks: StoppedTracks?
        if let mic = finished.summaries[.mic], let system = finished.summaries[.system] {
            tracks = StoppedTracks(screen: finished.summaries[.screen], mic: mic, system: system)
        } else if outcomeError == nil {
            outcomeError = RecorderError(.writerFailed, "audio tracks were not finalized")
        }
        for (kind, stats) in await writers.statistics().sorted(by: { $0.key.rawValue < $1.key.rawValue }) {
            sink.emit(.log(LogEvent(message: "\(kind.rawValue): \(stats.appended) samples written, \(stats.dropped) dropped")))
        }
        if let tracks, let screen = tracks.screen, screen.bytes == 0 {
            sink.emit(.log(LogEvent(message: "screen: no video frame was captured; screen.mp4 is empty")))
        }

        let stoppedAt = Date()
        let summary = RecorderSummary(
            startedAt: Timestamps.iso8601(startedAt),
            stoppedAt: Timestamps.iso8601(stoppedAt),
            durationSec: stoppedAt.timeIntervalSince(startedAt),
            tracks: tracks,
            error: outcomeError.map(ErrorEvent.init)
        )
        do {
            try RecorderSession.writeRecorderJSON(summary, in: outputDir)
        } catch {
            if summary.error == nil {
                let writeError = RecorderError.wrap(error, fallback: .writerFailed)
                let failed = RecorderSummary(
                    startedAt: summary.startedAt, stoppedAt: summary.stoppedAt,
                    durationSec: summary.durationSec, tracks: summary.tracks, error: ErrorEvent(writeError))
                sink.emit(.error(ErrorEvent(writeError)))
                clear()
                return StopOutcome(summary: failed, error: writeError)
            }
        }

        clear()
        // The audio tracks are the recording. When they were finalized, `stopped` goes out even
        // if capture failed mid-meeting (display disconnected, stream stopped by the system):
        // the server keeps the meeting usable and records the trailing `error` as provenance.
        if let event = summary.stoppedEvent {
            sink.emit(.stopped(event))
        }
        if let outcomeError {
            sink.emit(.error(ErrorEvent(outcomeError)))
            return StopOutcome(summary: summary, error: outcomeError)
        }
        return StopOutcome(summary: summary, error: nil)
    }

    private func clear() {
        engine = nil
        writers = nil
        outputDir = nil
        startedAt = nil
    }

    static func writeRecorderJSON(_ summary: RecorderSummary, in outputDir: URL) throws {
        let url = TrackFileNames.recorderJSONURL(in: outputDir)
        let tmp = url.appendingPathExtension("tmp")
        do {
            try (summary.serialized() + "\n").write(to: tmp, atomically: false, encoding: .utf8)
            if FileManager.default.fileExists(atPath: url.path) {
                _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
            } else {
                try FileManager.default.moveItem(at: tmp, to: url)
            }
        } catch {
            throw RecorderError(.writerFailed, "cannot write recorder.json: \(RecorderError.describe(error))")
        }
    }
}
