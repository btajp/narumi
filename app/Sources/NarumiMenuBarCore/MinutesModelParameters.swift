import Foundation

extension MinutesModelForm {
    /// An application limit; it must never be written into model capability metadata.
    public static func defaultOutputLimit(_ model: ProviderModelDescriptor?) -> Int {
        min(4096, model?.maxOutputTokens ?? 4096)
    }

    public func effectiveOutputLimit(_ model: ProviderModelDescriptor?) -> Int? {
        guard allowsMaxTokens else { return nil }
        return maxTokensText.isEmpty ? Self.defaultOutputLimit(model) : parsedMaxTokens
    }

    func parameterValidationMessage(_ model: ProviderModelDescriptor) -> String? {
        let effortParameter = model.parameterSchema.properties["reasoning_effort"]
        if !reasoningEffort.isEmpty {
            guard allowsReasoningEffort, let effortParameter,
                Self.reasoningOptions(model).contains(reasoningEffort),
                effortParameter.accepts(.string(reasoningEffort)) else {
                return "保存された推論量はこのモデルで利用できません。候補から選び直してください。"
            }
        } else if allowsReasoningEffort, let effortParameter {
            if let defaultValue = effortParameter.defaultValue {
                guard case .string(let value) = defaultValue,
                    Self.reasoningOptions(model).contains(value), effortParameter.accepts(defaultValue) else {
                    return "このモデルの推論量の既定値を確認できません。候補から明示的に選んでください。"
                }
            } else if model.parameterSchema.required.contains("reasoning_effort") {
                return "このモデルの推論量を選んでください。"
            }
        }
        guard allowsMaxTokens else {
            return maxTokensText.isEmpty ? nil : "Codex では出力上限を指定できません。"
        }
        guard let value = effectiveOutputLimit(model), (1...32768).contains(value) else {
            return "出力上限は 1〜32,768 の整数を入力してください。空欄ではアプリの既定値を使います。"
        }
        guard let parameter = model.parameterSchema.properties["max_tokens"],
            parameter.type == .integer, parameter.accepts(.number(Double(value))) else {
            return "出力上限が、このモデルで確認できた設定範囲に入っていません。"
        }
        if let knownLimit = model.maxOutputTokens, value > knownLimit {
            return "出力上限は、このモデルで確認済みの \(knownLimit) トークン以下にしてください。"
        }
        return nil
    }
}

private extension ProviderModelParameter {
    func accepts(_ value: ProviderScalarValue) -> Bool {
        switch (type, value) {
        case (.string, .string), (.boolean, .boolean): break
        case (.integer, .number(let number)):
            guard number.isFinite, number.rounded() == number else { return false }
        case (.number, .number(let number)):
            guard number.isFinite else { return false }
        default: return false
        }
        if let enumValues, !enumValues.contains(value) { return false }
        if case .number(let number) = value {
            if let minimum, !minimum.isFinite || number < minimum { return false }
            if let maximum, !maximum.isFinite || number > maximum { return false }
        }
        return true
    }
}
