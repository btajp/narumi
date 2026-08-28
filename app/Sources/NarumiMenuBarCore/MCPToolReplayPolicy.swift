/// Only known local reads may be replayed after a lost MCP session. Mutations and provider
/// metadata refreshes reconcile their original operation instead of automatically resending.
public enum MCPToolReplayPolicy {
    public static func allowsSessionRetry(tool: String, refreshingPermissions: Bool = false) -> Bool {
        if tool == ToolCatalog.getServerInfo { return !refreshingPermissions }
        let reads: Set<String> = [
            ToolCatalog.getRecordingStatus, ToolCatalog.listMeetings, ToolCatalog.searchTranscripts,
            ToolCatalog.getMeeting, ToolCatalog.getMinutes, ToolCatalog.getTranscript,
            ToolCatalog.listExportDestinations, ToolCatalog.getJobStatus, ToolCatalog.listProfiles,
            ToolCatalog.getProfile, ToolCatalog.getGaiaConnection,
            ToolCatalog.listProviders, ToolCatalog.listProviderConnections, ToolCatalog.getProviderAuthStatus,
        ]
        return reads.contains(tool)
    }

    public static func createsJob(
        tool: String, autoProcess: Bool? = nil, autoRegenerate: Bool? = nil
    ) -> Bool {
        switch tool {
        case ToolCatalog.regenerate, ToolCatalog.exportMinutes, ToolCatalog.prepareProviderRuntime:
            return true
        case ToolCatalog.importRecording, ToolCatalog.stopRecording:
            return autoProcess != false
        case ToolCatalog.registerContext:
            return autoRegenerate == true
        default:
            return false
        }
    }
}
