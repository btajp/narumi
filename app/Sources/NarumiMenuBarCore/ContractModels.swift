import Foundation

/// Decodable views of the tool results the main window consumes.
///
/// Shapes mirror `contracts/tools/*.json` outputSchema (source of truth). Every key is spelled
/// explicitly (no `convertFromSnakeCase`: dictionary keys such as `SPEAKER_00` must survive
/// verbatim). Timestamps stay ISO 8601 strings; `NarumiFormat` renders them.

// MARK: - Meetings

/// `defs/common.json#/$defs/meeting_summary`.
public struct MeetingSummary: Codable, Equatable, Sendable, Identifiable {
    public var meetingID: String
    public var meetingName: String
    public var engagement: String?
    public var scope: String?
    public var status: String
    public var startedAt: String
    public var stoppedAt: String?
    public var latestMinutesVersion: Int?
    public var activeJob: ActiveJob?

    public var id: String { meetingID }

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case meetingName = "meeting_name"
        case engagement
        case scope
        case status
        case startedAt = "started_at"
        case stoppedAt = "stopped_at"
        case latestMinutesVersion = "latest_minutes_version"
        case activeJob = "active_job"
    }

    public init(
        meetingID: String, meetingName: String, engagement: String? = nil, scope: String? = nil,
        status: String, startedAt: String, stoppedAt: String? = nil,
        latestMinutesVersion: Int? = nil, activeJob: ActiveJob? = nil
    ) {
        self.meetingID = meetingID
        self.meetingName = meetingName
        self.engagement = engagement
        self.scope = scope
        self.status = status
        self.startedAt = startedAt
        self.stoppedAt = stoppedAt
        self.latestMinutesVersion = latestMinutesVersion
        self.activeJob = activeJob
    }
}

/// `meeting_summary.active_job`.
public struct ActiveJob: Codable, Equatable, Sendable {
    public var jobID: String
    public var kind: String
    public var status: String
    public var progress: JobProgress?

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case kind
        case status
        case progress
    }

    public init(jobID: String, kind: String, status: String, progress: JobProgress? = nil) {
        self.jobID = jobID
        self.kind = kind
        self.status = status
        self.progress = progress
    }
}

public struct JobProgress: Codable, Equatable, Sendable {
    public var stage: String?
    public var fraction: Double?

    public init(stage: String? = nil, fraction: Double? = nil) {
        self.stage = stage
        self.fraction = fraction
    }
}

public struct ListMeetingsResponse: Codable, Equatable, Sendable {
    public var meetings: [MeetingSummary]
}

// MARK: - Search

public struct SearchHit: Codable, Equatable, Sendable, Identifiable {
    public var meetingID: String
    public var meetingName: String
    public var sourceID: String
    public var segmentID: String
    public var start: Double
    public var end: Double
    public var speaker: String?
    public var text: String

    public var id: String { "\(meetingID)/\(sourceID)/\(segmentID)" }

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case meetingName = "meeting_name"
        case sourceID = "source_id"
        case segmentID = "segment_id"
        case start
        case end
        case speaker
        case text
    }
}

public struct SearchTranscriptsResponse: Codable, Equatable, Sendable {
    public var hits: [SearchHit]
}

// MARK: - Recording

public struct RecordingStatus: Codable, Equatable, Sendable {
    public var active: Bool
    public var meetingID: String?
    public var meetingName: String?
    public var startedAt: String?
    public var elapsedSec: Double?
    public var tracks: [String: String]?

    enum CodingKeys: String, CodingKey {
        case active
        case meetingID = "meeting_id"
        case meetingName = "meeting_name"
        case startedAt = "started_at"
        case elapsedSec = "elapsed_sec"
        case tracks
    }

    public init(
        active: Bool, meetingID: String? = nil, meetingName: String? = nil,
        startedAt: String? = nil, elapsedSec: Double? = nil, tracks: [String: String]? = nil
    ) {
        self.active = active
        self.meetingID = meetingID
        self.meetingName = meetingName
        self.startedAt = startedAt
        self.elapsedSec = elapsedSec
        self.tracks = tracks
    }
}

