import SwiftUI

struct MarketplaceView: View {
  @ObservedObject var model: AppModel

  private let columns = [
    GridItem(.adaptive(minimum: 255, maximum: 360), spacing: 16, alignment: .top)
  ]

  var body: some View {
    VStack(spacing: 0) {
      ScrollView {
        VStack(alignment: .leading, spacing: 22) {
          MarketplaceHero(model: model)
          shelfAndFilters

          if let error = model.marketplaceError {
            MarketplaceErrorBanner(message: error) {
              Task { await model.refreshMarketplace() }
            }
          }

          if let message = model.marketplaceActionMessage {
            MarketplaceSuccessBanner(message: message) {
              model.clearMarketplaceNotice()
            }
          }

          catalog
        }
        .frame(maxWidth: 1220, alignment: .leading)
        .padding(28)
        .frame(maxWidth: .infinity)
      }
    }
    .background(AgnesTheme.canvas)
    .toolbar {
      ToolbarItem(placement: .primaryAction) {
        Button {
          Task { await model.refreshMarketplace() }
        } label: {
          Label(
            model.marketplaceShelf == .stack ? "Refresh My Stack" : "Refresh Marketplace",
            systemImage: "arrow.clockwise"
          )
        }
        .disabled(model.hasActiveCLICommand)
      }
    }
    .sheet(
      isPresented: Binding(
        get: { model.presentedMarketplaceItem != nil },
        set: { isPresented in
          if !isPresented {
            model.dismissMarketplaceDetail()
          }
        }
      )
    ) {
      if let item = model.presentedMarketplaceItem {
        MarketplaceDetailSheet(model: model, initialItem: item)
          .frame(minWidth: 760, idealWidth: 860, minHeight: 620, idealHeight: 720)
      }
    }
  }

  private var shelfAndFilters: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(alignment: .center) {
        AgnesShelfPicker(model: model)
        Spacer()

        if model.marketplaceShelf == .browse {
          Picker("Source", selection: $model.marketplaceSourceFilter) {
            ForEach(AppModel.MarketplaceSourceFilter.allCases) { source in
              Text(source.title).tag(source)
            }
          }
          .labelsHidden()
          .frame(width: 145)
          .disabled(model.hasActiveCLICommand)
          .onChange(of: model.marketplaceSourceFilter) { _, _ in
            Task { await model.refreshMarketplace() }
          }

          Picker("Sort by", selection: $model.marketplaceSort) {
            ForEach(MarketplaceSort.allCases, id: \.self) { sort in
              Text(sort.displayName).tag(sort)
            }
          }
          .labelsHidden()
          .frame(width: 140)
          .disabled(model.hasActiveCLICommand)
          .onChange(of: model.marketplaceSort) { _, _ in
            Task { await model.refreshMarketplace() }
          }
        } else {
          Label("Items active in your Agnes stack", systemImage: "checkmark.circle")
            .font(.subheadline)
            .foregroundStyle(AgnesTheme.textSecondary)

          Button("Refresh My Stack") {
            Task { await model.refreshMarketplace() }
          }
          .buttonStyle(AgnesSecondaryButtonStyle())
          .disabled(model.hasActiveCLICommand)
        }
      }

