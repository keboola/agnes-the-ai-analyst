import SwiftUI

struct ChatWorkspaceView: View {
  @ObservedObject var model: AppModel

  private let suggestions = [
    AskSuggestion(
      title: "Explain a metric movement",
      prompt: "Explain which metrics changed the most this month and cite the evidence."
    ),
    AskSuggestion(
      title: "Investigate revenue risk",
      prompt: "Identify the largest current revenue risk. Separate facts from inference."
    ),
    AskSuggestion(
      title: "Audit data quality",
      prompt: "Check the most important available dataset for freshness and data-quality risks."
    ),
  ]

  var body: some View {
    VStack(spacing: 0) {
      workspaceHeader
      Divider().overlay(AgnesTheme.border)

      HSplitView {
        transcriptPane
          .frame(minWidth: 500, idealWidth: 720)
        inspectorPane
          .frame(minWidth: 300, idealWidth: 350, maxWidth: 430)
      }

      Divider().overlay(AgnesTheme.border)
      statusBar
    }
    .background(AgnesTheme.canvas)
    .toolbar {
      ToolbarItemGroup(placement: .primaryAction) {
        Button {
          model.clearRuns()
        } label: {
          Label("Clear local runs", systemImage: "trash")
        }
        .disabled(model.isRunningAgent || model.runs.isEmpty)

        if model.isRunningAgent {
          Button {
            model.stopRun()
          } label: {
            Label("Stop run", systemImage: "stop.fill")
          }
          .disabled(model.isStoppingAgent)
          .keyboardShortcut(".", modifiers: [.command])
        }
      }
    }
  }

  private var workspaceHeader: some View {
    HStack(spacing: 14) {
      VStack(alignment: .leading, spacing: 3) {
        HStack(spacing: 8) {
          Text("Agent Runs")
            .font(.system(size: 20, weight: .bold))
            .foregroundStyle(AgnesTheme.text)
          Text("MVP v2")
            .font(.caption.weight(.semibold))
            .foregroundStyle(AgnesTheme.action)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(AgnesTheme.actionTint, in: Capsule())
        }
        Text("Inspectable, isolated runs through your local Agnes CLI.")
          .font(.caption)
          .foregroundStyle(AgnesTheme.textMuted)
      }

      Spacer()

      Label("CLI-backed", systemImage: "terminal")
        .font(.caption.monospaced())
        .foregroundStyle(AgnesTheme.textSecondary)
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(AgnesTheme.surfaceMuted, in: Capsule())
        .overlay { Capsule().stroke(AgnesTheme.border) }
    }
    .padding(.horizontal, 20)
    .frame(height: 68)
    .background(AgnesTheme.surface)
  }

  private var transcriptPane: some View {
    VStack(spacing: 0) {
      ScrollViewReader { proxy in
        ScrollView {
          LazyVStack(spacing: 14) {
            isolatedModeNotice

            if model.runs.isEmpty {
              emptyRuns
            } else {
              ForEach(Array(model.runs.reversed())) { run in
                AgentRunCard(
                  run: run,
                  selected: model.selectedRunID == run.id,
                  onSelect: { model.selectRun(run.id) }
                )
                .id(run.id)
              }
            }
          }
          .padding(20)
        }
        .onChange(of: model.runs.count) { _, _ in
          guard let activeID = model.activeRun?.id else { return }
          withAnimation(.easeOut(duration: 0.2)) {
            proxy.scrollTo(activeID, anchor: .bottom)
          }
        }
      }

      Divider().overlay(AgnesTheme.border)
      composer
    }
    .background(AgnesTheme.canvas)
  }

