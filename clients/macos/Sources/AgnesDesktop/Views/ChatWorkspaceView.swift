import SwiftUI

struct ChatWorkspaceView: View {
  @ObservedObject var model: AppModel

  private let suggestions = [
    AskSuggestion(
      title: "Review a product through hypothetical personas",
      prompt: """
        Create a hypothetical usability review of this product from three distinct user personas. For each persona, describe their goal, likely friction, and one improvement. End with three testable hypotheses and one success metric. Clearly label this as a simulation, not user evidence.
        """
    ),
    AskSuggestion(
      title: "Summarize the latest revenue trends",
      prompt: "Summarize the latest revenue trends."
    ),
    AskSuggestion(
      title: "Which metrics changed the most this month?",
      prompt: "Which metrics changed the most this month?"
    ),
    AskSuggestion(
      title: "Explain the biggest business risk in plain English",
      prompt: "Explain the biggest business risk in plain English."
    ),
  ]

  var body: some View {
    ScrollView {
      VStack(spacing: 26) {
        ChatHero()

        VStack(spacing: 18) {
          VStack(spacing: 7) {
            Text("Ask Agnes anything")
              .font(.system(size: 27, weight: .bold))
              .foregroundStyle(AgnesTheme.text)
            Text("Send one focused request through the Agnes CLI on this Mac.")
              .font(.system(size: 14))
              .foregroundStyle(AgnesTheme.textSecondary)
          }

          agentField
          promptComposer

          if model.isAsking {
            HStack(spacing: 9) {
              ProgressView()
                .controlSize(.small)
              Text(model.isStopping ? "Stopping request…" : "Agnes is working…")
                .font(.subheadline)
                .foregroundStyle(AgnesTheme.textSecondary)
            }
          }

          if let error = model.askError {
            AskErrorCard(message: error)
          } else if let result = model.result {
            AskResultCard(result: result)
          } else if !model.isAsking {
            suggestionsView
          }
        }
        .frame(maxWidth: 840)
        .padding(.horizontal, 28)
        .padding(.bottom, 36)
      }
      .frame(maxWidth: .infinity)
    }
    .background(AgnesTheme.canvas)
    .toolbar {
      ToolbarItemGroup(placement: .primaryAction) {
        Button {
          model.clearAsk()
        } label: {
          Label("Clear request", systemImage: "trash")
        }
        .disabled(
          model.isAsking
            || (model.result == nil && model.askError == nil && model.prompt.isEmpty))

        if model.isAsking {
          Button {
            model.stopRequest()
          } label: {
            Label("Stop", systemImage: "stop.fill")
          }
          .disabled(model.isStopping)
        }
      }
    }
  }

  private var agentField: some View {
    VStack(alignment: .leading, spacing: 8) {
      HStack {
        Label("Agent slug", systemImage: "person.crop.circle.badge.checkmark")
          .font(.system(size: 13, weight: .semibold))
          .foregroundStyle(AgnesTheme.text)
        Spacer()
        Text("Saved on this Mac")
          .font(.caption)
          .foregroundStyle(AgnesTheme.textMuted)
      }

      TextField("For example: analyst", text: $model.agentSlug)
        .textFieldStyle(.plain)
        .font(.system(size: 14, design: .monospaced))
        .padding(.horizontal, 13)
        .frame(height: 40)
        .background(AgnesTheme.surfaceMuted, in: RoundedRectangle(cornerRadius: 9))
        .overlay {
          RoundedRectangle(cornerRadius: 9)
            .stroke(AgnesTheme.border, lineWidth: 1)
        }
        .disabled(model.hasActiveCLICommand)

      Text(
        "PAT-authenticated CLI sessions cannot list web agent profiles. Copy the slug from Agnes Agents and paste it here."
      )
      .font(.caption)
      .foregroundStyle(AgnesTheme.textMuted)
      .fixedSize(horizontal: false, vertical: true)
    }
    .agnesCard(padding: 16)
  }

  private var promptComposer: some View {
    VStack(alignment: .leading, spacing: 12) {
      TextEditor(text: $model.prompt)
        .font(.system(size: 15))
        .scrollContentBackground(.hidden)
        .frame(minHeight: 108)
        .padding(10)
        .background(AgnesTheme.surface)
        .disabled(model.isAsking)
        .accessibilityLabel("Prompt")

      Divider()
        .overlay(AgnesTheme.border)

      HStack(spacing: 10) {
        Label("Uses your local Agnes CLI session", systemImage: "lock.shield")
          .font(.caption)
          .foregroundStyle(AgnesTheme.textMuted)
        Spacer()

        if model.isAsking {
          Button("Stop", systemImage: "stop.fill") {
            model.stopRequest()
          }
          .buttonStyle(AgnesSecondaryButtonStyle())
          .disabled(model.isStopping)
        } else {
          Button("Ask", systemImage: "arrow.up") {
            model.submitPrompt()
          }
          .buttonStyle(AgnesPrimaryButtonStyle())
          .keyboardShortcut(.return, modifiers: [.command])
          .disabled(!model.canAsk)
        }
      }
    }
    .agnesCard(padding: 12)
  }

