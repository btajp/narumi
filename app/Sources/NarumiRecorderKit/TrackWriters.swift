import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

// MARK: - One AVAssetWriter per track

/// Writes one track to its own file. The writer is created lazily on the first accepted sample so
/// the session can start at the shared origin and the input gets a real `sourceFormatHint`.
///
/// Not thread-safe by itself: ``TrackWriterSet`` owns instances and serializes all access on the
/// ScreenCaptureKit sample handler queue (its actor executor).
final class TrackWriter {
    let kind: TrackKind
    let url: URL
    private let fileType: AVFileType
    private let mediaType: AVMediaType
    private let outputSettings: [String: Any]

    private var assetWriter: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private(set) var appendedSamples = 0
    private(set) var droppedSamples = 0
    private(set) var firstPTS: CMTime?
    private(set) var lastPTS: CMTime?

    init(kind: TrackKind, url: URL, fileType: AVFileType, mediaType: AVMediaType, outputSettings: [String: Any]) {
        self.kind = kind
        self.url = url
        self.fileType = fileType
        self.mediaType = mediaType
        self.outputSettings = outputSettings
    }

    /// H.264 in an MP4 container, from `.screen` sample buffers.
    static func screen(url: URL, dimensions: VideoDimensions, framesPerSecond: Int) -> TrackWriter {
        let compression: [String: Any] = [
            AVVideoAverageBitRateKey: 3_000_000,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            AVVideoAllowFrameReorderingKey: false,
            AVVideoExpectedSourceFrameRateKey: framesPerSecond,
            AVVideoMaxKeyFrameIntervalDurationKey: 5.0,
        ]
        return TrackWriter(
            kind: .screen,
            url: url,
            fileType: .mp4,
            mediaType: .video,
            outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: dimensions.width,
                AVVideoHeightKey: dimensions.height,
                AVVideoCompressionPropertiesKey: compression,
            ]
        )
    }

    /// AAC 48 kHz stereo 128 kbps, from `.audio` (system audio) sample buffers.
    static func systemAudio(url: URL) -> TrackWriter {
        TrackWriter(
            kind: .system,
            url: url,
            fileType: .m4a,
            mediaType: .audio,
            outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 2,
                AVEncoderBitRateKey: 128_000,
            ]
        )
    }

    /// AAC 48 kHz mono 96 kbps, from `.microphone` sample buffers (device native format in).
    static func microphone(url: URL) -> TrackWriter {
        TrackWriter(
            kind: .mic,
            url: url,
            fileType: .m4a,
            mediaType: .audio,
            outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 96_000,
            ]
        )
    }

    var fileName: String { TrackFileNames.fileName(for: kind) }

    /// Append one sample. `sessionStart` is the origin shared by every track.
    func append(_ sampleBuffer: CMSampleBuffer, sessionStart: CMTime) throws {
        guard CMSampleBufferDataIsReady(sampleBuffer) else {
            droppedSamples += 1
            return
        }
        if assetWriter == nil {
            try startWriting(
                formatHint: CMSampleBufferGetFormatDescription(sampleBuffer),
                sessionStart: sessionStart
            )
        }
        guard let assetWriter, let input else {
            return
        }
        if assetWriter.status == .failed {
            throw failure(assetWriter.error)
        }
        guard input.isReadyForMoreMediaData else {
            droppedSamples += 1
            return
        }
        guard input.append(sampleBuffer) else {
            throw failure(assetWriter.error)
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        if firstPTS == nil {
            firstPTS = pts
        }
        lastPTS = pts
        appendedSamples += 1
    }

    private func startWriting(formatHint: CMFormatDescription?, sessionStart: CMTime) throws {
        let fileManager = FileManager.default
        if fileManager.fileExists(atPath: url.path) {
            do {
                try fileManager.removeItem(at: url)
            } catch {
                throw RecorderError(.writerFailed, "\(fileName): cannot replace existing file: \(RecorderError.describe(error))")
            }
        }
        let writer: AVAssetWriter
        do {
            writer = try AVAssetWriter(outputURL: url, fileType: fileType)
        } catch {
            throw RecorderError(.writerFailed, "\(fileName): \(RecorderError.describe(error))")
        }
        let writerInput = AVAssetWriterInput(
            mediaType: mediaType, outputSettings: outputSettings, sourceFormatHint: formatHint)
        writerInput.expectsMediaDataInRealTime = true
        guard writer.canAdd(writerInput) else {
            throw RecorderError(.writerFailed, "\(fileName): output settings rejected by AVAssetWriter")
        }
        writer.add(writerInput)
        guard writer.startWriting() else {
            throw failure(writer.error)
        }
        writer.startSession(atSourceTime: sessionStart)
        assetWriter = writer
        input = writerInput
    }

    /// Synchronous half of finalization: close the input and end the session.
    /// Returns nil when no sample was ever accepted (nothing to finish).
    func prepareToFinish(endTime: CMTime?) -> AVAssetWriter? {
        guard let assetWriter, let input else {
            return nil
        }
        input.markAsFinished()
        if let endTime, endTime.isValid, let lastPTS, endTime > lastPTS {
            assetWriter.endSession(atSourceTime: endTime)
        }
        return assetWriter
    }

    /// Abandon the file after a failure so no writer stays open.
    func cancel() {
        guard let assetWriter, assetWriter.status == .writing else {
            return
        }
        assetWriter.cancelWriting()
    }

    func failure(_ error: (any Error)?) -> RecorderError {
        let detail = error.map(RecorderError.describe) ?? "unknown AVAssetWriter failure"
        return RecorderError(.writerFailed, "\(fileName): \(detail)")
    }

    static func fileSize(of url: URL) throws -> Int {
        do {
            let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
            return (attributes[.size] as? NSNumber)?.intValue ?? 0
        } catch {
            throw RecorderError(.writerFailed, "\(url.lastPathComponent): cannot stat: \(RecorderError.describe(error))")
        }
    }

    static func duration(of url: URL) async throws -> Double {
        let asset = AVURLAsset(url: url)
        do {
            let duration = try await asset.load(.duration)
            return duration.isNumeric ? CMTimeGetSeconds(duration) : 0
        } catch {
            throw RecorderError(.writerFailed, "\(url.lastPathComponent): cannot read duration: \(RecorderError.describe(error))")
        }
    }
}

