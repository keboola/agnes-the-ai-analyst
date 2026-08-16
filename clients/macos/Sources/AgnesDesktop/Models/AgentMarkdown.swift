import Foundation

struct AgentMarkdownListItem: Equatable, Sendable {
  let level: Int
  let ordinal: Int?
  let text: String
}

enum AgentMarkdownBlock: Equatable, Sendable {
  case heading(level: Int, text: String)
  case paragraph(String)
  case list(ordered: Bool, items: [AgentMarkdownListItem])
  case quote(String)
  case code(language: String?, content: String)
  case table(headers: [String], rows: [[String]])
  case divider
}

enum AgentMarkdownParser {
  static func parse(_ markdown: String) -> [AgentMarkdownBlock] {
    let normalized =
      markdown
      .replacingOccurrences(of: "\r\n", with: "\n")
      .replacingOccurrences(of: "\r", with: "\n")
    let lines = normalized.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)

    var blocks: [AgentMarkdownBlock] = []
    var index = 0

    while index < lines.count {
      let line = lines[index]
      let trimmed = line.trimmingCharacters(in: .whitespaces)

      if trimmed.isEmpty {
        index += 1
        continue
      }

      if let fence = fence(in: line) {
        let parsed = parseCodeBlock(lines: lines, startingAt: index, fence: fence)
        blocks.append(parsed.block)
        index = parsed.nextIndex
        continue
      }

      if isTable(at: index, in: lines) {
        let parsed = parseTable(lines: lines, startingAt: index)
        blocks.append(parsed.block)
        index = parsed.nextIndex
        continue
      }

      if let heading = heading(in: line) {
        blocks.append(.heading(level: heading.level, text: heading.text))
        index += 1
        continue
      }

      if isDivider(line) {
        blocks.append(.divider)
        index += 1
        continue
      }

      if let firstItem = listItem(in: line) {
        let parsed = parseList(lines: lines, startingAt: index, firstItem: firstItem)
        blocks.append(parsed.block)
        index = parsed.nextIndex
        continue
      }

      if isQuote(line) {
        let parsed = parseQuote(lines: lines, startingAt: index)
        blocks.append(parsed.block)
        index = parsed.nextIndex
        continue
      }

      let parsed = parseParagraph(lines: lines, startingAt: index)
      blocks.append(parsed.block)
      index = parsed.nextIndex
    }

