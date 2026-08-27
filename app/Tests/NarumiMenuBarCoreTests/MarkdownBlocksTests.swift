import XCTest

@testable import NarumiMenuBarCore

final class MarkdownBlocksTests: XCTestCase {
    func testMinutesShapedDocument() {
        let markdown = """
            # 週次定例

            ## 決定事項
            - オンボーディング資料を来週までに更新する
            - 次回は 9/3

            本文の段落です。
            続きの行。
            """
        let blocks = MarkdownParser.blocks(from: markdown)
        XCTAssertEqual(
            blocks,
            [
                .heading(level: 1, text: "週次定例"),
                .heading(level: 2, text: "決定事項"),
                .bulletList(["オンボーディング資料を来週までに更新する", "次回は 9/3"]),
                .paragraph("本文の段落です。\n続きの行。"),
            ])
    }

    func testHeadingRules() {
        XCTAssertEqual(
            MarkdownParser.blocks(from: "### 見出し"),
            [.heading(level: 3, text: "見出し")])
        // "#hashtag" is not a heading.
        XCTAssertEqual(MarkdownParser.blocks(from: "#hashtag"), [.paragraph("#hashtag")])
        // 7 hashes exceed the heading range.
        XCTAssertEqual(MarkdownParser.blocks(from: "####### x"), [.paragraph("####### x")])
    }

    func testOrderedListAndMarkers() {
        XCTAssertEqual(
            MarkdownParser.blocks(from: "1. 一つ目\n2. 二つ目"),
            [.orderedList(["一つ目", "二つ目"])])
        XCTAssertEqual(
            MarkdownParser.blocks(from: "* star\n+ plus"),
            [.bulletList(["star", "plus"])])
    }

    func testCodeFence() {
        let markdown = "```\nlet x = 1\nlet y = 2\n```"
        XCTAssertEqual(MarkdownParser.blocks(from: markdown), [.codeBlock("let x = 1\nlet y = 2")])
    }

    func testUnclosedCodeFenceKeepsContent() {
        XCTAssertEqual(
            MarkdownParser.blocks(from: "```\norphan line"),
            [.codeBlock("orphan line")])
    }

    func testTableKeptAsRawRows() {
        let markdown = "| a | b |\n|---|---|\n| 1 | 2 |"
        XCTAssertEqual(
            MarkdownParser.blocks(from: markdown),
            [.table(["| a | b |", "|---|---|", "| 1 | 2 |"])])
    }

    func testThematicBreak() {
        XCTAssertEqual(
            MarkdownParser.blocks(from: "上\n\n---\n\n下"),
            [.paragraph("上"), .rule, .paragraph("下")])
        // A rule with mixed characters is a paragraph, not a rule.
        XCTAssertEqual(MarkdownParser.blocks(from: "--*"), [.paragraph("--*")])
    }

    func testBlankInputProducesNoBlocks() {
        XCTAssertEqual(MarkdownParser.blocks(from: ""), [])
        XCTAssertEqual(MarkdownParser.blocks(from: "\n\n"), [])
    }
}
