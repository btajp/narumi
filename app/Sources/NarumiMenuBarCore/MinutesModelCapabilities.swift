import Foundation

extension ServerCapabilities {
    /// A missing field is a Codex-only compatibility case only for contract 3.
    public func supportedMinutesModelProviders(contractVersion: String?) -> [String] {
        guard let contractVersion, RecordingPermissionContract.supportsSetup(contractVersion),
            workflow?.stageModelSelection == true else { return [] }
        switch contractVersion.split(separator: ".").first {
        case "3": return ["codex-app-server"]
        case "4":
            let advertised = Set(minutesModelProviders ?? [])
            return MinutesModelSelection.providers.filter { advertised.contains($0) }
        default: return []
        }
    }
}
