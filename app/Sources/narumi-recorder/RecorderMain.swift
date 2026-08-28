import Darwin
import Foundation
import NarumiRecorderKit

/// Why the `record` loop ended.
enum StopReason: Sendable {
    case signal(Int32)
    case stdinStop
    case stdinClosed
    case failure(RecorderError)
}

@main
struct RecorderMain {
    static func main() async {
        let status = await RecorderCLI.run(Array(CommandLine.arguments.dropFirst()))
        exit(status)
    }
}

enum RecorderCLI {
    static func run(_ arguments: [String]) async -> Int32 {
        let sink = StdoutEventSink()
        let command: RecorderCommand
        do {
            command = try ArgumentParser.parse(arguments)
        } catch {
            sink.emit(.error(ErrorEvent(RecorderError.wrap(error, fallback: .invalidArgument))))
            fputs(ArgumentParser.usage + "\n", stderr)
            return 2
        }

        switch command {
        case .help:
            print(ArgumentParser.usage)
            return 0
        case .check:
            print(Permissions.check().serialized())
            return 0
        case .listDisplays:
            do {
                let displays = try await CaptureEngine.listDisplays()
                print(DisplayInfo.jsonArray(displays))
                return 0
            } catch {
                sink.emit(.error(ErrorEvent(RecorderError.wrap(error))))
                return 1
            }
        case .record(let options):
            return await record(options, sink: sink)
        case .requestPermission, .openPermissionSettings:
            do {
                print(try await PermissionSetup.run(command).serialized())
                return 0
            } catch {
                sink.emit(.error(ErrorEvent(RecorderError.wrap(error))))
                return 1
            }
        }
    }

    private static func record(_ options: RecordOptions, sink: StdoutEventSink) async -> Int32 {
        let (reasons, continuation) = AsyncStream<StopReason>.makeStream()
        let signals = SignalWatcher(signals: [SIGINT, SIGTERM]) { signal in
            continuation.yield(.signal(signal))
        }
        let stdin = StdinWatcher { reason in
            continuation.yield(reason)
        }
        signals.activate()

        let session = RecorderSession(sink: sink)
        await session.setFailureHandler { error in
            continuation.yield(.failure(error))
        }
        let outputDir = URL(fileURLWithPath: options.outputDir, isDirectory: true)
        do {
            try await session.start(outputDir: outputDir, options: options)
        } catch {
            sink.emit(.error(ErrorEvent(RecorderError.wrap(error))))
            return 1
        }
        stdin.start()

        var failure: RecorderError?
        for await reason in reasons {
            switch reason {
            case .signal(let signal):
                sink.emit(.log(LogEvent(message: "received signal \(signal); stopping")))
            case .stdinStop:
                sink.emit(.log(LogEvent(message: "received stop on stdin; stopping")))
            case .stdinClosed:
                sink.emit(.log(LogEvent(message: "stdin pipe closed by parent; stopping")))
            case .failure(let error):
                failure = error
            }
            break
        }

        let outcome = await session.stop(failure: failure)
        return outcome.error == nil ? 0 : 1
    }
}

/// Replaces the default SIGINT/SIGTERM handlers with dispatch sources.
final class SignalWatcher: Sendable {
    private let sources: [DispatchSourceSignal]

    init(signals: [Int32], handler: @escaping @Sendable (Int32) -> Void) {
        let queue = DispatchQueue(label: "jp.btajp.narumi.recorder.signals")
        sources = signals.map { number in
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: queue)
            source.setEventHandler {
                handler(number)
            }
            return source
        }
    }

    func activate() {
        for source in sources {
            source.activate()
        }
    }
}

/// Reads stdin on a background thread. A line `stop` ends the recording. EOF on a *pipe* also
/// ends it (the parent went away); EOF on /dev/null or a TTY is ignored.
final class StdinWatcher: Sendable {
    private let handler: @Sendable (StopReason) -> Void

    init(handler: @escaping @Sendable (StopReason) -> Void) {
        self.handler = handler
    }

    func start() {
        let handler = self.handler
        let thread = Thread {
            while let line = readLine(strippingNewline: true) {
                if line.trimmingCharacters(in: .whitespacesAndNewlines) == "stop" {
                    handler(.stdinStop)
                    return
                }
            }
            if StdinWatcher.stdinIsPipe() {
                handler(.stdinClosed)
            }
        }
        thread.name = "narumi-recorder.stdin"
        thread.start()
    }

    static func stdinIsPipe() -> Bool {
        var info = stat()
        guard fstat(STDIN_FILENO, &info) == 0 else {
            return false
        }
        return (info.st_mode & S_IFMT) == S_IFIFO
    }
}
