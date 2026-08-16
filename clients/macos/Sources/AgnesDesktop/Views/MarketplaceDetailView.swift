import AppKit
import SwiftUI

struct MarketplaceDetailSheet: View {
  @ObservedObject var model: AppModel
  let initialItem: MarketplaceItem

  @Environment(\.dismiss) private var dismiss
  @State private var requestedInstalledState: Bool?

  private var item: MarketplaceItem {
    model.presentedMarketplaceItem ?? initialItem
  }

  private var detail: MarketplaceDetail? {
    model.marketplaceDetail
  }

  private var actionOutcome: AppModel.MarketplaceActionOutcome? {
    guard
      let outcome = model.marketplaceActionOutcome,
      outcome.itemID == item.id
    else { return nil }
    return outcome
  }

  private var isInstalled: Bool {
    actionOutcome?.installed ?? detail?.installed ?? item.installed
  }

  var body: some View {
    VStack(spacing: 0) {
      detailHeader
      Divider().overlay(AgnesTheme.border)

      ScrollView {
        if model.isLoadingMarketplaceDetail, detail == nil {
          VStack(spacing: 14) {
            ProgressView()
              .controlSize(.large)
            Text("Loading details…")
              .foregroundStyle(AgnesTheme.textSecondary)
          }
          .frame(maxWidth: .infinity, minHeight: 420)
        } else if let error = model.marketplaceDetailError, detail == nil {
          VStack(spacing: 16) {
            ContentUnavailableView(
              "Details unavailable",
              systemImage: "exclamationmark.triangle",
              description: Text(error)
            )
            Button("Retry") {
              Task { await model.loadPresentedMarketplaceDetail() }
            }
            .buttonStyle(AgnesPrimaryButtonStyle())
          }
          .frame(maxWidth: .infinity, minHeight: 420)
        } else {
          detailContent
        }
      }
      .background(AgnesTheme.canvas)
    }
    .confirmationDialog(
      requestedInstalledState == true
        ? "Add “\(title)” to My Stack?" : "Remove “\(title)” from My Stack?",
      isPresented: Binding(
        get: { requestedInstalledState != nil },
        set: { isPresented in
          if !isPresented { requestedInstalledState = nil }
        }
      )
    ) {
      if let requestedInstalledState {
        Button(
          requestedInstalledState ? "Add to My Stack" : "Remove from My Stack",
          role: requestedInstalledState ? nil : .destructive
        ) {
          self.requestedInstalledState = nil
          Task {
            await model.setPresentedMarketplaceItemInstalled(requestedInstalledState)
          }
        }
      }
      Button("Cancel", role: .cancel) {
        requestedInstalledState = nil
      }
    } message: {
      Text(
        requestedInstalledState == true
          ? "Agnes CLI will add “\(title)” to your stack. Credentials stay with the CLI."
          : "Agnes CLI will remove “\(title)” from your stack."
      )
    }
  }

  private var detailHeader: some View {
    HStack(alignment: .center, spacing: 14) {
      Image(systemName: iconName)
        .font(.system(size: 22, weight: .semibold))
        .foregroundStyle(typeColor)
        .frame(width: 46, height: 46)
        .background(typeColor.opacity(0.1), in: RoundedRectangle(cornerRadius: 11))

      VStack(alignment: .leading, spacing: 5) {
        HStack(spacing: 7) {
          Text(title)
            .font(.system(size: 24, weight: .bold))
            .foregroundStyle(AgnesTheme.text)
          Text(item.type.rawValue.uppercased())
            .font(.system(size: 10, weight: .bold))
            .foregroundStyle(typeColor)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(typeColor.opacity(0.09), in: Capsule())
        }
        HStack(spacing: 8) {
          if let category = (detail?.category ?? item.category)?.nonEmpty {
            Text(category)
          }
          if let publisher = publisher.nonEmpty {
            Text(publisher)
          }
          if let version = (detail?.version ?? item.version)?.nonEmpty {
            Text("v\(version)")
          }
        }
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
      }

      Spacer()

      if model.isChangingMarketplaceStack {
        ProgressView()
          .controlSize(.small)
      }

      stackActionButton

      Button {
        model.dismissMarketplaceDetail()
        dismiss()
      } label: {
        Image(systemName: "xmark")
      }
      .buttonStyle(.borderless)
      .accessibilityLabel("Close Marketplace details")
      .disabled(model.isChangingMarketplaceStack)
    }
    .padding(22)
    .background(AgnesTheme.surface)
  }