// MARK: - Meeting detail

/// `defs/common.json#/$defs/meeting_config`.
public struct MeetingConfig: Codable, Equatable, Sendable {
    public var transcriptionEngine: String?
    public var diarizationEngine: String?
    public var llmProvider: String?
    public var minutesModel: CodexMinutesSelection?
    public var externalSendPolicy: String?
    public var language: String?
    public var selfName: String?
    public var vocabHints: [String]?

    enum CodingKeys: String, CodingKey {
        case transcriptionEngine = "transcription_engine"
        case diarizationEngine = "diarization_engine"
        case llmProvider = "llm_provider"
        case minutesModel = "minutes_model"
        case externalSendPolicy = "external_send_policy"
        case language
        case selfName = "self_name"
        case vocabHints = "vocab_hints"
    }

    public init(
        transcriptionEngine: String? = nil, diarizationEngine: String? = nil,
        llmProvider: String? = nil, externalSendPolicy: String? = nil, language: String? = nil,
        selfName: String? = nil, vocabHints: [String]? = nil,
        minutesModel: CodexMinutesSelection? = nil
    ) {
        self.transcriptionEngine = transcriptionEngine
        self.diarizationEngine = diarizationEngine
        self.llmProvider = llmProvider
        self.minutesModel = minutesModel
        self.externalSendPolicy = externalSendPolicy
        self.language = language
        self.selfName = selfName
        self.vocabHints = vocabHints
    }
}

/// `defs/common.json#/$defs/track_status`.
public struct TrackStatus: Codable, Equatable, Sendable {
    public var path: String
    public var sha256: String?
    public var bytes: Int?
    public var durationSec: Double?
    public var discarded: Bool

    enum CodingKeys: String, CodingKey {
        case path
        case sha256
        case bytes
        case durationSec = "duration_sec"
        case discarded
    }
}

public struct MeetingRecordingInfo: Codable, Equatable, Sendable {
    public var startedAt: String?
    public var stoppedAt: String?
    public var durationSec: Double?
    public var tracks: [String: TrackStatus]

    enum CodingKeys: String, CodingKey {
        case startedAt = "started_at"
        case stoppedAt = "stopped_at"
        case durationSec = "duration_sec"
        case tracks
    }
}

public struct ContextEntry: Codable, Equatable, Sendable, Identifiable {
    public var contextID: String
    public var sourceType: String
    public var status: String
    public var registeredAt: String
    public var label: String?

    public var id: String { contextID }

    enum CodingKeys: String, CodingKey {
        case contextID = "context_id"
        case sourceType = "source_type"
        case status
        case registeredAt = "registered_at"
        case label
    }
}

public struct MinutesVersionInfo: Codable, Equatable, Sendable, Identifiable {
    public var version: Int
    public var generatedAt: String
    public var provider: String
    public var path: String

    public var id: Int { version }

    enum CodingKeys: String, CodingKey {
        case version
        case generatedAt = "generated_at"
        case provider
        case path
    }
}

public struct LatestMinutes: Codable, Equatable, Sendable {
    public var version: Int
    public var markdown: String
}

public struct ExportRecord: Codable, Equatable, Sendable {
    public var destination: String
    public var ref: String
    public var minutesVersion: Int
    public var at: String

    enum CodingKeys: String, CodingKey {
        case destination
        case ref
        case minutesVersion = "minutes_version"
        case at
    }
}

/// `get_meeting` output.
public struct MeetingDetail: Codable, Equatable, Sendable {
    public var meeting: MeetingSummary
    public var bundlePath: String
    public var config: MeetingConfig
    public var recording: MeetingRecordingInfo
    public var contexts: [ContextEntry]
    public var minutesVersions: [MinutesVersionInfo]
    public var latestMinutes: LatestMinutes?
    public var exports: [ExportRecord]
    public var artifacts: [String]