  private var isolatedModeNotice: some View {
    HStack(alignment: .top, spacing: 10) {
      Image(systemName: "square.stack.3d.up.slash")
        .foregroundStyle(.orange)
        .frame(width: 18)
      VStack(alignment: .leading, spacing: 3) {
        Text("Each prompt is an isolated run")
          .font(.system(size: 13, weight: .semibold))
          .foregroundStyle(AgnesTheme.text)
        Text(
          "The desktop has no durable or reconnectable CLI session. Cleanup after this one turn is best-effort, runs do not share context, and a real session lifecycle is tracked in #1345."
        )
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
      }
      Spacer(minLength: 0)
    }
    .padding(12)
    .background(Color.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 10))
    .overlay { RoundedRectangle(cornerRadius: 10).stroke(Color.orange.opacity(0.2)) }
  }

  private var emptyRuns: some View {
    VStack(spacing: 18) {
      VStack(spacing: 7) {
        Image(systemName: "terminal.fill")
          .font(.system(size: 28))
          .foregroundStyle(AgnesTheme.action)
        Text("Run an Agnes agent")
          .font(.title2.bold())
          .foregroundStyle(AgnesTheme.text)
        Text("The answer, tool activity, raw AG-UI events, and budget stay inspectable here.")
          .font(.subheadline)
          .foregroundStyle(AgnesTheme.textSecondary)
          .multilineTextAlignment(.center)
      }

      VStack(spacing: 8) {
        ForEach(suggestions) { suggestion in
          Button {
            model.prompt = suggestion.prompt
          } label: {
            HStack(spacing: 10) {
              Image(systemName: "sparkle")
                .foregroundStyle(AgnesTheme.action)
              Text(suggestion.title)
                .foregroundStyle(AgnesTheme.text)
              Spacer()
              Image(systemName: "arrow.down.left")
                .font(.caption)
                .foregroundStyle(AgnesTheme.textMuted)
            }
            .padding(.horizontal, 13)
            .frame(minHeight: 40)
            .background(AgnesTheme.surface, in: RoundedRectangle(cornerRadius: 9))
            .overlay { RoundedRectangle(cornerRadius: 9).stroke(AgnesTheme.border) }
          }
          .buttonStyle(.plain)
          .disabled(model.isRunningAgent)
        }
      }
      .frame(maxWidth: 520)
    }
    .frame(maxWidth: .infinity)
    .padding(.vertical, 34)
  }

  private var composer: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack(spacing: 10) {
        Label("Agent", systemImage: "person.crop.circle.badge.checkmark")
          .font(.caption.weight(.semibold))
          .foregroundStyle(AgnesTheme.textSecondary)

        TextField("agent slug", text: $model.agentSlug)
          .textFieldStyle(.plain)
          .font(.system(size: 12, design: .monospaced))
          .padding(.horizontal, 9)
          .frame(height: 30)
          .background(AgnesTheme.surfaceMuted, in: RoundedRectangle(cornerRadius: 7))
          .overlay { RoundedRectangle(cornerRadius: 7).stroke(AgnesTheme.border) }
          .disabled(model.isRunningAgent)
          .onSubmit {
            Task { await model.refreshAgentUsage() }
          }

        Text("manual until #1344")
          .font(.caption2.monospaced())
          .foregroundStyle(AgnesTheme.textMuted)
      }

      TextEditor(text: $model.prompt)
        .font(.system(size: 14))
        .scrollContentBackground(.hidden)
        .frame(minHeight: 64, maxHeight: 112)
        .padding(9)
        .background(AgnesTheme.surface)
        .overlay { RoundedRectangle(cornerRadius: 9).stroke(AgnesTheme.border) }
        .disabled(model.isRunningAgent)
        .accessibilityLabel("Agent prompt")

      HStack(spacing: 10) {
        Label("No shell · credentials stay in Agnes CLI", systemImage: "lock.shield")
          .font(.caption)
          .foregroundStyle(AgnesTheme.textMuted)
        Spacer()

        if model.isRunningAgent {
          Button(model.isStoppingAgent ? "Stopping…" : "Stop", systemImage: "stop.fill") {
            model.stopRun()
          }
          .buttonStyle(AgnesSecondaryButtonStyle())
          .disabled(model.isStoppingAgent)
        } else {
          Button("Run", systemImage: "arrow.up") {
            model.submitRun()
          }
          .buttonStyle(AgnesPrimaryButtonStyle())
          .keyboardShortcut(.return, modifiers: [.command])
          .disabled(!model.canRun)
        }
      }
    }
    .padding(14)
    .background(AgnesTheme.surface)
  }

  private var inspectorPane: some View {
    VStack(spacing: 0) {
      Picker("Inspector", selection: $model.runInspectorTab) {
        ForEach(AppModel.RunInspectorTab.allCases) { tab in
          Text(tab.rawValue).tag(tab)
        }
      }
      .pickerStyle(.segmented)
      .labelsHidden()
      .padding(12)

      Divider().overlay(AgnesTheme.border)

      Group {
        switch model.runInspectorTab {
        case .run:
          runInspector
        case .agent:
          agentInspector
        case .events:
          eventsInspector
        }
      }
      .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
    .background(AgnesTheme.surface)
  }

  private var runInspector: some View {
    ScrollView {
      if let run = model.selectedRun {
        VStack(alignment: .leading, spacing: 18) {
          InspectorHeading(title: "Run", subtitle: run.id.uuidString.lowercased())
          InspectorMetric(label: "State", value: run.state.label)
          InspectorMetric(label: "Agent", value: run.agentSlug)
          InspectorMetric(
            label: "Started",
            value: run.startedAt.formatted(date: .omitted, time: .standard)
          )
          InspectorMetric(
            label: "Duration",
            value: run.duration.map { String(format: "%.1f s", $0) } ?? "running"
          )
          InspectorMetric(
            label: "Events",
            value: String(run.result?.events.count ?? 0)
          )
          InspectorMetric(
            label: "Tool calls",
            value: String(run.result?.toolNames.count ?? 0)
          )

          if let result = run.result, !result.notableEvents.isEmpty {
            Divider().overlay(AgnesTheme.border)
            Text("ACTIVITY")
              .font(.caption2.bold())
              .tracking(1)
              .foregroundStyle(AgnesTheme.textMuted)
            ForEach(result.notableEvents) { event in
              InspectorEventRow(event: event)
            }
          }

          if let error = run.errorMessage {
            Text(error)
              .font(.caption)
              .foregroundStyle(.orange)
              .textSelection(.enabled)
          }
        }
        .padding(16)
      } else {
        InspectorEmpty(
          icon: "cursorarrow.click.2",
          title: "Select a run",
          message: "Run state and activity will appear here."
        )
      }
    }
  }

  private var agentInspector: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 16) {
        InspectorHeading(
          title: model.agentSlug.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? "Agent" : model.agentSlug,
          subtitle: "runtime contract"
        )

        usageCard

        VStack(alignment: .leading, spacing: 8) {
          HStack {
            Text("CLI INVOCATION")
              .font(.caption2.bold())
              .tracking(1)
              .foregroundStyle(AgnesTheme.textMuted)
            Spacer()
            Text("read-only")
              .font(.caption2.monospaced())
              .foregroundStyle(AgnesTheme.textMuted)
          }
          CodeBlock(text: model.agentRuntimeContract)
        }

        VStack(alignment: .leading, spacing: 7) {
          Label("CLI gaps", systemImage: "point.3.connected.trianglepath.dotted")
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(AgnesTheme.text)
          Link(
            "#1344 · runnable agent discovery",
            destination: URL(
              string: "https://github.com/keboola/agnes-the-ai-analyst/issues/1344")!
          )
          Link(
            "#1345 · machine-readable sessions",
            destination: URL(
              string: "https://github.com/keboola/agnes-the-ai-analyst/issues/1345")!
          )
        }
        .font(.caption)
      }
      .padding(16)
    }
  }

  @ViewBuilder
  private var usageCard: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack {
        Label("Usage", systemImage: "gauge.with.dots.needle.33percent")
          .font(.subheadline.weight(.semibold))
        Spacer()
        Button {
          Task { await model.refreshAgentUsage() }
        } label: {
          Image(systemName: "arrow.clockwise")
        }
        .buttonStyle(.plain)
        .disabled(model.isLoadingAgentUsage || model.agentSlug.isEmpty)
        .accessibilityLabel("Refresh agent usage")
      }

      if model.isLoadingAgentUsage {
        ProgressView("Loading usage…")
          .controlSize(.small)
      } else if let usage = model.agentUsage {
        InspectorMetric(label: usage.period, value: "\(usage.totalTokens.formatted()) tokens")
        if let limit = usage.budgetLimit, limit > 0 {
          let used = min(max(limit - (usage.budgetRemaining ?? 0), 0), limit)
          ProgressView(value: Double(used), total: Double(limit))
          Text("\(used.formatted()) of \(limit.formatted())")
            .font(.caption2.monospacedDigit())
            .foregroundStyle(AgnesTheme.textMuted)
        } else {
          Text("No agent budget limit")
            .font(.caption)
            .foregroundStyle(AgnesTheme.textMuted)
        }
      } else if let error = model.agentUsageError {
        Text(error)
          .font(.caption)
          .foregroundStyle(.orange)
          .textSelection(.enabled)
      } else {
        Text("Enter an agent slug, then refresh to load its CLI usage contract.")
          .font(.caption)
          .foregroundStyle(AgnesTheme.textMuted)
      }
    }
    .padding(12)
    .background(AgnesTheme.surfaceMuted, in: RoundedRectangle(cornerRadius: 10))
    .overlay { RoundedRectangle(cornerRadius: 10).stroke(AgnesTheme.border) }
  }

  private var eventsInspector: some View {
    Group {
      if let run = model.selectedRun, let result = run.result {
        VStack(alignment: .leading, spacing: 10) {
          InspectorHeading(
            title: "AG-UI events",
            subtitle: "\(result.events.count) events · untrusted CLI output"
          )
          CodeBlock(text: result.rawEventsJSON)
        }
        .padding(16)
      } else {
        InspectorEmpty(
          icon: "curlybraces.square",
          title: "No events yet",
          message: "Select a finished run to inspect its raw JSON event array."
        )
      }
    }
  }

  private var statusBar: some View {
    HStack(spacing: 9) {
      Circle()
        .fill(cliStatusColor)
        .frame(width: 7, height: 7)
      Text(model.cliVersion ?? "CLI unavailable")
        .lineLimit(1)
      Divider().frame(height: 14)
      Text(model.agentSlug.isEmpty ? "no agent" : "@\(model.agentSlug)")
        .font(.caption.monospaced())
      Divider().frame(height: 14)
      Text("isolated one-shot")
        .font(.caption.monospaced())
      Spacer()
      if let active = model.activeRun {
        ProgressView()
          .controlSize(.mini)
        Text(model.isStoppingAgent ? "stopping" : "running \(active.id.uuidString.prefix(8))")
          .font(.caption.monospaced())
      } else {
        Text("ready")
          .font(.caption.monospaced())
      }
    }
    .font(.caption)
    .foregroundStyle(AgnesTheme.textMuted)
    .padding(.horizontal, 14)
    .frame(height: 30)
    .background(AgnesTheme.surfaceMuted)
  }

  private var cliStatusColor: Color {
    switch model.cliStatus {
    case .ready: .green
    case .checking: AgnesTheme.action
    case .unavailable: .orange
    case .idle: AgnesTheme.textMuted
    }
  }
}

