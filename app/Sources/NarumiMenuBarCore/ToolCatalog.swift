import Foundation

/// The MCP tool names narumi.app calls, in one place instead of scattered string literals.
///
/// Surface parity (AGENTS.md 絶対原則 3, `docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`):
/// the app is a plain MCP client, so "app ⊆ contract" must hold. `ToolCatalogTests` checks each
/// name in `allUsed` against `contracts/manifest.json`. Add every new tool the app starts calling
/// here (and to `allUsed`) — never call `callTool` with a bare literal.
public enum ToolCatalog {
    // Recording (menu bar + window banner)
    public static let startRecording = "start_recording"
    public static let stopRecording = "stop_recording"
    public static let getRecordingStatus = "get_recording_status"
    public static let configureRecordingPermission = "configure_recording_permission"

    // Meetings list / search
    public static let listMeetings = "list_meetings"
    public static let searchTranscripts = "search_transcripts"

    // Meeting detail
    public static let getMeeting = "get_meeting"
    public static let getMinutes = "get_minutes"
    public static let getTranscript = "get_transcript"
    public static let registerContext = "register_context"
    public static let regenerate = "regenerate"
    public static let setMeetingConfig = "set_meeting_config"
    public static let exportMinutes = "export_minutes"
    public static let listExportDestinations = "list_export_destinations"

    // Jobs
    public static let getJobStatus = "get_job_status"
    public static let cancelJob = "cancel_job"

    // Destructive
    public static let discardTracks = "discard_tracks"
    public static let deleteMeeting = "delete_meeting"

    // Import
    public static let importRecording = "import_recording"

    // Profiles
    public static let listProfiles = "list_profiles"
    public static let getProfile = "get_profile"
    public static let setProfile = "set_profile"
    public static let deleteProfile = "delete_profile"

    // Optional Gaia connection settings
    public static let getGaiaConnection = "get_gaia_connection"
    public static let setGaiaConnection = "set_gaia_connection"
    public static let testGaiaConnection = "test_gaia_connection"

    // Provider connections and model metadata (generation selections are separate).
    public static let listProviders = "list_providers"
    public static let listProviderConnections = "list_provider_connections"
    public static let setProviderConnection = "set_provider_connection"
    public static let deleteProviderConnection = "delete_provider_connection"
    public static let prepareProviderRuntime = "prepare_provider_runtime"
    public static let authenticateProviderConnection = "authenticate_provider_connection"
    public static let getProviderAuthStatus = "get_provider_auth_status"
    public static let testProviderConnection = "test_provider_connection"
    public static let listProviderModels = "list_provider_models"
    public static let verifyProviderModel = "verify_provider_model"

    // Diagnostics
    public static let getServerInfo = "get_server_info"
    public static let rebuildCatalog = "rebuild_catalog"

    /// Every tool name the app currently calls; the parity test walks this list.
    public static let allUsed: [String] = [
        startRecording,
        stopRecording,
        getRecordingStatus,
        configureRecordingPermission,
        listMeetings,
        searchTranscripts,
        getMeeting,
        getMinutes,
        getTranscript,
        registerContext,
        regenerate,
        setMeetingConfig,
        exportMinutes,
        listExportDestinations,
        getJobStatus,
        cancelJob,
        discardTracks,
        deleteMeeting,
        importRecording,
        listProfiles,
        getProfile,
        setProfile,
        deleteProfile,
        getGaiaConnection,
        setGaiaConnection,
        testGaiaConnection,
        listProviders,
        listProviderConnections,
        setProviderConnection,
        deleteProviderConnection,
        prepareProviderRuntime,
        authenticateProviderConnection,
        getProviderAuthStatus,
        testProviderConnection,
        listProviderModels,
        verifyProviderModel,
        getServerInfo,
        rebuildCatalog,
    ]
}
