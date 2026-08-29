import Foundation

public enum MinutesGenerationMode: String, CaseIterable, Sendable {
    case legacy, single, ensemble

    public var title: String {
        switch self {
        case .legacy: return "従来設定"
        case .single: return "接続とモデルを指定"
        case .ensemble: return "複数案を生成して統合"
        }
    }
}

public struct MinutesEnsembleGeneratorForm: Equatable, Sendable, Identifiable {
    public let id: String
    public var label: String
    public var model: MinutesModelForm

    public init(id: String = MinutesEnsembleGenerator.newID(), label: String, model: MinutesModelForm = .init()) {
        self.id = id
        self.label = label
        self.model = model
    }

    public init(generator: MinutesEnsembleGenerator) {
        id = generator.id
        label = generator.label
        model = MinutesModelForm(selection: generator.selection)
    }

    public var generator: MinutesEnsembleGenerator? {
        guard let selection = model.selection else { return nil }
        let value = MinutesEnsembleGenerator(id: id, label: label, selection: selection)
        return value.isWellFormed ? value : nil
    }
}

public struct MinutesEnsembleForm: Equatable, Sendable {
    public var generators: [MinutesEnsembleGeneratorForm]
    public var synthesizer: MinutesModelForm

    public init(selection: MinutesEnsembleSelection? = nil) {
        if let selection {
            generators = selection.generators.map(MinutesEnsembleGeneratorForm.init)
            synthesizer = MinutesModelForm(selection: selection.synthesizer)
        } else {
            generators = [
                MinutesEnsembleGeneratorForm(label: "案1", model: Self.emptyModel()),
                MinutesEnsembleGeneratorForm(label: "案2", model: Self.emptyModel()),
            ]
            synthesizer = Self.emptyModel()
        }
    }

    @discardableResult
    public mutating func addGenerator(
        id: String = MinutesEnsembleGenerator.newID(), label: String? = nil
    ) -> Bool {
        guard generators.count < 4, !generators.contains(where: { $0.id == id }) else { return false }
        generators.append(MinutesEnsembleGeneratorForm(
            id: id, label: label ?? "案\(generators.count + 1)", model: Self.emptyModel()))
        return true
    }

    @discardableResult
    public mutating func removeGenerator(id: String) -> Bool {
        guard generators.count > 2, let index = generators.firstIndex(where: { $0.id == id }) else {
            return false
        }
        generators.remove(at: index)
        return true
    }

    public mutating func moveGenerator(fromOffsets: IndexSet, toOffset: Int) {
        let moving = fromOffsets.sorted().map { generators[$0] }
        for index in fromOffsets.sorted(by: >) { generators.remove(at: index) }
        let removedBefore = fromOffsets.filter { $0 < toOffset }.count
        generators.insert(contentsOf: moving, at: max(0, min(generators.count, toOffset - removedBefore)))
    }

    public mutating func activateEditing() {
        for index in generators.indices { generators[index].model.mode = .selected }
        synthesizer.mode = .selected
    }

    public var selection: MinutesEnsembleSelection? {
        guard (2...4).contains(generators.count),
            Set(generators.map(\.id)).count == generators.count,
            let synthesizer = synthesizer.selection else { return nil }
        let values = generators.compactMap(\.generator)
        guard values.count == generators.count else { return nil }
        let result = MinutesEnsembleSelection(generators: values, synthesizer: synthesizer)
        return result.isWellFormed ? result : nil
    }

    public var structuralValidationMessage: String? {
        guard (2...4).contains(generators.count) else { return "生成担当は2〜4件にしてください。" }
        guard Set(generators.map(\.id)).count == generators.count,
            generators.allSatisfy({ MinutesEnsembleGenerator.isValidID($0.id) }) else {
            return "生成担当の識別子が重複または不正です。"
        }
        for generator in generators {
            guard (1...80).contains(generator.label.unicodeScalars.count),
                generator.label.range(of: #"\S"#, options: .regularExpression) != nil else {
                return "各生成担当の表示名を1〜80文字で入力してください。"
            }
            guard generator.model.selection != nil else {
                return "各生成担当の接続・モデル・パラメータを選んでください。"
            }
        }
        return synthesizer.selection == nil ? "統合担当の接続・モデル・パラメータを選んでください。" : nil
    }

    private static func emptyModel() -> MinutesModelForm {
        var form = MinutesModelForm()
        form.mode = .selected
        return form
    }
}
