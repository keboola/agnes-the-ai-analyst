import AppKit
import SwiftUI

struct SettingsView: View {
  @ObservedObject var model: AppModel

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 22) {
        VStack(alignment: .leading, spacing: 6) {
          Text("Settings")
            .font(.system(size: 27, weight: .bold))
            .foregroundStyle(AgnesTheme.text)
          Text("Connect the desktop app to the Agnes CLI already configured on this Mac.")
            .font(.system(size: 14))
            .foregroundStyle(AgnesTheme.textSecondary)
        }

        cliCard
        authenticationCard
        commandCard
      }
      .frame(maxWidth: 900, alignment: .leading)
      .padding(28)
      .frame(maxWidth: .infinity, alignment: .topLeading)
    }
    .background(AgnesTheme.canvas)
  }

  private var cliCard: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(alignment: .top) {
        VStack(alignment: .leading, spacing: 4) {
          Label("Agnes CLI", systemImage: "terminal")
            .font(.headline)
            .foregroundStyle(AgnesTheme.text)
          Text("Leave the path empty to search the standard shell locations automatically.")
            .font(.caption)
            .foregroundStyle(AgnesTheme.textMuted)
        }
        Spacer()
        CLIStatusBadge(status: model.cliStatus)
      }

      HStack(spacing: 9) {
        TextField("Auto-detect Agnes CLI", text: $model.preferredExecutablePath)
          .textFieldStyle(.plain)
          .font(.system(size: 13, design: .monospaced))
          .padding(.horizontal, 12)
          .frame(height: 40)
          .background(AgnesTheme.surfaceMuted, in: RoundedRectangle(cornerRadius: 9))
          .overlay {
            RoundedRectangle(cornerRadius: 9)
              .stroke(AgnesTheme.border, lineWidth: 1)
          }
          .disabled(model.hasActiveCLICommand)

        Button("Choose…") {
          chooseExecutable()
        }
        .buttonStyle(AgnesSecondaryButtonStyle())
        .disabled(model.hasActiveCLICommand)

        Button("Check again") {
          Task { await model.refreshMarketplace() }
        }
        .buttonStyle(AgnesPrimaryButtonStyle())
        .disabled(model.hasActiveCLICommand)
      }

      if let path = model.resolvedExecutablePath {
        Label(path, systemImage: "checkmark.circle.fill")
          .font(.caption.monospaced())
          .foregroundStyle(.green)
          .textSelection(.enabled)
      }

      if case .unavailable(let message) = model.cliStatus {
        HStack(alignment: .top, spacing: 9) {
          Image(systemName: "exclamationmark.triangle.fill")
            .foregroundStyle(.orange)
          Text(message)
            .font(.caption)
            .foregroundStyle(AgnesTheme.textSecondary)
            .textSelection(.enabled)
        }
      }
    }
    .agnesCard(padding: 20)
  }

  private var authenticationCard: some View {
    VStack(alignment: .leading, spacing: 13) {
      Label("Authentication stays with Agnes CLI", systemImage: "lock.shield")
        .font(.headline)
        .foregroundStyle(AgnesTheme.text)

      Text(
        "The desktop app never reads or stores your Agnes token. It launches the CLI, which uses the session created by agnes auth login and its configuration under ~/.config/agnes."
      )
      .font(.system(size: 14))
      .foregroundStyle(AgnesTheme.textSecondary)
      .fixedSize(horizontal: false, vertical: true)

      Divider()
        .overlay(AgnesTheme.border)

      HStack(alignment: .top, spacing: 11) {
        Image(systemName: "person.crop.circle.badge.exclamationmark")
          .foregroundStyle(AgnesTheme.action)
        Text(
          "Agent-profile discovery is intentionally web-only for PAT sessions. That is why Ask uses a manual agent slug instead of running agnes agent list. Marketplace browsing is fully available through the CLI."
        )
        .font(.caption)
        .foregroundStyle(AgnesTheme.textSecondary)
        .fixedSize(horizontal: false, vertical: true)
      }
    }
    .agnesCard(padding: 20)
  }

  private var commandCard: some View {
    VStack(alignment: .leading, spacing: 12) {
      Label("MVP command surface", systemImage: "chevron.left.forwardslash.chevron.right")
        .font(.headline)
        .foregroundStyle(AgnesTheme.text)
      Text("These are the only Agnes operations this build invokes.")
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)

      VStack(alignment: .leading, spacing: 8) {
        command("agnes marketplace search --limit 48 --json")
        command("agnes marketplace detail --json <item-id>")
        command("agnes my-stack show --json")
        command("agnes marketplace add <item-id>")
        command("agnes marketplace remove <item-id>")
        command("agnes chat --agent <slug> --once <prompt> --json")
      }
    }
    .agnesCard(padding: 20)
  }

  private func command(_ value: String) -> some View {
    Text(value)
      .font(.system(size: 12, design: .monospaced))
      .foregroundStyle(AgnesTheme.textSecondary)
      .textSelection(.enabled)
      .padding(.horizontal, 11)
      .padding(.vertical, 8)
      .frame(maxWidth: .infinity, alignment: .leading)
      .background(AgnesTheme.surfaceMuted, in: RoundedRectangle(cornerRadius: 7))
  }

  private func chooseExecutable() {
    let panel = NSOpenPanel()
    panel.title = "Choose Agnes CLI"
    panel.prompt = "Choose"
    panel.canChooseDirectories = false
    panel.canChooseFiles = true
    panel.allowsMultipleSelection = false

    if panel.runModal() == .OK, let url = panel.url {
      model.preferredExecutablePath = url.path
    }
  }
}

private struct CLIStatusBadge: View {
  let status: AppModel.CLIStatus

  var body: some View {
    HStack(spacing: 6) {
      if case .checking = status {
        ProgressView()
          .controlSize(.mini)
      } else {
        Circle()
          .fill(color)
          .frame(width: 7, height: 7)
      }
      Text(label)
        .font(.caption.weight(.semibold))
        .lineLimit(1)
    }
    .foregroundStyle(AgnesTheme.textSecondary)
    .padding(.horizontal, 9)
    .padding(.vertical, 5)
    .background(AgnesTheme.surfaceMuted, in: Capsule())
    .overlay {
      Capsule().stroke(AgnesTheme.border, lineWidth: 1)
    }
  }

  private var color: Color {
    switch status {
    case .ready: .green
    case .unavailable: .orange
    case .checking: AgnesTheme.action
    case .idle: AgnesTheme.textMuted
    }
  }

  private var label: String {
    switch status {
    case .idle: "Not checked"
    case .checking: "Checking…"
    case .ready(let version): "Ready · \(version)"
    case .unavailable: "Needs attention"
    }
  }
}
