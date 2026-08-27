import Foundation

/// One block of the lightweight markdown rendering used for the minutes preview.
///
/// Deliberately minimal and dependency-free: headings, paragraphs, bullet / ordered lists,
/// fenced code, tables (kept as raw lines for a monospaced fallback) and thematic breaks.
/// Inline emphasis stays inside the text; the SwiftUI layer renders it with
/// `AttributedString(markdown:)` (Foundation) per block.
public enum MarkdownBlock: Equatable, Sendable {
    case heading(level: Int, text: String)
    case paragraph(String)
    /// Items keep their text without the `-` / `*` / `+` marker; nesting is flattened.
    case bulletList([String])
    /// Items keep their text without the `1.` marker.
    case orderedList([String])
    case codeBlock(String)
    /// Raw `|`-delimited lines including the separator row; rendered monospaced.
    case table([String])
    case rule
}

public enum MarkdownParser {
    /// Split markdown into blocks. Never fails; unknown constructs become paragraphs.
    public static func blocks(from markdown: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var bullets: [String] = []
        var ordered: [String] = []
        var tableRows: [String] = []
        var codeLines: [String] = []
        var inCode = false

        func flushParagraph() {
            if !paragraph.isEmpty {
                blocks.append(.paragraph(paragraph.joined(separator: "\n")))
                paragraph.removeAll()
            }
        }
        func flushBullets() {
            if !bullets.isEmpty {
                blocks.append(.bulletList(bullets))
                bullets.removeAll()
            }
        }
        func flushOrdered() {
            if !ordered.isEmpty {
                blocks.append(.orderedList(ordered))
                ordered.removeAll()
            }
        }
        func flushTable() {
            if !tableRows.isEmpty {
                blocks.append(.table(tableRows))
                tableRows.removeAll()
            }
        }
        func flushAll() {
            flushParagraph()
            flushBullets()
            flushOrdered()
            flushTable()
        }

        for rawLine in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(rawLine)
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            if inCode {
                if trimmed.hasPrefix("```") {
                    blocks.append(.codeBlock(codeLines.joined(separator: "\n")))
                    codeLines.removeAll()
                    inCode = false
                } else {
                    codeLines.append(line)
                }
                continue
            }
            if trimmed.hasPrefix("```") {
                flushAll()
                inCode = true
                continue
            }
            if trimmed.isEmpty {
                flushAll()
                continue
            }
            if let heading = parseHeading(trimmed) {
                flushAll()
                blocks.append(heading)
                continue
            }
            if trimmed.hasPrefix("|") {
                flushParagraph()
                flushBullets()
                flushOrdered()
                tableRows.append(trimmed)
                continue
            }
            if isRule(trimmed) {
                flushAll()
                blocks.append(.rule)
                continue
            }
            if let item = parseBullet(trimmed) {
                flushParagraph()
                flushOrdered()
                flushTable()
                bullets.append(item)
                continue
            }
            if let item = parseOrdered(trimmed) {
                flushParagraph()
                flushBullets()
                flushTable()
                ordered.append(item)
                continue
            }
            flushBullets()
            flushOrdered()
            flushTable()
            paragraph.append(trimmed)
        }
        if inCode {
            // Unclosed fence: keep what was collected instead of dropping it.
            blocks.append(.codeBlock(codeLines.joined(separator: "\n")))
        }
        flushAll()
        return blocks
    }

    private static func parseHeading(_ line: String) -> MarkdownBlock? {
        guard line.hasPrefix("#") else {
            return nil
        }
        let level = line.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(level) else {
            return nil
        }
        let rest = line.dropFirst(level)
        guard rest.first == " " || rest.isEmpty else {
            return nil  // "#hashtag" is not a heading
        }
        return .heading(level: level, text: rest.trimmingCharacters(in: .whitespaces))
    }

    private static func parseBullet(_ line: String) -> String? {
        for marker in ["- ", "* ", "+ "] where line.hasPrefix(marker) {
            return String(line.dropFirst(marker.count)).trimmingCharacters(in: .whitespaces)
        }
        return nil
    }

    private static func parseOrdered(_ line: String) -> String? {
        let digits = line.prefix(while: { $0.isNumber })
        guard !digits.isEmpty else {
            return nil
        }
        let rest = line.dropFirst(digits.count)
        guard rest.hasPrefix(". ") else {
            return nil
        }
        return String(rest.dropFirst(2)).trimmingCharacters(in: .whitespaces)
    }

    /// `---` / `***` / `___` (3+ of the same character, nothing else).
    private static func isRule(_ line: String) -> Bool {
        guard line.count >= 3, let first = line.first, "-*_".contains(first) else {
            return false
        }
        return line.allSatisfy { $0 == first }
    }
}