  private var suggestionsView: some View {
    VStack(alignment: .leading, spacing: 10) {
      Text("TRY ASKING")
        .font(.system(size: 11, weight: .bold))
        .tracking(1.2)
        .foregroundStyle(AgnesTheme.textMuted)

      ForEach(suggestions) { suggestion in
        Button {
          model.prompt = suggestion.prompt
        } label: {
          HStack(spacing: 11) {
            Image(systemName: "sparkle")
              .foregroundStyle(AgnesTheme.action)
            Text(suggestion.title)
              .font(.system(size: 14))
              .foregroundStyle(AgnesTheme.text)
            Spacer()
            Image(systemName: "arrow.up.left")
              .font(.caption)
              .foregroundStyle(AgnesTheme.textMuted)
          }
          .padding(.horizontal, 15)
          .frame(minHeight: 43)
          .background(AgnesTheme.surface, in: RoundedRectangle(cornerRadius: 10))
          .overlay {
            RoundedRectangle(cornerRadius: 10)
              .stroke(AgnesTheme.border, lineWidth: 1)
          }
        }
        .buttonStyle(.plain)
        .disabled(model.isAsking)
      }
    }
  }
}

private struct AskSuggestion: Identifiable {
  let title: String
  let prompt: String

  var id: String { title }
}

private struct ChatHero: View {
  var body: some View {
    ZStack {
      LinearGradient(
        colors: [
          Color(red: 239 / 255, green: 246 / 255, blue: 255 / 255),
          Color(red: 248 / 255, green: 250 / 255, blue: 252 / 255),
          Color(red: 245 / 255, green: 243 / 255, blue: 255 / 255),
        ],
        startPoint: .leading,
        endPoint: .trailing
      )

      HStack {
        decorativeOrb(color: AgnesTheme.action, size: 124)
          .offset(x: -38, y: 24)
        Spacer()
        decorativeOrb(color: .purple, size: 88)
          .offset(x: 24, y: -28)
      }
      .opacity(0.12)

      VStack(spacing: 10) {
        AgnesTheme.mark
          .resizable()
          .scaledToFit()
          .frame(width: 48, height: 48)
        Text("Secure. Private. Always in sync.")
          .font(.system(size: 13, weight: .semibold))
          .foregroundStyle(AgnesTheme.textSecondary)
      }
    }
    .frame(height: 178)
    .frame(maxWidth: .infinity)
    .clipped()
    .overlay(alignment: .bottom) {
      Rectangle()
        .fill(AgnesTheme.border)
        .frame(height: 1)
    }
    .accessibilityElement(children: .combine)
  }

  private func decorativeOrb(color: Color, size: CGFloat) -> some View {
    Circle()
      .fill(color)
      .frame(width: size, height: size)
      .blur(radius: 18)
  }
}

private struct AskErrorCard: View {
  let message: String

  var body: some View {
    HStack(alignment: .top, spacing: 12) {
      Image(systemName: "exclamationmark.triangle.fill")
        .foregroundStyle(.orange)
      VStack(alignment: .leading, spacing: 5) {
        Text("Request unavailable")
          .font(.headline)
          .foregroundStyle(AgnesTheme.text)
        Text(message)
          .font(.system(size: 13))
          .foregroundStyle(AgnesTheme.textSecondary)
          .textSelection(.enabled)
      }
      Spacer()
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .agnesCard(padding: 18)
  }
}

private struct AskResultCard: View {
  let result: AskResult

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      Label("Answer", systemImage: "sparkles")
        .font(.headline)
        .foregroundStyle(AgnesTheme.text)

      Text(result.answer.isEmpty ? "(No answer returned.)" : result.answer)
        .font(.system(size: 14))
        .foregroundStyle(AgnesTheme.textSecondary)
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)

      if !result.toolNames.isEmpty {
        Divider()
          .overlay(AgnesTheme.border)
        VStack(alignment: .leading, spacing: 8) {
          Label("Tools used", systemImage: "wrench.and.screwdriver")
            .font(.subheadline.weight(.semibold))
          FlowLayout(spacing: 7) {
            ForEach(Array(result.toolNames.enumerated()), id: \.offset) { _, name in
              Text(name)
                .font(.caption.monospaced())
                .foregroundStyle(AgnesTheme.textSecondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(AgnesTheme.surfaceMuted, in: Capsule())
            }
          }
        }
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .agnesCard(padding: 20)
  }
}

private struct FlowLayout: Layout {
  var spacing: CGFloat

  init(spacing: CGFloat = 8) {
    self.spacing = spacing
  }

  func sizeThatFits(
    proposal: ProposedViewSize,
    subviews: Subviews,
    cache: inout ()
  ) -> CGSize {
    let maxWidth = proposal.width ?? .greatestFiniteMagnitude
    var lineWidth: CGFloat = 0
    var lineHeight: CGFloat = 0
    var totalHeight: CGFloat = 0

    for subview in subviews {
      let size = subview.sizeThatFits(.unspecified)
      if lineWidth > 0, lineWidth + spacing + size.width > maxWidth {
        totalHeight += lineHeight + spacing
        lineWidth = 0
        lineHeight = 0
      }
      lineWidth += (lineWidth == 0 ? 0 : spacing) + size.width
      lineHeight = max(lineHeight, size.height)
    }
    return CGSize(width: proposal.width ?? lineWidth, height: totalHeight + lineHeight)
  }

  func placeSubviews(
    in bounds: CGRect,
    proposal: ProposedViewSize,
    subviews: Subviews,
    cache: inout ()
  ) {
    var point = bounds.origin
    var lineHeight: CGFloat = 0

    for subview in subviews {
      let size = subview.sizeThatFits(.unspecified)
      if point.x > bounds.minX, point.x + spacing + size.width > bounds.maxX {
        point.x = bounds.minX
        point.y += lineHeight + spacing
        lineHeight = 0
      }
      if point.x > bounds.minX {
        point.x += spacing
      }
      subview.place(at: point, proposal: ProposedViewSize(size))
      point.x += size.width
      lineHeight = max(lineHeight, size.height)
    }
  }
}