    enum CodingKeys: String, CodingKey {
        case meeting
        case bundlePath = "bundle_path"
        case config
        case recording
        case contexts
        case minutesVersions = "minutes_versions"
        case latestMinutes = "latest_minutes"
        case exports
        case artifacts
    }
}

// MARK: - Minutes / transcript

/// `get_minutes` output.
public struct Minutes: Codable, Equatable, Sendable {
    public var meetingID: String
    public var version: Int
    public var markdown: String
    public var generatedAt: String
    public var provider: String
    public var unresolvedSpeakers: [String]
    public var availableVersions: [Int]

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case version
        case markdown
        case generatedAt = "generated_at"
        case provider
        case unresolvedSpeakers = "unresolved_speakers"
        case availableVersions = "available_versions"
    }
}

/// `defs/common.json#/$defs/segment`.
public struct TranscriptSegment: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var start: Double
    public var end: Double
    public var text: String
    public var speaker: String?
    public var speakerName: String?
    public var confidence: Double?

    enum CodingKeys: String, CodingKey {
        case id
        case start
        case end
        case text
        case speaker
        case speakerName = "speaker_name"
        case confidence
    }
}

/// `speaker_map` value: `name` is `string | null` (null = unresolved).
public struct SpeakerIdentity: Codable, Equatable, Sendable {
    public var name: String?
    public var confidence: Double
}

/// `get_transcript` output.
public struct Transcript: Codable, Equatable, Sendable {
    public var meetingID: String
    public var source: String
    public var segments: [TranscriptSegment]
    public var speakerMap: [String: SpeakerIdentity]
    public var availableSources: [String]

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case source
        case segments
        case speakerMap = "speaker_map"
        case availableSources = "available_sources"
    }
}

// MARK: - Jobs

/// `defs/common.json#/$defs/error`.
public struct ToolErrorInfo: Codable, Equatable, Sendable {
    public var code: String
    public var message: String
}

/// Known keys of a succeeded job's `result` (free-form object in the contract).
public struct JobResultSummary: Codable, Equatable, Sendable {
    public var meetingID: String?
    public var minutesVersion: Int?
    public var destination: String?
    public var ref: String?

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case minutesVersion = "minutes_version"
        case destination
        case ref
    }
}

/// `defs/common.json#/$defs/job`.
public struct Job: Codable, Equatable, Sendable, Identifiable {
    public var jobID: String
    public var meetingID: String?
    public var kind: String
    public var status: String
    public var progress: JobProgress?
    public var result: JobResultSummary?
    public var error: ToolErrorInfo?
    public var createdAt: String
    public var updatedAt: String

    public var id: String { jobID }
    public var isActive: Bool { status == "queued" || status == "running" }

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case meetingID = "meeting_id"
        case kind
        case status
        case progress
        case result
        case error
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

public struct JobStatusResponse: Codable, Equatable, Sendable {
    public var job: Job
}

public struct RegenerateResponse: Codable, Equatable, Sendable {
    public var jobID: String
    public var meetingID: String

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case meetingID = "meeting_id"
    }
}

// MARK: - Context / config / export

public struct RegisterContextResponse: Codable, Equatable, Sendable {
    public var contextID: String
    public var status: String
    public var jobID: String?

    enum CodingKeys: String, CodingKey {
        case contextID = "context_id"
        case status
        case jobID = "job_id"
    }
}

public struct SetMeetingConfigResponse: Codable, Equatable, Sendable {
    public var meetingID: String
    public var config: MeetingConfig
    public var scope: String?

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case config
        case scope
    }
}

public struct ExportResult: Codable, Equatable, Sendable {
    public var destination: String
    public var ref: String
    public var minutesVersion: Int
    public var at: String

    enum CodingKeys: String, CodingKey {
        case destination
        case ref
        case minutesVersion = "minutes_version"
        case at
    }
}

/// `export_minutes` output: exactly one of `result` / `jobID`.
public struct ExportMinutesResponse: Codable, Equatable, Sendable {
    public var result: ExportResult?
    public var jobID: String?