private struct AgentRunCard: View {
  let run: AgentRunRecord
  let selected: Bool
  let onSelect: () -> Void

  var body: some View {
    VStack(alignment: .leading, spacing: 14) {
      HStack(spacing: 9) {
        Text(run.agentSlug)
          .font(.caption.monospaced().weight(.semibold))
          .foregroundStyle(AgnesTheme.textSecondary)
        Text(run.state.label)
          .font(.caption2.weight(.semibold))
          .foregroundStyle(stateColor)
          .padding(.horizontal, 7)
          .padding(.vertical, 3)
          .background(stateColor.opacity(0.09), in: Capsule())
        Spacer()
        Text(run.startedAt.formatted(date: .omitted, time: .shortened))
          .font(.caption2.monospacedDigit())
          .foregroundStyle(AgnesTheme.textMuted)
      }

      VStack(alignment: .leading, spacing: 5) {
        Text("YOU")
          .font(.caption2.bold())
          .tracking(1)
          .foregroundStyle(AgnesTheme.textMuted)
        Text(run.prompt)
          .font(.system(size: 14))
          .foregroundStyle(AgnesTheme.text)
          .textSelection(.enabled)
      }

      Divider().overlay(AgnesTheme.border)

      if run.state == .running {
        HStack(spacing: 9) {
          ProgressView().controlSize(.small)
          VStack(alignment: .leading, spacing: 2) {
            Text("Agnes CLI is running")
              .font(.subheadline.weight(.semibold))
            Text("The current CLI returns structured events when this process exits.")
              .font(.caption)
              .foregroundStyle(AgnesTheme.textMuted)
          }
        }
      } else if let result = run.result {
        VStack(alignment: .leading, spacing: 8) {
          Text("AGNES")
            .font(.caption2.bold())
            .tracking(1)
            .foregroundStyle(AgnesTheme.action)
          if result.answer.isEmpty {
            Text("(No answer returned.)")
              .foregroundStyle(AgnesTheme.textMuted)
          } else {
            AgentMarkdownView(markdown: result.answer)
          }
        }
        .font(.system(size: 14))

        if !result.toolNames.isEmpty {
          HStack(spacing: 7) {
            Image(systemName: "wrench.and.screwdriver")
              .foregroundStyle(AgnesTheme.textMuted)
            ForEach(Array(result.toolNames.enumerated()), id: \.offset) { _, name in
              Text(name)
                .font(.caption2.monospaced())
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(AgnesTheme.surfaceMuted, in: Capsule())
            }
          }
        }
      }

      if let error = run.errorMessage {
        Label(error, systemImage: "exclamationmark.triangle.fill")
          .font(.caption)
          .foregroundStyle(.orange)
          .textSelection(.enabled)
      }
    }
    .frame(maxWidth: .infinity, alignment: .leading)
    .padding(16)
    .background(AgnesTheme.surface, in: RoundedRectangle(cornerRadius: 12))
    .overlay {
      RoundedRectangle(cornerRadius: 12)
        .stroke(selected ? AgnesTheme.action : AgnesTheme.border, lineWidth: selected ? 1.5 : 1)
    }
    .contentShape(Rectangle())
    .onTapGesture(perform: onSelect)
    .accessibilityAddTraits(selected ? .isSelected : [])
  }

