import Foundation

/// The MCP tool names narumi.app calls, in one place instead of scattered string literals.
///
/// Surface parity (AGENTS.md 絶対原則 3, `docs/superpowers/specs/2026-08-27-narumi-surface-parity-design.md`):
/// the app is a plain MCP client, so "app ⊆ contract" must hold. `ToolCatalogTests` checks each
/// name in `allUsed` against `contracts/manifest.json`. Add every new tool the app starts calling
/// here (and to `allUsed`) — never call `callTool` with a bare literal.
public enum ToolCatalog {
    public static let startRecording = "start_recording"
    public static let stopRecording = "stop_recording"
    public static let getServerInfo = "get_server_info"

    /// Every tool name the app currently calls; the parity test walks this list.
    public static let allUsed: [String] = [
        startRecording,
        stopRecording,
        getServerInfo,
    ]
}