    return blocks
  }

  private struct Fence {
    let marker: String
    let language: String?
  }

  private struct ParsedListItem {
    let ordered: Bool
    let item: AgentMarkdownListItem
  }

  private static func fence(in line: String) -> Fence? {
    let trimmed = line.trimmingCharacters(in: .whitespaces)
    guard let markerCharacter = trimmed.first, markerCharacter == "`" || markerCharacter == "~"
    else {
      return nil
    }

    let marker = String(trimmed.prefix { $0 == markerCharacter })
    guard marker.count >= 3 else { return nil }

    let info = trimmed.dropFirst(marker.count).trimmingCharacters(in: .whitespaces)
    return Fence(marker: marker, language: info.isEmpty ? nil : info)
  }

  private static func parseCodeBlock(
    lines: [String],
    startingAt start: Int,
    fence: Fence
  ) -> (block: AgentMarkdownBlock, nextIndex: Int) {
    var content: [String] = []
    var index = start + 1

    while index < lines.count {
      let trimmed = lines[index].trimmingCharacters(in: .whitespaces)
      if trimmed.hasPrefix(fence.marker) {
        let suffix = trimmed.dropFirst(fence.marker.count).trimmingCharacters(in: .whitespaces)
        if suffix.isEmpty {
          return (
            .code(language: fence.language, content: content.joined(separator: "\n")), index + 1
          )
        }
      }
      content.append(lines[index])
      index += 1
    }

    return (.code(language: fence.language, content: content.joined(separator: "\n")), index)
  }

  private static func heading(in line: String) -> (level: Int, text: String)? {
    let trimmed = line.trimmingCharacters(in: .whitespaces)
    let hashes = trimmed.prefix { $0 == "#" }
    guard (1...6).contains(hashes.count) else { return nil }

    let remainder = trimmed.dropFirst(hashes.count)
    guard remainder.first?.isWhitespace == true else { return nil }
    return (hashes.count, remainder.trimmingCharacters(in: .whitespaces))
  }

  private static func isDivider(_ line: String) -> Bool {
    let compact = line.filter { !$0.isWhitespace }
    guard compact.count >= 3, let marker = compact.first, "-*_".contains(marker) else {
      return false
    }
    return compact.allSatisfy { $0 == marker }
  }

  private static func listItem(in line: String) -> ParsedListItem? {
    var columns = 0
    var contentStart = line.startIndex
    while contentStart < line.endIndex, line[contentStart].isWhitespace {
      columns += line[contentStart] == "\t" ? 2 : 1
      contentStart = line.index(after: contentStart)
    }

    let content = line[contentStart...]
    guard !content.isEmpty else { return nil }
    let level = columns / 2

    if let marker = content.first, "-*+".contains(marker) {
      let afterMarker = content.index(after: content.startIndex)
      guard afterMarker < content.endIndex, content[afterMarker].isWhitespace else { return nil }
      let text = content[afterMarker...].trimmingCharacters(in: .whitespaces)
      return ParsedListItem(
        ordered: false,
        item: AgentMarkdownListItem(level: level, ordinal: nil, text: text)
      )
    }

    let digits = content.prefix { $0.isNumber }
    guard !digits.isEmpty, let ordinal = Int(digits) else { return nil }
    let punctuationIndex = content.index(content.startIndex, offsetBy: digits.count)
    guard punctuationIndex < content.endIndex,
      content[punctuationIndex] == "." || content[punctuationIndex] == ")"
    else {
      return nil
    }
    let afterPunctuation = content.index(after: punctuationIndex)
    guard afterPunctuation < content.endIndex, content[afterPunctuation].isWhitespace else {
      return nil
    }
    let text = content[afterPunctuation...].trimmingCharacters(in: .whitespaces)
    return ParsedListItem(
      ordered: true,
      item: AgentMarkdownListItem(level: level, ordinal: ordinal, text: text)
    )
  }

  private static func parseList(
    lines: [String],
    startingAt start: Int,
    firstItem: ParsedListItem
  ) -> (block: AgentMarkdownBlock, nextIndex: Int) {
    var items: [AgentMarkdownListItem] = []
    var index = start

    while index < lines.count {
      guard let parsed = listItem(in: lines[index]), parsed.ordered == firstItem.ordered else {
        break
      }
      items.append(parsed.item)
      index += 1

      while index < lines.count {
        let continuation = lines[index]
        guard !continuation.trimmingCharacters(in: .whitespaces).isEmpty,
          listItem(in: continuation) == nil,
          leadingWhitespace(in: continuation) > parsed.item.level * 2
        else { break }

        let extra = continuation.trimmingCharacters(in: .whitespaces)
        let previous = items.removeLast()
        items.append(
          AgentMarkdownListItem(
            level: previous.level,
            ordinal: previous.ordinal,
            text: previous.text + " " + extra
          )
        )
        index += 1
      }
    }

    return (.list(ordered: firstItem.ordered, items: items), index)
  }

  private static func leadingWhitespace(in line: String) -> Int {
    var columns = 0
    for character in line {
      guard character.isWhitespace else { break }
      columns += character == "\t" ? 2 : 1
    }
    return columns
  }

  private static func isQuote(_ line: String) -> Bool {
    line.trimmingCharacters(in: .whitespaces).hasPrefix(">")
  }

  private static func parseQuote(
    lines: [String],
    startingAt start: Int
  ) -> (block: AgentMarkdownBlock, nextIndex: Int) {
    var quoted: [String] = []
    var index = start

    while index < lines.count {
      let trimmed = lines[index].trimmingCharacters(in: .whitespaces)
      guard trimmed.hasPrefix(">") else { break }
      quoted.append(trimmed.dropFirst().trimmingCharacters(in: .whitespaces))
      index += 1
    }

    return (.quote(quoted.joined(separator: "\n")), index)
  }

  private static func parseParagraph(
    lines: [String],
    startingAt start: Int
  ) -> (block: AgentMarkdownBlock, nextIndex: Int) {
    var paragraph: [String] = []
    var index = start

    while index < lines.count {
      let line = lines[index]
      guard !line.trimmingCharacters(in: .whitespaces).isEmpty else { break }
      if index > start, startsBlock(at: index, in: lines) { break }
      paragraph.append(line)
      index += 1
    }

    var text = ""
    for (offset, line) in paragraph.enumerated() {
      let hardBreak = line.hasSuffix("  ")
      let rawValue = hardBreak ? String(line.dropLast(2)) : line
      let value = rawValue.trimmingCharacters(in: .whitespaces)
      if offset > 0 {
        text += paragraph[offset - 1].hasSuffix("  ") ? "\n" : " "
      }
      text += value
    }
    return (.paragraph(text), index)
  }

  private static func startsBlock(at index: Int, in lines: [String]) -> Bool {
    let line = lines[index]
    return fence(in: line) != nil
      || heading(in: line) != nil
      || isDivider(line)
      || listItem(in: line) != nil
      || isQuote(line)
      || isTable(at: index, in: lines)
  }

  private static func isTable(at index: Int, in lines: [String]) -> Bool {
    guard index + 1 < lines.count else { return false }
    let headers = tableCells(in: lines[index])
    let separator = tableCells(in: lines[index + 1])
    return headers.count >= 2
      && headers.count == separator.count
      && separator.allSatisfy(isTableSeparator)
  }

  private static func parseTable(
    lines: [String],
    startingAt start: Int
  ) -> (block: AgentMarkdownBlock, nextIndex: Int) {
    let headers = tableCells(in: lines[start])
    var rows: [[String]] = []
    var index = start + 2

    while index < lines.count {
      let line = lines[index]
      guard !line.trimmingCharacters(in: .whitespaces).isEmpty else { break }
      var cells = tableCells(in: line)
      guard cells.count >= 2 else { break }
      if cells.count < headers.count {
        cells.append(contentsOf: repeatElement("", count: headers.count - cells.count))
      } else if cells.count > headers.count {
        cells = Array(cells.prefix(headers.count))
      }
      rows.append(cells)
      index += 1
    }

    return (.table(headers: headers, rows: rows), index)
  }

  private static func tableCells(in line: String) -> [String] {
    let trimmed = line.trimmingCharacters(in: .whitespaces)
    guard trimmed.contains("|") else { return [] }

    var cells: [String] = []
    var cell = ""
    var isEscaped = false
    var isInCode = false

    for character in trimmed {
      if isEscaped {
        cell.append(character)
        isEscaped = false
      } else if character == "\\" {
        cell.append(character)
        isEscaped = true
      } else if character == "`" {
        isInCode.toggle()
        cell.append(character)
      } else if character == "|", !isInCode {
        cells.append(cell.trimmingCharacters(in: .whitespaces))
        cell = ""
      } else {
        cell.append(character)
      }
    }
    cells.append(cell.trimmingCharacters(in: .whitespaces))

    if trimmed.hasPrefix("|"), cells.first?.isEmpty == true { cells.removeFirst() }
    if trimmed.hasSuffix("|"), cells.last?.isEmpty == true { cells.removeLast() }
    return cells
  }

  private static func isTableSeparator(_ cell: String) -> Bool {
    var value = cell.trimmingCharacters(in: .whitespaces)
    if value.hasPrefix(":") { value.removeFirst() }
    if value.hasSuffix(":") { value.removeLast() }
    return value.count >= 3 && value.allSatisfy { $0 == "-" }
  }
}

enum AgentInlineMarkdown {
  static func attributedString(for source: String) -> AttributedString {
    let options = AttributedString.MarkdownParsingOptions(
      interpretedSyntax: .inlineOnlyPreservingWhitespace,
      failurePolicy: .returnPartiallyParsedIfPossible
    )
    return (try? AttributedString(markdown: source, options: options)) ?? AttributedString(source)
  }
}