  private var stateColor: Color {
    switch run.state {
    case .running: AgnesTheme.action
    case .completed: .green
    case .failed: .red
    case .truncated, .stopped: .orange
    }
  }
}

private struct InspectorHeading: View {
  let title: String
  let subtitle: String

  var body: some View {
    VStack(alignment: .leading, spacing: 3) {
      Text(title)
        .font(.headline)
        .foregroundStyle(AgnesTheme.text)
      Text(subtitle)
        .font(.caption2.monospaced())
        .foregroundStyle(AgnesTheme.textMuted)
        .textSelection(.enabled)
    }
    .frame(maxWidth: .infinity, alignment: .leading)
  }
}

private struct InspectorMetric: View {
  let label: String
  let value: String

  var body: some View {
    HStack(alignment: .firstTextBaseline) {
      Text(label)
        .foregroundStyle(AgnesTheme.textMuted)
      Spacer()
      Text(value)
        .font(.caption.monospaced())
        .foregroundStyle(AgnesTheme.textSecondary)
        .multilineTextAlignment(.trailing)
        .textSelection(.enabled)
    }
    .font(.caption)
  }
}

private struct InspectorEventRow: View {
  let event: AgentRunEvent

  var body: some View {
    HStack(alignment: .top, spacing: 9) {
      Image(systemName: icon)
        .foregroundStyle(color)
        .frame(width: 16)
      VStack(alignment: .leading, spacing: 2) {
        Text(event.title)
          .font(.caption.weight(.semibold))
          .foregroundStyle(AgnesTheme.text)
        if let detail = event.detail {
          Text(detail)
            .font(.caption2.monospaced())
            .foregroundStyle(AgnesTheme.textMuted)
            .lineLimit(3)
            .textSelection(.enabled)
        }
      }
      Spacer(minLength: 0)
      Text("#\(event.sequence)")
        .font(.caption2.monospacedDigit())
        .foregroundStyle(AgnesTheme.textMuted)
    }
  }

