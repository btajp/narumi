import Foundation

extension ServerCapabilities {
    /// ASR selection has no compatibility fallback: both contract 5 and explicit support are required.
    public func supportedTranscriptionModelProviders(contractVersion: String?) -> [String] {
        guard let contractVersion, RecordingPermissionContract.supportsSetup(contractVersion),
            contractVersion.split(separator: ".").first == "5",
            transports.contains("streamable-http"), workflow?.stageModelSelection == true else { return [] }
        let advertised = Set(transcriptionModelProviders ?? [])
        return TranscriptionModelSelection.providers.filter { advertised.contains($0) }
    }
}