    enum CodingKeys: String, CodingKey {
        case result
        case jobID = "job_id"
    }
}

public struct ExportDestinationInfo: Codable, Equatable, Sendable, Identifiable {
    public var name: String
    public var description: String
    // options_schema is deliberately not decoded: the window only needs the names, and the
    // file exporters' options (output_path / overwrite) are part of the export_minutes contract.

    public var id: String { name }
}

public struct ListExportDestinationsResponse: Codable, Equatable, Sendable {
    public var destinations: [ExportDestinationInfo]
}

// MARK: - Destructive

public struct DiscardTracksResponse: Codable, Equatable, Sendable {
    public var meetingID: String
    public var tracks: [String: TrackStatus]

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case tracks
    }
}

public struct DeleteMeetingResponse: Codable, Equatable, Sendable {
    public var meetingID: String
    public var deleted: Bool
    public var movedTo: String

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case deleted
        case movedTo = "moved_to"
    }
}

// MARK: - Import

public struct ImportRecordingResponse: Codable, Equatable, Sendable {
    public var meetingID: String
    public var bundlePath: String
    public var tracks: [String: TrackStatus]
    public var jobID: String?

    enum CodingKeys: String, CodingKey {
        case meetingID = "meeting_id"
        case bundlePath = "bundle_path"
        case tracks
        case jobID = "job_id"
    }
}

// MARK: - Profiles

/// `defs/common.json#/$defs/profile`.
public struct Profile: Codable, Equatable, Sendable, Identifiable {
    public var name: String
    public var config: MeetingConfig
    public var scope: String?
    public var engagement: String?
    public var exportDestinations: [String]
    public var isDefault: Bool

    public var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name
        case config
        case scope
        case engagement
        case exportDestinations = "export_destinations"
        case isDefault = "is_default"
    }
}

public struct ListProfilesResponse: Codable, Equatable, Sendable {
    public var profiles: [Profile]
    public var defaultName: String

    enum CodingKeys: String, CodingKey {
        case profiles
        case defaultName = "default"
    }
}

public struct ProfileResponse: Codable, Equatable, Sendable {
    public var profile: Profile
}

public struct DeleteProfileResponse: Codable, Equatable, Sendable {
    public var name: String
    public var deleted: Bool
}

// MARK: - Server info / diagnostics

public struct BinaryInfo: Codable, Equatable, Sendable {
    public var path: String
    public var version: String
}

public struct RecorderPermissions: Codable, Equatable, Sendable {
    public var screenRecording: String
    public var microphone: String

    enum CodingKeys: String, CodingKey {
        case screenRecording = "screen_recording"
        case microphone
    }

    public init(screenRecording: String, microphone: String) {
        self.screenRecording = screenRecording
        self.microphone = microphone
    }
}

public struct ServerCapabilities: Codable, Equatable, Sendable {
    public var recording: Bool
    public var permissions: RecorderPermissions?
    public var transports: [String]
    public var transcriptionEngines: [String]
    public var diarizationEngines: [String]
    public var llmProviders: [String]
    public var exportDestinations: [String]
    public var permissionSetupInProgress: Bool
    public var workflow: ProviderWorkflowCapabilities?

    enum CodingKeys: String, CodingKey {
        case recording
        case permissions
        case transports
        case transcriptionEngines = "transcription_engines"
        case diarizationEngines = "diarization_engines"
        case llmProviders = "llm_providers"
        case exportDestinations = "export_destinations"
        case permissionSetupInProgress = "permission_setup_in_progress"
        case workflow
    }