      if model.marketplaceShelf == .browse {
        HStack(spacing: 8) {
          ForEach(AppModel.MarketplaceTypeFilter.allCases) { filter in
            Button(filter.title) {
              model.marketplaceTypeFilter = filter
              Task { await model.refreshMarketplace() }
            }
            .buttonStyle(
              MarketplaceChipButtonStyle(isSelected: model.marketplaceTypeFilter == filter)
            )
            .disabled(model.hasActiveCLICommand)
          }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Marketplace item type")
      }
    }
  }

  @ViewBuilder
  private var catalog: some View {
    let items = model.visibleMarketplaceItems

    if model.isRefreshingMarketplace, items.isEmpty {
      VStack(spacing: 14) {
        ProgressView()
          .controlSize(.large)
        Text(model.marketplaceShelf == .stack ? "Loading My Stack…" : "Loading Marketplace…")
          .foregroundStyle(AgnesTheme.textSecondary)
      }
      .frame(maxWidth: .infinity, minHeight: 280)
    } else if items.isEmpty, model.marketplaceError == nil {
      ContentUnavailableView(
        model.marketplaceShelf == .stack
          ? "Your stack is empty" : "No Marketplace results",
        systemImage: model.marketplaceShelf == .stack
          ? "square.stack.3d.up.slash" : "magnifyingglass",
        description: Text(
          model.marketplaceShelf == .stack
            ? "Add an item from Browse to make it appear here."
            : "Try a different search or filter."
        )
      )
      .frame(maxWidth: .infinity, minHeight: 280)
    } else {
      HStack(alignment: .firstTextBaseline) {
        VStack(alignment: .leading, spacing: 2) {
          Text(model.marketplaceShelf == .stack ? "My Stack" : "All items")
            .font(.system(size: 20, weight: .bold))
            .foregroundStyle(AgnesTheme.text)
          if model.marketplaceShelf == .stack {
            Text("Active items, including any required by your organization")
              .font(.caption)
              .foregroundStyle(AgnesTheme.textMuted)
          }
        }
        Text("\(items.count)")
          .font(.subheadline.monospacedDigit())
          .foregroundStyle(AgnesTheme.textMuted)
        Spacer()
        if model.isRefreshingMarketplace {
          ProgressView()
            .controlSize(.small)
          Text("Refreshing…")
            .font(.caption)
            .foregroundStyle(AgnesTheme.textMuted)
        }
      }

      LazyVGrid(columns: columns, alignment: .leading, spacing: 16) {
        ForEach(items) { item in
          MarketplaceCard(item: item) {
            model.presentMarketplaceItem(item)
          }
        }
      }
    }
  }
}

private struct MarketplaceHero: View {
  @ObservedObject var model: AppModel

  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack(alignment: .top) {
        VStack(alignment: .leading, spacing: 6) {
          Text("MARKETPLACE")
            .font(.system(size: 12, weight: .bold))
            .tracking(1.5)
            .foregroundStyle(AgnesTheme.action)
          Text("Plugin Marketplace")
            .font(.system(size: 30, weight: .bold))
            .foregroundStyle(AgnesTheme.text)
          Text(
            model.marketplaceShelf == .stack
              ? "Everything currently active in your Agnes stack."
              : "Discover AI tools from curated catalogs and the community."
          )
          .font(.system(size: 15))
          .foregroundStyle(AgnesTheme.textSecondary)
        }
        Spacer()
        ZStack {
          Circle()
            .fill(AgnesTheme.actionTint)
            .frame(width: 92, height: 92)
          Image(systemName: "storefront.fill")
            .font(.system(size: 42, weight: .medium))
            .foregroundStyle(AgnesTheme.action)
          Image(systemName: "sparkles")
            .font(.system(size: 18, weight: .semibold))
            .foregroundStyle(.purple)
            .offset(x: 42, y: -32)
        }
        .accessibilityHidden(true)
      }

