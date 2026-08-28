/// All v2 capabilities belong to the authenticated MCP session that reported them.
/// A replaced session permits only an empty server-info probe until compatibility is known.
public struct MCPPermissionSessionState: Equatable, Sendable {
    public private(set) var generation: UInt64 = 0
    public private(set) var contractVersion: String?
    public private(set) var serverInstanceID: String?

    public init() {}

    public mutating func reset() {
        generation &+= 1
        contractVersion = nil
        serverInstanceID = nil
    }

    @discardableResult
    public mutating func observeServerInfo(
        contractVersion: String?, serverInstanceID: String? = nil, requestGeneration: UInt64
    ) -> Bool {
        guard requestGeneration == generation else { return false }
        let observedInstanceID = RecordingPermissionContract.isValidServerInstanceID(serverInstanceID)
            ? serverInstanceID : nil
        if let knownInstanceID = self.serverInstanceID, knownInstanceID != observedInstanceID {
            generation &+= 1
        }
        self.contractVersion = contractVersion
        self.serverInstanceID = observedInstanceID
        return true
    }

    public func allowsCall(tool: String, refreshingPermissions: Bool = false) -> Bool {
        if tool == ToolCatalog.getServerInfo && !refreshingPermissions { return true }
        return RecordingPermissionContract.supportsSetup(contractVersion, serverInstanceID: serverInstanceID)
    }
}
