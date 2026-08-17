import XCTest

@testable import AgnesDesktop

final class AgentMarkdownParserTests: XCTestCase {
  func testParsesAgentAnswerIntoNativeBlocks() {
    let markdown = """
      Here's what's available in your Agnes workspace:

      ## Tables (50 total)

      **Keboola (materialized)** — CRM, financial, and product data:
      - **Sales/CRM**: `account`, `contact`, `opportunity`
      - **Revenue**: `mrr`, `order`, `product`

      > Results reflect your current workspace permissions.
      """

    XCTAssertEqual(
      AgentMarkdownParser.parse(markdown),
      [
        .paragraph("Here's what's available in your Agnes workspace:"),
        .heading(level: 2, text: "Tables (50 total)"),
        .paragraph("**Keboola (materialized)** — CRM, financial, and product data:"),
        .list(
          ordered: false,
          items: [
            .init(
              level: 0, ordinal: nil, text: "**Sales/CRM**: `account`, `contact`, `opportunity`"),
            .init(level: 0, ordinal: nil, text: "**Revenue**: `mrr`, `order`, `product`"),
          ]
        ),
        .quote("Results reflect your current workspace permissions."),
      ]
    )
  }

  func testFencedCodeIsKeptVerbatimAndDoesNotBecomeHeadings() {
    let markdown = """
      ```sql
      ## not a heading
      SELECT * FROM orders
      WHERE status = 'paid';
      ```
      """

    XCTAssertEqual(
      AgentMarkdownParser.parse(markdown),
      [
        .code(
          language: "sql",
          content: "## not a heading\nSELECT * FROM orders\nWHERE status = 'paid';"
        )
      ]
    )
  }

  func testParsesOrderedAndNestedListMarkers() {
    let markdown = """
      3. Inspect the schema
        4. Run the query
      5. Explain the result
      """

    XCTAssertEqual(
      AgentMarkdownParser.parse(markdown),
      [
        .list(
          ordered: true,
          items: [
            .init(level: 0, ordinal: 3, text: "Inspect the schema"),
            .init(level: 1, ordinal: 4, text: "Run the query"),
            .init(level: 0, ordinal: 5, text: "Explain the result"),
          ]
        )
      ]
    )
  }

  func testParsesPipeTableAndKeepsPipeInsideInlineCode() {
    let markdown = """
      | Metric | Expression |
      | :--- | ---: |
      | Revenue | `sum(price | tax)` |
      | Orders | `count(*)` |
      """

    XCTAssertEqual(
      AgentMarkdownParser.parse(markdown),
      [
        .table(
          headers: ["Metric", "Expression"],
          rows: [
            ["Revenue", "`sum(price | tax)`"],
            ["Orders", "`count(*)`"],
          ]
        )
      ]
    )
  }

  func testInlineMarkdownRemovesMarkersFromRenderedCharacters() {
    let attributed = AgentInlineMarkdown.attributedString(
      for: "Use **Revenue** with `mrr` and [documentation](https://example.test)."
    )

    XCTAssertEqual(String(attributed.characters), "Use Revenue with mrr and documentation.")
  }

  func testPreservesHardBreaksButJoinsSoftWrappedParagraphs() {
    let markdown = "first line  \nsecond line\nwraps softly"

    XCTAssertEqual(
      AgentMarkdownParser.parse(markdown),
      [.paragraph("first line\nsecond line wraps softly")]
    )
  }
}