      if model.marketplaceShelf == .browse {
        HStack(spacing: 10) {
          HStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
              .foregroundStyle(AgnesTheme.textMuted)
            TextField(
              "Search plugins, skills, agents — by name, description, author…",
              text: $model.marketplaceQuery
            )
            .textFieldStyle(.plain)
            .disabled(model.hasActiveCLICommand)
            .onSubmit {
              Task { await model.refreshMarketplace() }
            }
          }
          .padding(.horizontal, 14)
          .frame(height: 42)
          .background(AgnesTheme.surface, in: RoundedRectangle(cornerRadius: 10))
          .overlay {
            RoundedRectangle(cornerRadius: 10)
              .stroke(AgnesTheme.border, lineWidth: 1)
          }

          Button("Search") {
            Task { await model.refreshMarketplace() }
          }
          .buttonStyle(AgnesPrimaryButtonStyle())
          .keyboardShortcut(.return, modifiers: [])
          .disabled(model.hasActiveCLICommand)
        }
      } else {
        Label(
          "This view comes directly from your Agnes stack, not the current Marketplace search.",
          systemImage: "checkmark.seal"
        )
        .font(.subheadline)
        .foregroundStyle(AgnesTheme.textSecondary)
        .padding(.vertical, 4)
      }
    }
    .padding(26)
    .background(
      LinearGradient(
        colors: [
          AgnesTheme.surface,
          Color(red: 239 / 255, green: 246 / 255, blue: 255 / 255),
          Color(red: 245 / 255, green: 243 / 255, blue: 255 / 255),
        ],
        startPoint: .leading,
        endPoint: .trailing
      ),
      in: RoundedRectangle(cornerRadius: AgnesTheme.cardRadius)
    )
    .overlay {
      RoundedRectangle(cornerRadius: AgnesTheme.cardRadius)
        .stroke(AgnesTheme.border, lineWidth: 1)
    }
  }
}

private struct AgnesShelfPicker: View {
  @ObservedObject var model: AppModel

  var body: some View {
    HStack(spacing: 3) {
      ForEach(AppModel.MarketplaceShelf.allCases) { shelf in
        Button {
          Task { await model.selectMarketplaceShelf(shelf) }
        } label: {
          HStack(spacing: 7) {
            Image(systemName: shelf == .browse ? "scope" : "square.stack.3d.up")
            Text(shelf.title)
            Text(
              "\(shelf == .browse ? model.marketplaceItems.count : model.installedMarketplaceCount)"
            )
            .font(.caption.monospacedDigit())
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(.white.opacity(0.13), in: Capsule())
          }
          .font(.system(size: 13, weight: .medium))
          .foregroundStyle(.white)
          .padding(.horizontal, 14)
          .padding(.vertical, 9)
          .background(
            model.marketplaceShelf == shelf ? Color.white.opacity(0.15) : .clear,
            in: RoundedRectangle(cornerRadius: 8)
          )
        }
        .buttonStyle(.plain)
        .disabled(model.hasActiveCLICommand)
        .accessibilityAddTraits(model.marketplaceShelf == shelf ? .isSelected : [])
      }
    }
    .padding(4)
    .background(
      Color(red: 38 / 255, green: 38 / 255, blue: 38 / 255),
      in: RoundedRectangle(cornerRadius: 11)
    )
  }
}

private struct MarketplaceCard: View {
  let item: MarketplaceItem
  let action: () -> Void

  var body: some View {
    Button(action: action) {
      VStack(alignment: .leading, spacing: 13) {
        HStack(alignment: .top) {
          Text(initials)
            .font(.system(size: 20, weight: .bold))
            .foregroundStyle(typeColor)
            .frame(width: 52, height: 52)
            .background(typeColor.opacity(0.1), in: RoundedRectangle(cornerRadius: 11))
          Spacer()
          if item.installed {
            Label("In stack", systemImage: "checkmark.circle.fill")
              .font(.caption.weight(.semibold))
              .foregroundStyle(.green)
          }
        }

        VStack(alignment: .leading, spacing: 6) {
          HStack(spacing: 6) {
            Text(item.type.rawValue.uppercased())
              .font(.system(size: 10, weight: .bold))
              .foregroundStyle(typeColor)
            if let category = item.category, !category.isEmpty {
              Text(category)
                .font(.caption)
                .foregroundStyle(AgnesTheme.textMuted)
            }
          }
          Text(title)
            .font(.system(size: 17, weight: .bold))
            .foregroundStyle(AgnesTheme.text)
            .lineLimit(2)
          Text(publisher)
            .font(.caption)
            .foregroundStyle(AgnesTheme.textMuted)
            .lineLimit(1)
        }

        Text(summary)
          .font(.system(size: 13))
          .foregroundStyle(AgnesTheme.textSecondary)
          .lineLimit(4)
          .frame(maxWidth: .infinity, minHeight: 58, alignment: .topLeading)

        Divider()
          .overlay(AgnesTheme.border)

        HStack(spacing: 12) {
          if item.stackCount > 0 {
            Label("\(item.stackCount) installed", systemImage: "shippingbox")
          }
          if item.invocations30d > 0 {
            Label("\(item.invocations30d) calls", systemImage: "bolt.fill")
          }
          Spacer()
          if let version = item.version, !version.isEmpty {
            Text("v\(version)")
              .lineLimit(1)
          }
        }
        .font(.caption)
        .foregroundStyle(AgnesTheme.textMuted)
      }
      .frame(maxWidth: .infinity, alignment: .leading)
      .agnesCard(padding: 17)
      .contentShape(Rectangle())
    }
    .buttonStyle(.plain)
    .accessibilityLabel("\(title), \(item.type.rawValue), \(publisher)")
    .accessibilityHint("Open Marketplace details")
  }

