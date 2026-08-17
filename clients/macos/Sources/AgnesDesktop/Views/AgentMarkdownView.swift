import SwiftUI

struct AgentMarkdownView: View {
  private let blocks: [AgentMarkdownBlock]

  init(markdown: String) {
    blocks = AgentMarkdownParser.parse(markdown)
  }

  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
        blockView(block)
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .foregroundStyle(AgnesTheme.textSecondary)
    .textSelection(.enabled)
  }

  @ViewBuilder
  private func blockView(_ block: AgentMarkdownBlock) -> some View {
    switch block {
    case .heading(let level, let text):
      AgentInlineMarkdownText(source: text)
        .font(headingFont(for: level))
        .foregroundStyle(AgnesTheme.text)
        .padding(.top, level <= 2 ? 4 : 1)
    case .paragraph(let text):
      AgentInlineMarkdownText(source: text)
    case .list(let ordered, let items):
      VStack(alignment: .leading, spacing: 5) {
        ForEach(Array(items.enumerated()), id: \.offset) { index, item in
          HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(listMarker(ordered: ordered, item: item, fallbackIndex: index))
              .font(.system(size: 13, design: .monospaced))
              .foregroundStyle(AgnesTheme.textMuted)
              .frame(width: ordered ? 28 : 14, alignment: .trailing)
            AgentInlineMarkdownText(source: item.text)
              .frame(maxWidth: .infinity, alignment: .leading)
          }
          .padding(.leading, CGFloat(item.level) * 16)
        }
      }
    case .quote(let text):
      HStack(alignment: .top, spacing: 10) {
        RoundedRectangle(cornerRadius: 1.5)
          .fill(AgnesTheme.action.opacity(0.55))
          .frame(width: 3)
        AgentInlineMarkdownText(source: text)
          .foregroundStyle(AgnesTheme.textMuted)
          .italic()
          .padding(.vertical, 2)
      }
    case .code(let language, let content):
      AgentMarkdownCodeBlock(language: language, content: content)
    case .table(let headers, let rows):
      AgentMarkdownTable(headers: headers, rows: rows)
    case .divider:
      Divider().overlay(AgnesTheme.border)
    }
  }

  private func headingFont(for level: Int) -> Font {
    switch level {
    case 1: .title2.weight(.bold)
    case 2: .headline.weight(.bold)
    case 3: .subheadline.weight(.bold)
    default: .body.weight(.semibold)
    }
  }

  private func listMarker(
    ordered: Bool,
    item: AgentMarkdownListItem,
    fallbackIndex: Int
  ) -> String {
    ordered ? "\(item.ordinal ?? fallbackIndex + 1)." : "•"
  }
}

private struct AgentInlineMarkdownText: View {
  let source: String

  var body: some View {
    Text(AgentInlineMarkdown.attributedString(for: source))
      .tint(AgnesTheme.action)
  }
}

private struct AgentMarkdownCodeBlock: View {
  let language: String?
  let content: String

  var body: some View {
    VStack(alignment: .leading, spacing: 0) {
      if let language {
        Text(language.uppercased())
          .font(.caption2.monospaced().weight(.semibold))
          .foregroundStyle(AgnesTheme.textMuted)
          .padding(.horizontal, 11)
          .padding(.vertical, 7)
      }
      ScrollView(.horizontal) {
        Text(content)
          .font(.system(size: 12.5, design: .monospaced))
          .foregroundStyle(AgnesTheme.text)
          .fixedSize(horizontal: true, vertical: false)
          .padding(11)
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(AgnesTheme.surfaceMuted, in: RoundedRectangle(cornerRadius: 8))
    .overlay {
      RoundedRectangle(cornerRadius: 8)
        .stroke(AgnesTheme.border, lineWidth: 1)
    }
  }
}

private struct AgentMarkdownTable: View {
  let headers: [String]
  let rows: [[String]]

  private let cellWidth: CGFloat = 180

  var body: some View {
    ScrollView(.horizontal) {
      VStack(alignment: .leading, spacing: 0) {
        row(headers, isHeader: true)
        ForEach(Array(rows.enumerated()), id: \.offset) { index, cells in
          Divider().overlay(AgnesTheme.border)
          row(cells, isHeader: false, shaded: index.isMultiple(of: 2))
        }
      }
      .background(AgnesTheme.surface)
      .clipShape(RoundedRectangle(cornerRadius: 8))
      .overlay {
        RoundedRectangle(cornerRadius: 8)
          .stroke(AgnesTheme.border, lineWidth: 1)
      }
    }
  }

  private func row(_ cells: [String], isHeader: Bool, shaded: Bool = false) -> some View {
    HStack(alignment: .top, spacing: 0) {
      ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
        AgentInlineMarkdownText(source: cell)
          .font(isHeader ? .caption.weight(.bold) : .caption)
          .foregroundStyle(isHeader ? AgnesTheme.text : AgnesTheme.textSecondary)
          .frame(width: cellWidth, alignment: .leading)
          .padding(.horizontal, 10)
          .padding(.vertical, 8)
          .overlay(alignment: .trailing) {
            if index < cells.count - 1 {
              Rectangle()
                .fill(AgnesTheme.border)
                .frame(width: 1)
            }
          }
      }
    }
    .background(
      isHeader || shaded ? AgnesTheme.surfaceMuted.opacity(isHeader ? 0.9 : 0.45) : .clear)
  }
}
