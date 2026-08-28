import Foundation

public enum ProviderModality: String, Codable, Sendable {
    case text, image, audio
}

public enum ProviderTimestampSupport: String, Codable, Sendable {
    case none, segment, word
    case diarizedSegment = "diarized_segment"
}

public enum ProviderModelSource: String, Codable, Sendable {
    case providerAPI = "provider_api"
    case runtime
    case localCatalog = "local_catalog"
}

public enum ProviderBillingKind: String, Codable, Sendable {
    case local, api, subscription, unknown
}

public enum ProviderParameterType: String, Codable, Sendable {
    case string, integer, number, boolean
}

/// Parameters are scalar values; credentials and nested runtime settings are not parameter values.
public enum ProviderScalarValue: Codable, Equatable, Sendable {
    case string(String)
    case number(Double)
    case boolean(Bool)

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Bool.self) {
            self = .boolean(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "Provider parameters must be non-null scalar values")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .boolean(let value): try container.encode(value)
        }
    }
}

public struct ProviderModelParameter: Decodable, Equatable, Sendable {
    public let type: ProviderParameterType
    public let description: String?
    public let enumValues: [ProviderScalarValue]?
    public let minimum: Double?
    public let maximum: Double?
    public let defaultValue: ProviderScalarValue?

    enum CodingKeys: String, CodingKey {
        case type, description
        case enumValues = "enum"
        case minimum, maximum
        case defaultValue = "default"
    }

    public init(
        type: ProviderParameterType, description: String? = nil,
        enumValues: [ProviderScalarValue]? = nil, minimum: Double? = nil,
        maximum: Double? = nil, defaultValue: ProviderScalarValue? = nil
    ) {
        self.type = type
        self.description = description
        self.enumValues = enumValues
        self.minimum = minimum
        self.maximum = maximum
        self.defaultValue = defaultValue
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        type = try container.decode(ProviderParameterType.self, forKey: .type)
        // These fields may be absent, but the contract does not allow explicit null.
        description =
            container.contains(.description) ? try container.decode(String.self, forKey: .description) : nil
        enumValues =
            container.contains(.enumValues)
            ? try container.decode([ProviderScalarValue].self, forKey: .enumValues) : nil
        minimum = container.contains(.minimum) ? try container.decode(Double.self, forKey: .minimum) : nil
        maximum = container.contains(.maximum) ? try container.decode(Double.self, forKey: .maximum) : nil
        defaultValue =
            container.contains(.defaultValue)
            ? try container.decode(ProviderScalarValue.self, forKey: .defaultValue) : nil
        if let enumValues, enumValues.isEmpty {
            throw DecodingError.dataCorruptedError(
                forKey: .enumValues, in: container,
                debugDescription: "Parameter enum must contain at least one value")
        }
    }
}

public struct ProviderParameterSchema: Decodable, Equatable, Sendable {
    public let type: String
    public let properties: [String: ProviderModelParameter]
    public let required: [String]
    public let additionalProperties: Bool

    enum CodingKeys: String, CodingKey {
        case type, properties, required, additionalProperties
    }

    public init(properties: [String: ProviderModelParameter] = [:], required: [String] = []) {
        type = "object"
        self.properties = properties
        self.required = required
        additionalProperties = false
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        type = try container.decode(String.self, forKey: .type)
        properties = try container.decode([String: ProviderModelParameter].self, forKey: .properties)
        required = try container.decode([String].self, forKey: .required)
        additionalProperties = try container.decode(Bool.self, forKey: .additionalProperties)
        let forbiddenNames: Set<String> = [
            "api_key", "token", "url", "endpoint", "command", "env", "environment", "path",
            "headers", "authorization",
        ]
        let validNames = properties.keys.allSatisfy { name in
            !forbiddenNames.contains(name)
                && name.range(of: #"\A[a-z][a-z0-9_]{0,63}\z"#, options: .regularExpression) != nil
        }
        guard type == "object", !additionalProperties, validNames,
            Set(required).count == required.count, required.allSatisfy({ properties[$0] != nil })
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .properties, in: container,
                debugDescription: "Provider parameter schemas must remain closed and contain supported names")
        }
    }
}

public struct ProviderModelBilling: Decodable, Equatable, Sendable {
    public let kind: ProviderBillingKind
    public let inputUSDPerMillionTokens: String?
    public let outputUSDPerMillionTokens: String?
    public let audioUSDPerMinute: String?
    public let fetchedAt: String?

    enum CodingKeys: String, CodingKey {
        case kind
        case inputUSDPerMillionTokens = "input_usd_per_million_tokens"
        case outputUSDPerMillionTokens = "output_usd_per_million_tokens"
        case audioUSDPerMinute = "audio_usd_per_minute"
        case fetchedAt = "fetched_at"
    }