  private var icon: String {
    switch event.type {
    case "TOOL_CALL_START": "wrench.and.screwdriver"
    case "TOOL_CALL_END": "checkmark.circle"
    case "RUN_ERROR": "exclamationmark.triangle"
    case "RUN_FINISHED": "flag.checkered"
    default: "circle.fill"
    }
  }

  private var color: Color {
    switch event.type {
    case "RUN_ERROR": .red
    case "RUN_FINISHED", "TOOL_CALL_END": .green
    case "TOOL_CALL_START": AgnesTheme.action
    default: AgnesTheme.textMuted
    }
  }
}

private struct CodeBlock: View {
  let text: String

  var body: some View {
    ScrollView([.horizontal, .vertical]) {
      Text(text)
        .font(.system(size: 11, design: .monospaced))
        .foregroundStyle(Color(red: 203 / 255, green: 213 / 255, blue: 225 / 255))
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
    }
    .background(Color(red: 15 / 255, green: 23 / 255, blue: 42 / 255))
    .clipShape(RoundedRectangle(cornerRadius: 9))
    .overlay { RoundedRectangle(cornerRadius: 9).stroke(Color.white.opacity(0.08)) }
  }
}

private struct InspectorEmpty: View {
  let icon: String
  let title: String
  let message: String

  var body: some View {
    VStack(spacing: 9) {
      Image(systemName: icon)
        .font(.system(size: 25))
        .foregroundStyle(AgnesTheme.textMuted)
      Text(title)
        .font(.headline)
        .foregroundStyle(AgnesTheme.text)
      Text(message)
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
        .multilineTextAlignment(.center)
    }
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .padding(24)
  }
}

private struct AskSuggestion: Identifiable {
  let title: String
  let prompt: String
  var id: String { title }
}
