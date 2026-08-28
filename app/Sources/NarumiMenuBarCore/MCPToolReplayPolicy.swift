/// Shared admission rules for automatic MCP retries. Permission setup can open an OS
/// prompt even when its response is lost, so only a new explicit user action may resend it.
public enum MCPToolReplayPolicy {
    public static func allowsSessionRetry(tool: String, refreshingPermissions: Bool = false) -> Bool {
        guard tool != ToolCatalog.configureRecordingPermission else { return false }
        // A replacement session may use an older contract that rejects the refresh input.
        // Rediscover its version with an empty probe instead of replaying that input.
        return tool != ToolCatalog.getServerInfo || !refreshingPermissions
    }

    public static func createsJob(
        tool: String, autoProcess: Bool? = nil, autoRegenerate: Bool? = nil
    ) -> Bool {
        switch tool {
        case ToolCatalog.regenerate, ToolCatalog.exportMinutes:
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