  private var title: String {
    item.displayName?.nonEmpty ?? item.name
  }

  private var publisher: String {
    if let marketplace = item.marketplaceName?.nonEmpty {
      return "via \(marketplace)"
    }
    if let owner = item.owner?.nonEmpty {
      return "by \(owner)"
    }
    return item.source.rawValue.capitalized
  }

  private var summary: String {
    item.tagline?.nonEmpty ?? item.description?.nonEmpty ?? "No description provided."
  }

  private var initials: String {
    let words = title.split(whereSeparator: { !$0.isLetter && !$0.isNumber })
    let letters = words.prefix(2).compactMap(\.first)
    return letters.isEmpty ? "A" : String(letters).uppercased()
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

private struct MarketplaceErrorBanner: View {
  let message: String
  let retry: () -> Void

  var body: some View {
    HStack(alignment: .top, spacing: 12) {
      Image(systemName: "exclamationmark.triangle.fill")
        .foregroundStyle(.orange)
      VStack(alignment: .leading, spacing: 4) {
        Text("Marketplace could not refresh")
          .font(.headline)
        Text(message)
          .font(.caption)
          .foregroundStyle(AgnesTheme.textSecondary)
          .textSelection(.enabled)
      }
      Spacer()
      Button("Retry", action: retry)
        .buttonStyle(AgnesSecondaryButtonStyle())
    }
    .agnesCard()
  }
}

private struct MarketplaceSuccessBanner: View {
  let message: String
  let dismiss: () -> Void

  var body: some View {
    HStack(spacing: 12) {
      Image(systemName: "checkmark.circle.fill")
        .foregroundStyle(.green)
      Text(message)
        .font(.subheadline)
        .foregroundStyle(AgnesTheme.textSecondary)
        .textSelection(.enabled)
      Spacer()
      Button("Dismiss", action: dismiss)
        .buttonStyle(.borderless)
    }
    .agnesCard()
  }
}

private struct MarketplaceChipButtonStyle: ButtonStyle {
  let isSelected: Bool

  func makeBody(configuration: Configuration) -> some View {
    configuration.label
      .font(.system(size: 13, weight: isSelected ? .semibold : .medium))
      .foregroundStyle(isSelected ? .white : AgnesTheme.text)
      .padding(.horizontal, 13)
      .padding(.vertical, 7)
      .background(
        isSelected ? AgnesTheme.action : AgnesTheme.surface,
        in: Capsule()
      )
      .overlay {
        if !isSelected {
          Capsule().stroke(AgnesTheme.border, lineWidth: 1)
        }
      }
      .opacity(configuration.isPressed ? 0.78 : 1)
  }
}

extension MarketplaceSort {
  fileprivate var displayName: String {
    switch self {
    case .recent: "Recent"
    case .mostUsed: "Most used"
    case .trending: "Trending"
    }
  }
}

extension String {
  var nonEmpty: String? {
    let value = trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.isEmpty else {
      return nil
    }
    return value
  }
}
