import Foundation

extension MeetingConfig {
    public init(from decoder: Decoder) throws {
        try requireContractKeys(
            decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)), required: [])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            transcriptionEngine: try c.decodeIfPresent(String.self, forKey: .transcriptionEngine),
            diarizationEngine: try c.decodeIfPresent(String.self, forKey: .diarizationEngine),
            llmProvider: try c.decodeIfPresent(String.self, forKey: .llmProvider),
            externalSendPolicy: try c.decodeIfPresent(String.self, forKey: .externalSendPolicy),
            language: try c.decodeIfPresent(String.self, forKey: .language),
            selfName: try c.decodeIfPresent(String.self, forKey: .selfName),
            vocabHints: try c.decodeIfPresent([String].self, forKey: .vocabHints),
            minutesModel: try c.decodeIfPresent(MinutesModelSelection.self, forKey: .minutesModel),
            minutesEnsemble: try c.decodeIfPresent(MinutesEnsembleSelection.self, forKey: .minutesEnsemble),
            transcriptionModel: try c.decodeIfPresent(TranscriptionModelSelection.self, forKey: .transcriptionModel))
        guard minutesModel == nil || minutesEnsemble == nil else {
            throw DecodingError.dataCorruptedError(
                forKey: .minutesEnsemble, in: c,
                debugDescription: "minutes_model and minutes_ensemble are mutually exclusive")
        }
    }

    public func encode(to encoder: Encoder) throws {
        guard minutesModel == nil || minutesEnsemble == nil else {
            throw EncodingError.invalidValue(self, .init(
                codingPath: encoder.codingPath,
                debugDescription: "minutes_model and minutes_ensemble are mutually exclusive"))
        }
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(transcriptionEngine, forKey: .transcriptionEngine)
        try c.encodeIfPresent(diarizationEngine, forKey: .diarizationEngine)
        try c.encodeIfPresent(llmProvider, forKey: .llmProvider)
        try c.encodeIfPresent(minutesModel, forKey: .minutesModel)
        try c.encodeIfPresent(minutesEnsemble, forKey: .minutesEnsemble)
        try c.encodeIfPresent(transcriptionModel, forKey: .transcriptionModel)
        try c.encodeIfPresent(externalSendPolicy, forKey: .externalSendPolicy)
        try c.encodeIfPresent(language, forKey: .language)
        try c.encodeIfPresent(selfName, forKey: .selfName)
        try c.encodeIfPresent(vocabHints, forKey: .vocabHints)
    }
}

extension Minutes {
    public init(from decoder: Decoder) throws {
        try requireContractKeys(
            decoder, allowed: Set(CodingKeys.allCases.map(\.rawValue)),
            required: ["meeting_id", "version", "markdown", "generated_at", "provider",
                       "unresolved_speakers", "available_versions"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        meetingID = try c.decode(String.self, forKey: .meetingID)
        version = try c.decode(Int.self, forKey: .version)
        markdown = try c.decode(String.self, forKey: .markdown)
        generatedAt = try c.decode(String.self, forKey: .generatedAt)
        provider = try c.decode(String.self, forKey: .provider)
        unresolvedSpeakers = try c.decode([String].self, forKey: .unresolvedSpeakers)
        availableVersions = try c.decode([Int].self, forKey: .availableVersions)
        provenance = try c.decodeIfPresent(PublishedMinutesEnsembleProvenance.self, forKey: .provenance)
        provenanceWasPresent = c.contains(.provenance)
        guard provenance?.isWellFormed ?? true else {
            throw DecodingError.dataCorruptedError(
                forKey: .provenance, in: c, debugDescription: "Invalid minutes ensemble provenance")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(meetingID, forKey: .meetingID); try c.encode(version, forKey: .version)
        try c.encode(markdown, forKey: .markdown); try c.encode(generatedAt, forKey: .generatedAt)
        try c.encode(provider, forKey: .provider); try c.encode(unresolvedSpeakers, forKey: .unresolvedSpeakers)
        try c.encode(availableVersions, forKey: .availableVersions)
        if provenanceWasPresent { try c.encode(provenance, forKey: .provenance) }
    }

    public func validatesProvenance(contractVersion: String?) -> Bool {
        contractVersion?.split(separator: ".").first != "6" || provenanceWasPresent
    }
}
