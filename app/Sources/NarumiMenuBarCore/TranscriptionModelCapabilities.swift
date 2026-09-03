import Foundation

extension ServerCapabilities {
    /// ASR selection has no compatibility fallback: contract 5/6 and explicit support are required.
    public func supportedTranscriptionModelProviders(contractVersion: String?) -> [String] {
        guard let contractVersion, RecordingPermissionContract.supportsTranscriptionModelSelection(contractVersion),
            transports.contains("streamable-http"), workflow?.stageModelSelection == true else { return [] }
        let advertised = Set(transcriptionModelProviders ?? [])
        return TranscriptionModelSelection.providers.filter { advertised.contains($0) }
    }
}
