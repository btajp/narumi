import Foundation

/// Options of `narumi-recorder record`.
public struct RecordOptions: Equatable, Sendable {
    public var outputDir: String
    public var displayID: UInt32?
    public var includeVideo: Bool
    public var microphoneDeviceUID: String?

    public init(
        outputDir: String,
        displayID: UInt32? = nil,
        includeVideo: Bool = true,
        microphoneDeviceUID: String? = nil
    ) {
        self.outputDir = outputDir
        self.displayID = displayID
        self.includeVideo = includeVideo
        self.microphoneDeviceUID = microphoneDeviceUID
    }
}

public enum RecorderCommand: Equatable, Sendable {
    case record(RecordOptions)
    case check
    case listDisplays
    case help
}

/// Hand-rolled argument parser (no third-party dependency).
public enum ArgumentParser {
    public static let usage = """
        usage: narumi-recorder <command> [options]

        commands:
          record --output <dir> [--display <id>] [--no-video] [--mic <device-uid>]
                 Record until SIGINT/SIGTERM or a "stop" line on stdin. Events are printed
                 on stdout as JSON Lines (started / stopped / error / log).
          check          Print TCC permission status as JSON.
          list-displays  Print available displays as a JSON array.
          help           Show this message.
        """

    public static func parse(_ arguments: [String]) throws -> RecorderCommand {
        guard let command = arguments.first else {
            throw RecorderError(.invalidArgument, "missing command (record | check | list-displays)")
        }
        let rest = Array(arguments.dropFirst())
        switch command {
        case "record":
            return .record(try parseRecord(rest))
        case "check":
            try rejectExtraArguments(rest, command: command)
            return .check
        case "list-displays":
            try rejectExtraArguments(rest, command: command)
            return .listDisplays
        case "help", "--help", "-h":
            return .help
        default:
            throw RecorderError(.invalidArgument, "unknown command: \(command)")
        }
    }

    private static func rejectExtraArguments(_ rest: [String], command: String) throws {
        if let extra = rest.first {
            throw RecorderError(.invalidArgument, "\(command) takes no arguments (got \(extra))")
        }
    }

    private static func parseRecord(_ arguments: [String]) throws -> RecordOptions {
        var outputDir: String?
        var displayID: UInt32?
        var includeVideo = true
        var microphoneDeviceUID: String?

        var index = 0
        func takeValue(for flag: String) throws -> String {
            index += 1
            guard index < arguments.count else {
                throw RecorderError(.invalidArgument, "\(flag) requires a value")
            }
            return arguments[index]
        }

        while index < arguments.count {
            let argument = arguments[index]
            let (flag, inlineValue) = splitInline(argument)
            switch flag {
            case "--output", "-o":
                outputDir = try inlineValue ?? takeValue(for: flag)
            case "--display":
                let raw = try inlineValue ?? takeValue(for: flag)
                guard let parsed = UInt32(raw) else {
                    throw RecorderError(.invalidArgument, "--display expects a numeric display id (got \(raw))")
                }
                displayID = parsed
            case "--no-video":
                if inlineValue != nil {
                    throw RecorderError(.invalidArgument, "--no-video takes no value")
                }
                includeVideo = false
            case "--mic":
                microphoneDeviceUID = try inlineValue ?? takeValue(for: flag)
            default:
                throw RecorderError(.invalidArgument, "unknown option for record: \(argument)")
            }
            index += 1
        }

        guard let outputDir, !outputDir.isEmpty else {
            throw RecorderError(.invalidArgument, "record requires --output <dir>")
        }
        return RecordOptions(
            outputDir: outputDir,
            displayID: displayID,
            includeVideo: includeVideo,
            microphoneDeviceUID: microphoneDeviceUID
        )
    }

    /// `--flag=value` → (`--flag`, `value`); anything else → (argument, nil).
    private static func splitInline(_ argument: String) -> (String, String?) {
        guard argument.hasPrefix("--"), let equals = argument.firstIndex(of: "=") else {
            return (argument, nil)
        }
        return (String(argument[..<equals]), String(argument[argument.index(after: equals)...]))
    }
}