// MARK: - Frame filtering

enum VideoFrameFilter {
    /// Accept only frames that carry an image and whose ScreenCaptureKit status says it is content.
    static func isRenderable(_ sampleBuffer: CMSampleBuffer) -> Bool {
        guard CMSampleBufferGetImageBuffer(sampleBuffer) != nil else {
            return false
        }
        guard
            let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
            let first = attachments.first,
            let rawStatus = first[.status] as? Int,
            let status = SCFrameStatus(rawValue: rawStatus)
        else {
            return true
        }
        switch status {
        case .complete, .started, .idle:
            return true
        case .blank, .suspended, .stopped:
            return false
        @unknown default:
            return false
        }
    }
}

// MARK: - The set of writers fed by the stream

/// Owns the three writers. The ScreenCaptureKit sample handler queue doubles as this actor's
/// executor, so the stream delegate can hand buffers over synchronously via `assumeIsolated`.
actor TrackWriterSet {
    nonisolated let queue: DispatchSerialQueue
    nonisolated var unownedExecutor: UnownedSerialExecutor { queue.asUnownedSerialExecutor() }

    private let writers: [TrackKind: TrackWriter]
    private let onFailure: @Sendable (RecorderError) -> Void
    private var sessionStart: CMTime?
    /// First writer failure. Other tracks keep recording so their files stay recoverable.
    private var failure: RecorderError?
    private var failedKinds: Set<TrackKind> = []

    /// Outcome of ``finish(endTime:)``: every track that could be finalized, plus the first error.
    struct FinishResult: Sendable {
        var summaries: [TrackKind: TrackSummary]
        var error: RecorderError?
    }

    init(
        outputDir: URL,
        includeVideo: Bool,
        videoDimensions: VideoDimensions?,
        framesPerSecond: Int,
        queue: DispatchSerialQueue,
        onFailure: @escaping @Sendable (RecorderError) -> Void
    ) {
        var writers: [TrackKind: TrackWriter] = [
            .mic: .microphone(url: TrackFileNames.url(for: .mic, in: outputDir)),
            .system: .systemAudio(url: TrackFileNames.url(for: .system, in: outputDir)),
        ]
        if includeVideo, let videoDimensions {
            writers[.screen] = .screen(
                url: TrackFileNames.url(for: .screen, in: outputDir),
                dimensions: videoDimensions,
                framesPerSecond: framesPerSecond
            )
        }
        self.writers = writers
        self.queue = queue
        self.onFailure = onFailure
    }

    /// Entry point for the SCStreamOutput callback. Must be called on `queue`.
    ///
    /// `CMSampleBuffer` is not `Sendable`; the hand-over is synchronous on the very queue that is
    /// this actor's executor and the caller never touches the buffer afterwards, so the local
    /// `nonisolated(unsafe)` binding does not introduce a race.
    nonisolated func consume(_ sampleBuffer: CMSampleBuffer, type: SCStreamOutputType) {
        nonisolated(unsafe) let buffer = sampleBuffer
        assumeIsolated { set in
            set.handle(buffer, type: type)
        }
    }

    private func handle(_ sampleBuffer: CMSampleBuffer, type: SCStreamOutputType) {
        let kind: TrackKind
        switch type {
        case .screen: kind = .screen
        case .audio: kind = .system
        case .microphone: kind = .mic
        @unknown default: return
        }
        guard let writer = writers[kind], !failedKinds.contains(kind) else {
            return
        }
        if kind == .screen, !VideoFrameFilter.isRenderable(sampleBuffer) {
            return
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        guard pts.isValid, pts.isNumeric else {
            return
        }
        if sessionStart == nil {
            sessionStart = pts
        }
        guard let sessionStart else {
            return
        }
        do {
            try writer.append(sampleBuffer, sessionStart: sessionStart)
        } catch {
            let recorderError = RecorderError.wrap(error, fallback: .writerFailed)
            failedKinds.insert(kind)
            writer.cancel()
            if failure == nil {
                failure = recorderError
                onFailure(recorderError)
            }
        }
    }

    /// Finalize every writer that did not fail. Never throws so healthy tracks are always
    /// reported; `error` carries the first failure (mid-recording or during finalization).
    func finish(endTime: CMTime?) async -> FinishResult {
        var result = FinishResult(summaries: [:], error: failure)
        for kind in TrackKind.allCases {
            guard let writer = writers[kind], !failedKinds.contains(kind) else {
                continue
            }
            do {
                result.summaries[kind] = try await finish(writer, endTime: endTime)
            } catch {
                if result.error == nil {
                    result.error = RecorderError.wrap(error, fallback: .writerFailed)
                }
            }
        }
        return result
    }

    private func finish(_ writer: TrackWriter, endTime: CMTime?) async throws -> TrackSummary {
        guard let assetWriter = writer.prepareToFinish(endTime: endTime) else {
            // No sample was ever accepted (e.g. a display that never produced a frame).
            return TrackSummary(path: writer.fileName, bytes: 0, durationSec: 0)
        }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            assetWriter.finishWriting {
                continuation.resume()
            }
        }
        guard assetWriter.status == .completed else {
            throw writer.failure(assetWriter.error)
        }
        let bytes = try TrackWriter.fileSize(of: writer.url)
        let duration = try await TrackWriter.duration(of: writer.url)
        return TrackSummary(path: writer.fileName, bytes: bytes, durationSec: duration)
    }

    func statistics() -> [TrackKind: (appended: Int, dropped: Int)] {
        var result: [TrackKind: (appended: Int, dropped: Int)] = [:]
        for (kind, writer) in writers {
            result[kind] = (writer.appendedSamples, writer.droppedSamples)
        }
        return result
    }
}