    public init(
        recording: Bool, permissions: RecorderPermissions? = nil, transports: [String],
        transcriptionEngines: [String], diarizationEngines: [String], llmProviders: [String],
        exportDestinations: [String], permissionSetupInProgress: Bool = false,
        workflow: ProviderWorkflowCapabilities? = nil
    ) {
        self.recording = recording
        self.permissions = permissions
        self.transports = transports
        self.transcriptionEngines = transcriptionEngines
        self.diarizationEngines = diarizationEngines
        self.llmProviders = llmProviders
        self.exportDestinations = exportDestinations
        self.permissionSetupInProgress = permissionSetupInProgress
        self.workflow = workflow
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        recording = try container.decode(Bool.self, forKey: .recording)
        permissions = try container.decodeIfPresent(RecorderPermissions.self, forKey: .permissions)
        transports = try container.decode([String].self, forKey: .transports)
        transcriptionEngines = try container.decode([String].self, forKey: .transcriptionEngines)
        diarizationEngines = try container.decode([String].self, forKey: .diarizationEngines)
        llmProviders = try container.decode([String].self, forKey: .llmProviders)
        exportDestinations = try container.decode([String].self, forKey: .exportDestinations)
        workflow = try container.decodeIfPresent(ProviderWorkflowCapabilities.self, forKey: .workflow)
        if container.contains(.permissionSetupInProgress) {
            permissionSetupInProgress = try container.decode(Bool.self, forKey: .permissionSetupInProgress)
        } else {
            permissionSetupInProgress = false
        }
    }
}

public struct ServerDiagnostics: Codable, Equatable, Sendable {
    public var ffmpeg: BinaryInfo?
    public var ffprobe: BinaryInfo?
    public var dataRoot: String
    public var meetingsRoot: String
    public var catalogPath: String
    public var recorderPath: String?
    public var contractsDir: String

    enum CodingKeys: String, CodingKey {
        case ffmpeg
        case ffprobe
        case dataRoot = "data_root"
        case meetingsRoot = "meetings_root"
        case catalogPath = "catalog_path"
        case recorderPath = "recorder_path"
        case contractsDir = "contracts_dir"
    }
}

/// `get_server_info` output.
public struct ServerInfo: Codable, Equatable, Sendable {
    public var name: String
    public var serverVersion: String
    public var serverInstanceID: String?
    public var contractVersion: String
    public var capabilities: ServerCapabilities
    public var diagnostics: ServerDiagnostics
    public var secureTransport: SecureTransportMetadata?

    enum CodingKeys: String, CodingKey {
        case name
        case serverVersion = "server_version"
        case serverInstanceID = "server_instance_id"
        case contractVersion = "contract_version"
        case capabilities
        case diagnostics
        case secureTransport = "secure_transport"
    }

    public init(
        name: String, serverVersion: String, contractVersion: String,
        capabilities: ServerCapabilities, diagnostics: ServerDiagnostics, serverInstanceID: String? = nil,
        secureTransport: SecureTransportMetadata? = nil
    ) {
        self.name = name
        self.serverVersion = serverVersion
        self.serverInstanceID = serverInstanceID
        self.contractVersion = contractVersion
        self.capabilities = capabilities
        self.diagnostics = diagnostics
        self.secureTransport = secureTransport
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        serverVersion = try container.decode(String.self, forKey: .serverVersion)
        contractVersion = try container.decode(String.self, forKey: .contractVersion)
        capabilities = try container.decode(ServerCapabilities.self, forKey: .capabilities)
        diagnostics = try container.decode(ServerDiagnostics.self, forKey: .diagnostics)
        secureTransport = try container.decodeIfPresent(SecureTransportMetadata.self, forKey: .secureTransport)
        if container.contains(.serverInstanceID) {
            let instanceID = try container.decode(String.self, forKey: .serverInstanceID)
            guard RecordingPermissionContract.isValidServerInstanceID(instanceID) else {
                throw DecodingError.dataCorruptedError(
                    forKey: .serverInstanceID, in: container,
                    debugDescription: "server_instance_id must be a lowercase UUIDv4")
            }
            serverInstanceID = instanceID
        } else {
            serverInstanceID = nil
        }
    }
}

public struct RebuildCatalogResponse: Codable, Equatable, Sendable {
    public var meetings: Int
    public var segments: Int
    public var errors: [String]
}
