import AppKit
import CoreMedia
import Foundation
import ScreenCaptureKit

/// What the engine ended up capturing.
public struct CaptureStartInfo: Sendable, Equatable {
    public let display: DisplayInfo
    public let videoDimensions: VideoDimensions?
    public let pointPixelScale: Double
}

/// ScreenCaptureKit stream wrapper: one display, system audio and microphone as three outputs.
public actor CaptureEngine {
    static let framesPerSecond = 10
    static let maxVideoWidth = 1920

    private let queue: DispatchSerialQueue
    private var stream: SCStream?
    private var bridge: StreamBridge?

    init(queue: DispatchSerialQueue) {
        self.queue = queue
    }

    // MARK: Shareable content

    static func fetchShareableContent() async throws -> SCShareableContent {
        do {
            return try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        } catch {
            throw SCErrorMapper.map(error)
        }
    }

    static func displayInfos(_ displays: [SCDisplay]) async -> [DisplayInfo] {
        var infos: [DisplayInfo] = []
        for display in displays {
            let id = display.displayID
            let name = await displayName(for: id)
            infos.append(DisplayInfo(id: id, width: display.width, height: display.height, name: name))
        }
        return infos
    }

    @MainActor
    private static func displayName(for id: CGDirectDisplayID) -> String {
        let key = NSDeviceDescriptionKey("NSScreenNumber")
        for screen in NSScreen.screens {
            if let number = screen.deviceDescription[key] as? NSNumber, number.uint32Value == id {
                return screen.localizedName
            }
        }
        return CGDisplayIsBuiltin(id) != 0 ? "Built-in Display \(id)" : "Display \(id)"
    }

    /// Displays available to `list-displays`.
    public static func listDisplays() async throws -> [DisplayInfo] {
        let content = try await fetchShareableContent()
        let infos = await displayInfos(content.displays)
        if infos.isEmpty, !CGPreflightScreenCaptureAccess() {
            throw RecorderError(.permissionDenied, "screen recording permission not granted")
        }
        return infos
    }

    // MARK: Lifecycle

    /// Resolve the display, configure the stream and start capturing. `makeConsumer` receives the
    /// final video dimensions (nil with `--no-video`) and must return the writer set to feed.
    func start(
        options: RecordOptions,
        onStopped: @escaping @Sendable (RecorderError) -> Void,
        makeConsumer: @Sendable (VideoDimensions?) -> TrackWriterSet
    ) async throws -> (CaptureStartInfo, TrackWriterSet) {
        guard stream == nil else {
            throw RecorderError(.captureFailed, "capture already running")
        }
        let content = try await CaptureEngine.fetchShareableContent()
        let infos = await CaptureEngine.displayInfos(content.displays)
        if infos.isEmpty, !CGPreflightScreenCaptureAccess() {
            throw RecorderError(.permissionDenied, "screen recording permission not granted")
        }
        let selected = try DisplaySelection.select(from: infos, requestedID: options.displayID)
        guard let scDisplay = content.displays.first(where: { $0.displayID == selected.id }) else {
            throw RecorderError(.noDisplay, "display \(selected.id) disappeared")
        }

        let filter = SCContentFilter(display: scDisplay, excludingWindows: [])
        let scale = Double(filter.pointPixelScale)
        let dimensions: VideoDimensions? = options.includeVideo
            ? VideoDimensions.fit(
                width: Int((Double(selected.width) * scale).rounded()),
                height: Int((Double(selected.height) * scale).rounded()),
                maxWidth: CaptureEngine.maxVideoWidth)
            : nil

        let configuration = SCStreamConfiguration()
        if let dimensions {
            configuration.width = dimensions.width
            configuration.height = dimensions.height
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(CaptureEngine.framesPerSecond))
        } else {
            // No screen output is attached; keep the renderer as cheap as possible.
            configuration.width = 64
            configuration.height = 64
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        }
        configuration.pixelFormat = kCVPixelFormatType_32BGRA
        configuration.showsCursor = false
        configuration.queueDepth = 5
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2
        configuration.captureMicrophone = true
        if let uid = options.microphoneDeviceUID {
            configuration.microphoneCaptureDeviceID = uid
        }

        let consumer = makeConsumer(dimensions)
        let bridge = StreamBridge(consumer: consumer) { error in
            onStopped(SCErrorMapper.map(error))
        }
        let stream = SCStream(filter: filter, configuration: configuration, delegate: bridge)
        do {
            if options.includeVideo {
                try stream.addStreamOutput(bridge, type: .screen, sampleHandlerQueue: queue)
            }
            try stream.addStreamOutput(bridge, type: .audio, sampleHandlerQueue: queue)
            try stream.addStreamOutput(bridge, type: .microphone, sampleHandlerQueue: queue)
        } catch {
            throw RecorderError(.captureFailed, "cannot attach stream outputs: \(RecorderError.describe(error))")
        }
        do {
            try await stream.startCapture()
        } catch {
            throw SCErrorMapper.map(error)
        }
        self.stream = stream
        self.bridge = bridge
        let info = CaptureStartInfo(display: selected, videoDimensions: dimensions, pointPixelScale: scale)
        return (info, consumer)
    }

    /// Stop the stream. Returns the stream clock time at stop (used as the common session end) and
    /// a non-fatal stop error, if any (e.g. the stream had already been stopped by the system).
    func stop() async -> (endTime: CMTime?, stopError: RecorderError?) {
        guard let stream else {
            return (nil, nil)
        }
        let endTime = stream.synchronizationClock.map { CMClockGetTime($0) }
        var stopError: RecorderError?
        do {
            try await stream.stopCapture()
        } catch {
            stopError = SCErrorMapper.map(error)
        }
        self.stream = nil
        self.bridge = nil
        return (endTime, stopError)
    }
}

// MARK: - Delegate bridge

/// Receives SCStream callbacks on the sample handler queue and forwards them.
final class StreamBridge: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let consumer: TrackWriterSet
    private let onStopped: @Sendable (any Error) -> Void

    init(consumer: TrackWriterSet, onStopped: @escaping @Sendable (any Error) -> Void) {
        self.consumer = consumer
        self.onStopped = onStopped
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        consumer.consume(sampleBuffer, type: type)
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        onStopped(error)
    }
}

// MARK: - Error mapping

enum SCErrorMapper {
    /// Map ScreenCaptureKit / TCC failures to recorder error codes.
    static func map(_ error: any Error) -> RecorderError {
        if let recorderError = error as? RecorderError {
            return recorderError
        }
        let nsError = error as NSError
        guard nsError.domain == SCStreamErrorDomain, let code = SCStreamError.Code(rawValue: nsError.code) else {
            return RecorderError(.captureFailed, RecorderError.describe(error))
        }
        let detail = RecorderError.describe(error)
        switch code {
        case .userDeclined, .missingEntitlements:
            return RecorderError(.permissionDenied, "screen recording not permitted: \(detail)")
        case .noDisplayList, .noCaptureSource:
            return RecorderError(.noDisplay, detail)
        case .failedToStartMicrophoneCapture:
            if Permissions.microphoneStatus() != .granted {
                return RecorderError(.permissionDenied, "microphone not permitted: \(detail)")
            }
            return RecorderError(.captureFailed, detail)
        default:
            return RecorderError(.captureFailed, detail)
        }
    }
}
