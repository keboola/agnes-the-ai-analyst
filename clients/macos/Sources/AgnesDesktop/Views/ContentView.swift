import SwiftUI

struct ContentView: View {
  @ObservedObject var model: AppModel

  var body: some View {
    NavigationSplitView {
      AgnesSidebar(model: model)
        .navigationSplitViewColumnWidth(min: 210, ideal: 232, max: 260)
    } detail: {
      Group {
        switch model.selectedDestination {
        case .marketplace:
          MarketplaceView(model: model)
        case .runs:
          ChatWorkspaceView(model: model)
        case .settings:
          SettingsView(model: model)
        }
      }
      .frame(maxWidth: .infinity, maxHeight: .infinity)
      .background(AgnesTheme.canvas)
    }
    .tint(AgnesTheme.action)
    .task {
      await model.bootstrap()
    }
  }
}

private struct AgnesSidebar: View {
  @ObservedObject var model: AppModel

  var body: some View {
    VStack(alignment: .leading, spacing: 0) {
      HStack(spacing: 10) {
        AgnesTheme.mark
          .resizable()
          .scaledToFit()
          .frame(width: 32, height: 32)
        Text("Agnes")
          .font(.system(size: 18, weight: .bold))
          .foregroundStyle(AgnesTheme.text)
      }
      .padding(.horizontal, 16)
      .padding(.top, 14)
      .padding(.bottom, 18)

      VStack(spacing: 4) {
        destinationButton(.marketplace)
        destinationButton(.runs)
      }
      .padding(.horizontal, 10)

      Spacer()

      Divider()
        .overlay(AgnesTheme.border)
        .padding(.horizontal, 12)

      Button {
        model.selectedDestination = .settings
      } label: {
        HStack(spacing: 11) {
          Image(systemName: "gearshape")
            .frame(width: 18)
          VStack(alignment: .leading, spacing: 2) {
            Text("Settings")
              .font(.system(size: 14, weight: .medium))
            CLICompactStatus(status: model.cliStatus)
          }
          Spacer(minLength: 0)
        }
        .foregroundStyle(AgnesTheme.text)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(
          model.selectedDestination == .settings ? AgnesTheme.actionTint : .clear,
          in: RoundedRectangle(cornerRadius: 9)
        )
      }
      .buttonStyle(.plain)
      .accessibilityLabel("Settings and Agnes CLI status")
      .padding(10)
    }
    .background(AgnesTheme.surface)
  }

  private func destinationButton(_ destination: AppModel.Destination) -> some View {
    Button {
      model.selectedDestination = destination
    } label: {
      HStack(spacing: 11) {
        Image(systemName: destination.systemImage)
          .frame(width: 18)
        Text(destination.title)
          .font(.system(size: 14, weight: .medium))
        Spacer()
        if destination == .marketplace, model.marketplaceTotal > 0 {
          Text("\(model.marketplaceTotal)")
            .font(.caption.monospacedDigit())
            .foregroundStyle(AgnesTheme.textMuted)
        } else if destination == .runs, !model.runs.isEmpty {
          Text("\(model.runs.count)")
            .font(.caption.monospacedDigit())
            .foregroundStyle(AgnesTheme.textMuted)
        }
      }
      .foregroundStyle(AgnesTheme.text)
      .padding(.horizontal, 12)
      .padding(.vertical, 10)
      .background(
        model.selectedDestination == destination ? AgnesTheme.actionTint : .clear,
        in: RoundedRectangle(cornerRadius: 9)
      )
    }
    .buttonStyle(.plain)
  }
}

private struct CLICompactStatus: View {
  let status: AppModel.CLIStatus

  var body: some View {
    HStack(spacing: 5) {
      Circle()
        .fill(indicatorColor)
        .frame(width: 7, height: 7)
      Text(label)
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
        .lineLimit(1)
    }
  }

  private var indicatorColor: Color {
    switch status {
    case .ready: .green
    case .checking: AgnesTheme.action
    case .unavailable: .orange
    case .idle: AgnesTheme.textMuted
    }
  }

  private var label: String {
    switch status {
    case .ready(let version): version
    case .checking: "Checking CLI…"
    case .unavailable: "CLI needs attention"
    case .idle: "CLI not checked"
    }
  }
}