    public init(
        kind: ProviderBillingKind, inputUSDPerMillionTokens: String?,
        outputUSDPerMillionTokens: String?, audioUSDPerMinute: String?, fetchedAt: String?
    ) {
        self.kind = kind
        self.inputUSDPerMillionTokens = inputUSDPerMillionTokens
        self.outputUSDPerMillionTokens = outputUSDPerMillionTokens
        self.audioUSDPerMinute = audioUSDPerMinute
        self.fetchedAt = fetchedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        kind = try container.decode(ProviderBillingKind.self, forKey: .kind)
        inputUSDPerMillionTokens = try container.decode(String?.self, forKey: .inputUSDPerMillionTokens)
        outputUSDPerMillionTokens = try container.decode(String?.self, forKey: .outputUSDPerMillionTokens)
        audioUSDPerMinute = try container.decode(String?.self, forKey: .audioUSDPerMinute)
        fetchedAt = try container.decode(String?.self, forKey: .fetchedAt)
        for amount in [inputUSDPerMillionTokens, outputUSDPerMillionTokens, audioUSDPerMinute].compactMap({ $0 }) {
            guard amount.range(of: #"\A(0|[1-9][0-9]*)(\.[0-9]+)?\z"#, options: .regularExpression) != nil else {
                throw DecodingError.dataCorruptedError(
                    forKey: .kind, in: container,
                    debugDescription: "Billing amounts must be non-negative decimal strings or null")
            }
        }
    }
}

public struct ProviderModelDescriptor: Decodable, Equatable, Sendable {
    public let modelID: String
    public let displayName: String
    public let resolvedRevision: String?
    public let inputModalities: [ProviderModality]
    public let outputModalities: [ProviderModality]
    public let roles: [ProviderRole]
    public let timestampSupport: ProviderTimestampSupport
    public let contextWindow: Int?
    public let maxOutputTokens: Int?
    public let parameterSchema: ProviderParameterSchema
    public let availability: ProviderAvailability
    public let reason: String?
    public let source: ProviderModelSource
    public let fetchedAt: String?
    public let billing: ProviderModelBilling

    enum CodingKeys: String, CodingKey {
        case modelID = "model_id"
        case displayName = "display_name"
        case resolvedRevision = "resolved_revision"
        case inputModalities = "input_modalities"
        case outputModalities = "output_modalities"
        case roles
        case timestampSupport = "timestamp_support"
        case contextWindow = "context_window"
        case maxOutputTokens = "max_output_tokens"
        case parameterSchema = "parameter_schema"
        case availability, reason, source
        case fetchedAt = "fetched_at"
        case billing
    }

    public init(
        modelID: String, displayName: String, resolvedRevision: String?,
        inputModalities: [ProviderModality], outputModalities: [ProviderModality],
        roles: [ProviderRole], timestampSupport: ProviderTimestampSupport, contextWindow: Int?,
        maxOutputTokens: Int?, parameterSchema: ProviderParameterSchema,
        availability: ProviderAvailability, reason: String?, source: ProviderModelSource,
        fetchedAt: String?, billing: ProviderModelBilling
    ) {
        self.modelID = modelID
        self.displayName = displayName
        self.resolvedRevision = resolvedRevision
        self.inputModalities = inputModalities
        self.outputModalities = outputModalities
        self.roles = roles
        self.timestampSupport = timestampSupport
        self.contextWindow = contextWindow
        self.maxOutputTokens = maxOutputTokens
        self.parameterSchema = parameterSchema
        self.availability = availability
        self.reason = reason
        self.source = source
        self.fetchedAt = fetchedAt
        self.billing = billing
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        modelID = try container.decode(String.self, forKey: .modelID)
        displayName = try container.decode(String.self, forKey: .displayName)
        resolvedRevision = try container.decode(String?.self, forKey: .resolvedRevision)
        inputModalities = try container.decode([ProviderModality].self, forKey: .inputModalities)
        outputModalities = try container.decode([ProviderModality].self, forKey: .outputModalities)
        roles = try container.decode([ProviderRole].self, forKey: .roles)
        timestampSupport = try container.decode(ProviderTimestampSupport.self, forKey: .timestampSupport)
        contextWindow = try container.decode(Int?.self, forKey: .contextWindow)
        maxOutputTokens = try container.decode(Int?.self, forKey: .maxOutputTokens)
        parameterSchema = try container.decode(ProviderParameterSchema.self, forKey: .parameterSchema)
        availability = try container.decode(ProviderAvailability.self, forKey: .availability)
        reason = try container.decode(String?.self, forKey: .reason)
        source = try container.decode(ProviderModelSource.self, forKey: .source)
        fetchedAt = try container.decode(String?.self, forKey: .fetchedAt)
        billing = try container.decode(ProviderModelBilling.self, forKey: .billing)
        guard contextWindow.map({ $0 > 0 }) ?? true, maxOutputTokens.map({ $0 > 0 }) ?? true,
            Set(inputModalities).count == inputModalities.count,
            Set(outputModalities).count == outputModalities.count, Set(roles).count == roles.count
        else {
            throw DecodingError.dataCorruptedError(
                forKey: .contextWindow, in: container,
                debugDescription: "Model limits and modalities must match the contract")
        }
    }
}