  @ViewBuilder
  private var stackActionButton: some View {
    if isInstalled {
      Button("Remove from My Stack") {
        requestedInstalledState = false
      }
      .buttonStyle(AgnesSecondaryButtonStyle())
      .disabled(model.hasActiveCLICommand || !canChangeStack)
    } else {
      Button("+ Add to My Stack") {
        requestedInstalledState = true
      }
      .buttonStyle(AgnesPrimaryButtonStyle())
      .disabled(model.hasActiveCLICommand || !canChangeStack)
    }
  }

  private var detailContent: some View {
    HStack(alignment: .top, spacing: 18) {
      VStack(alignment: .leading, spacing: 16) {
        if let actionOutcome {
          marketplaceActionOutcome(actionOutcome)
        }

        VStack(alignment: .leading, spacing: 12) {
          Label("What it does", systemImage: "info.circle")
            .font(.headline)
            .foregroundStyle(AgnesTheme.text)
          Text(descriptionText)
            .font(.system(size: 14))
            .foregroundStyle(AgnesTheme.textSecondary)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .agnesCard(padding: 20)

        if let detail {
          if !detail.useCases.isEmpty {
            detailSection(
              "Use cases", icon: "lightbulb",
              entries: detail.useCases.map { entry in
                (entry.title, entry.description ?? entry.prompt)
              })
          }

          if hasInternalStructure(detail) {
            VStack(alignment: .leading, spacing: 18) {
              Label("Internal structure", systemImage: "square.stack.3d.up")
                .font(.headline)
                .foregroundStyle(AgnesTheme.text)
              detailEntries("Skills", entries: detail.skills)
              detailEntries("Agents", entries: detail.agents)
              detailEntries("Commands", entries: detail.commands)
              detailEntries("MCP servers", entries: detail.mcps)
            }
            .agnesCard(padding: 20)
          }
        }
      }
      .frame(maxWidth: .infinity, alignment: .top)

      VStack(alignment: .leading, spacing: 14) {
        Text("AVAILABILITY")
          .font(.system(size: 11, weight: .bold))
          .tracking(1.1)
          .foregroundStyle(AgnesTheme.textMuted)

        Label(
          isInstalled ? "In your stack" : "Not in your stack",
          systemImage: isInstalled ? "checkmark.circle.fill" : "circle"
        )
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(isInstalled ? .green : .orange)

        Text(
          isInstalled
            ? "This item is available through your Agnes stack."
            : "Add it to your stack to make it available to supported AI tools."
        )
        .font(.caption)
        .foregroundStyle(AgnesTheme.textSecondary)

        Divider().overlay(AgnesTheme.border)

        metadataRow("Source", item.source.rawValue.capitalized)
        if item.stackCount > 0 {
          metadataRow("Installed by", "\(item.stackCount) users")
        }
        if let owner = item.owner?.nonEmpty {
          metadataRow("Owner", owner)
        }

        if let error = model.marketplaceDetailError {
          Divider().overlay(AgnesTheme.border)
          Text(error)
            .font(.caption)
            .foregroundStyle(.red)
            .textSelection(.enabled)
        }
      }
      .frame(width: 220, alignment: .topLeading)
      .agnesCard(padding: 18)
    }
    .padding(22)
  }

  private func marketplaceActionOutcome(_ outcome: AppModel.MarketplaceActionOutcome) -> some View {
    VStack(alignment: .leading, spacing: 12) {
      Label(
        isInstalled ? "Added to My Stack" : "My Stack updated",
        systemImage: "checkmark.circle.fill"
      )
      .font(.headline)
      .foregroundStyle(.green)

      Text(outcome.message)
        .font(.subheadline)
        .foregroundStyle(AgnesTheme.textSecondary)
        .textSelection(.enabled)

      Divider().overlay(AgnesTheme.border)

      Text("Activate this change in Claude Code by running:")
        .font(.caption)
        .foregroundStyle(AgnesTheme.textSecondary)

      HStack(spacing: 10) {
        Text("/update-agnes-plugins")
          .font(.system(size: 13, weight: .semibold, design: .monospaced))
          .foregroundStyle(AgnesTheme.text)
          .textSelection(.enabled)
          .padding(.horizontal, 10)
          .padding(.vertical, 8)
          .background(AgnesTheme.canvas, in: RoundedRectangle(cornerRadius: 7))
          .overlay {
            RoundedRectangle(cornerRadius: 7)
              .stroke(AgnesTheme.border, lineWidth: 1)
          }

        Button("Copy Command") {
          copyActivationCommand()
        }
        .buttonStyle(AgnesSecondaryButtonStyle())
        .accessibilityHint("Copies the Claude Code activation command")

        Spacer()

        if isInstalled {
          Button("View My Stack") {
            model.dismissMarketplaceDetail()
            dismiss()
            Task { await model.selectMarketplaceShelf(.stack) }
          }
          .buttonStyle(AgnesPrimaryButtonStyle())
        }
      }

      Text("This app does not activate Marketplace items automatically.")
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
    }
    .agnesCard(padding: 18)
  }

  private func copyActivationCommand() {
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    pasteboard.setString("/update-agnes-plugins", forType: .string)
  }

  @ViewBuilder
  private func detailEntries(_ heading: String, entries: [MarketplaceDetailEntry]) -> some View {
    if !entries.isEmpty {
      VStack(alignment: .leading, spacing: 9) {
        HStack {
          Text(heading.uppercased())
            .font(.system(size: 11, weight: .bold))
            .foregroundStyle(AgnesTheme.textMuted)
          Spacer()
          Text("\(entries.count)")
            .font(.caption.monospacedDigit())
            .foregroundStyle(AgnesTheme.textMuted)
        }
        ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
          HStack(alignment: .top, spacing: 12) {
            Text(entry.name)
              .font(
                .system(
                  size: 13, weight: .semibold,
                  design: heading == "Commands" ? .monospaced : .default)
              )
              .foregroundStyle(heading == "Commands" ? AgnesTheme.action : AgnesTheme.text)
              .frame(width: 145, alignment: .leading)
              .textSelection(.enabled)
            if let description = entry.description?.nonEmpty {
              Text(description)
                .font(.caption)
                .foregroundStyle(AgnesTheme.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
          }
          .padding(.vertical, 5)
        }
      }
    }
  }

  private func detailSection(
    _ heading: String,
    icon: String,
    entries: [(String, String?)]
  ) -> some View {
    VStack(alignment: .leading, spacing: 12) {
      Label(heading, systemImage: icon)
        .font(.headline)
      ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
        VStack(alignment: .leading, spacing: 3) {
          Text(entry.0)
            .font(.subheadline.weight(.semibold))
          if let description = entry.1?.nonEmpty {
            Text(description)
              .font(.caption)
              .foregroundStyle(AgnesTheme.textSecondary)
          }
        }
      }
    }
    .agnesCard(padding: 20)
  }

  private func metadataRow(_ label: String, _ value: String) -> some View {
    VStack(alignment: .leading, spacing: 2) {
      Text(label)
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
      Text(value)
        .font(.caption.weight(.semibold))
        .foregroundStyle(AgnesTheme.text)
        .textSelection(.enabled)
    }
  }

  private func hasInternalStructure(_ detail: MarketplaceDetail) -> Bool {
    !detail.skills.isEmpty || !detail.agents.isEmpty || !detail.commands.isEmpty
      || !detail.mcps.isEmpty
  }

  private var title: String {
    detail?.displayName?.nonEmpty ?? item.displayName?.nonEmpty ?? item.name
  }

  private var publisher: String {
    detail?.marketplaceName?.nonEmpty
      ?? item.marketplaceName?.nonEmpty
      ?? detail?.ownerDisplay?.nonEmpty
      ?? detail?.authorName?.nonEmpty
      ?? item.owner?.nonEmpty
      ?? item.source.rawValue.capitalized
  }

  private var descriptionText: String {
    detail?.description?.nonEmpty
      ?? detail?.tagline?.nonEmpty
      ?? item.description?.nonEmpty
      ?? item.tagline?.nonEmpty
      ?? "No description provided."
  }

  private var canChangeStack: Bool {
    guard let detail else { return false }
    if isInstalled {
      return !detail.isSystem && !detail.isRequired
    }
    return detail.installable
  }

  private var iconName: String {
    switch item.type {
    case .plugin: "puzzlepiece.extension.fill"
    case .skill: "bolt.fill"
    case .agent: "person.crop.circle.fill"
    case .unknown: "shippingbox.fill"
    }
  }

  private var typeColor: Color {
    switch item.type {
    case .plugin: AgnesTheme.action
    case .skill: .purple
    case .agent: .orange
    case .unknown: AgnesTheme.textSecondary
    }
  }
}
