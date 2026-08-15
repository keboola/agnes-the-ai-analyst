import Foundation

@MainActor
final class AppModel: ObservableObject {
  enum Destination: String, CaseIterable, Identifiable {
    case marketplace
    case ask
    case settings

    var id: String { rawValue }

    var title: String {
      switch self {
      case .marketplace: "Marketplace"
      case .ask: "Ask Agnes"
      case .settings: "Settings"
      }
    }

    var systemImage: String {
      switch self {
      case .marketplace: "storefront"
      case .ask: "sparkles"
      case .settings: "gearshape"
      }
    }
  }

  enum CLIStatus: Equatable {
    case idle
    case checking
    case ready(version: String)
    case unavailable(String)
  }

  enum MarketplaceShelf: String, CaseIterable, Identifiable {
    case browse
    case stack

    var id: String { rawValue }
    var title: String { self == .browse ? "Browse" : "My Stack" }
  }

  enum MarketplaceTypeFilter: String, CaseIterable, Identifiable {
    case all
    case skills
    case agents
    case plugins

    var id: String { rawValue }

    var title: String {
      rawValue.capitalized
    }

    var cliValue: MarketplaceItemType? {
      switch self {
      case .all: nil
      case .skills: .skill
      case .agents: .agent
      case .plugins: .plugin
      }
    }
  }

  enum MarketplaceSourceFilter: String, CaseIterable, Identifiable {
    case all
    case curated
    case community

    var id: String { rawValue }

    var title: String {
      switch self {
      case .all: "All sources"
      case .curated: "Curated"
      case .community: "Community"
      }
    }

    var cliValue: MarketplaceSource? {
      switch self {
      case .all: nil
      case .curated: .curated
      case .community: .flea
      }
    }
  }

  struct MarketplaceActionOutcome: Equatable {
    let itemID: String
    let installed: Bool
    let message: String
  }

  @Published var selectedDestination: Destination = .marketplace
  @Published var preferredExecutablePath: String {
    didSet {
      UserDefaults.standard.set(preferredExecutablePath, forKey: Self.executablePathKey)
      invalidateCLISelection()
    }
  }
  @Published var agentSlug: String {
    didSet {
      UserDefaults.standard.set(agentSlug, forKey: Self.agentSlugKey)
    }
  }
  @Published var prompt = ""

  @Published private(set) var cliStatus: CLIStatus = .idle
  @Published var marketplaceQuery = ""
  @Published var marketplaceShelf: MarketplaceShelf = .browse
  @Published var marketplaceTypeFilter: MarketplaceTypeFilter = .all
  @Published var marketplaceSourceFilter: MarketplaceSourceFilter = .all
  @Published var marketplaceSort: MarketplaceSort = .recent
  @Published private(set) var marketplaceItems: [MarketplaceItem] = []
  @Published private(set) var marketplaceStackItems: [MarketplaceItem] = []
  @Published private(set) var marketplaceTotal = 0
  @Published private(set) var marketplaceBrowseError: String?
  @Published private(set) var marketplaceStackError: String?
  @Published private(set) var marketplaceActionOutcome: MarketplaceActionOutcome?
  @Published var presentedMarketplaceItem: MarketplaceItem?
  @Published private(set) var marketplaceDetail: MarketplaceDetail?
  @Published private(set) var marketplaceDetailError: String?
  @Published private(set) var isRefreshingMarketplace = false
  @Published private(set) var isLoadingMarketplaceDetail = false
  @Published private(set) var isChangingMarketplaceStack = false

  @Published private(set) var result: AskResult?
  @Published private(set) var askError: String?
  @Published private(set) var isAsking = false
  @Published private(set) var isStopping = false

  private static let executablePathKey = "agnesExecutablePath"
  private static let agentSlugKey = "agnesAgentSlug"
  private let cli: any AgnesCLIProviding
  private var executableURL: URL?
  private var askTask: Task<Void, Never>?

  init(cli: any AgnesCLIProviding = AgnesCLIClient()) {
    self.cli = cli
    preferredExecutablePath = UserDefaults.standard.string(forKey: Self.executablePathKey) ?? ""
    agentSlug = UserDefaults.standard.string(forKey: Self.agentSlugKey) ?? ""
  }

  var visibleMarketplaceItems: [MarketplaceItem] {
    switch marketplaceShelf {
    case .browse:
      marketplaceItems
    case .stack:
      marketplaceStackItems
    }
  }

  var installedMarketplaceCount: Int {
    marketplaceStackItems.count
  }

  var marketplaceActionMessage: String? {
    marketplaceActionOutcome?.message
  }

  /// Each shelf owns its own request and error state. A failed Stack refresh
  /// must not obscure a usable Browse result (and vice versa).
  var marketplaceError: String? {
    switch marketplaceShelf {
    case .browse: marketplaceBrowseError
    case .stack: marketplaceStackError
    }
  }

  var resolvedExecutablePath: String? {
    executableURL?.path
  }

  var canAsk: Bool {
    executableURL != nil
      && !agentSlug.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !hasActiveCLICommand
  }

  var hasActiveCLICommand: Bool {
    isRefreshingMarketplace || isLoadingMarketplaceDetail || isChangingMarketplaceStack
      || isAsking || isStopping
  }

  func bootstrap() async {
    guard !hasActiveCLICommand else { return }

    isRefreshingMarketplace = true
    marketplaceActionOutcome = nil
    defer { isRefreshingMarketplace = false }

    guard let executable = await resolveAndVerifyExecutable() else {
      setMarketplaceError(cliUnavailableMessage, for: .browse)
      setMarketplaceError(cliUnavailableMessage, for: .stack)
      return
    }

    await refreshMarketplaceShelf(.browse, executable: executable)
    await refreshMarketplaceShelf(.stack, executable: executable)
  }

  func refreshMarketplace() async {
    guard !hasActiveCLICommand else { return }

    isRefreshingMarketplace = true
    marketplaceActionOutcome = nil
    cliStatus = .checking
    defer { isRefreshingMarketplace = false }

    let shelf = marketplaceShelf
    guard let executable = await resolveAndVerifyExecutable() else {
      setMarketplaceError(cliUnavailableMessage, for: shelf)
      return
    }

    await refreshMarketplaceShelf(shelf, executable: executable)
  }

  func selectMarketplaceShelf(_ shelf: MarketplaceShelf) async {
    marketplaceShelf = shelf
    await refreshMarketplace()
  }

  func presentMarketplaceItem(_ item: MarketplaceItem) {
    guard !hasActiveCLICommand else { return }
    marketplaceActionOutcome = nil
    presentedMarketplaceItem = item
    marketplaceDetail = nil
    marketplaceDetailError = nil
    Task { await loadPresentedMarketplaceDetail() }
  }

  func loadPresentedMarketplaceDetail() async {
    guard
      !isLoadingMarketplaceDetail,
      !isRefreshingMarketplace,
      !isChangingMarketplaceStack,
      !isAsking,
      let executable = executableURL,
      let item = presentedMarketplaceItem
    else { return }

    isLoadingMarketplaceDetail = true
    marketplaceDetailError = nil
    defer { isLoadingMarketplaceDetail = false }

    do {
      marketplaceDetail = try await cli.marketplaceDetail(
        executable: executable,
        itemID: item.id
      )
    } catch {
      marketplaceDetailError = error.localizedDescription
    }
  }

  func dismissMarketplaceDetail() {
    guard !isChangingMarketplaceStack else { return }
    presentedMarketplaceItem = nil
    marketplaceDetail = nil
    marketplaceDetailError = nil
  }

  func setPresentedMarketplaceItemInstalled(_ installed: Bool) async {
    guard
      !hasActiveCLICommand,
      let executable = executableURL,
      let item = presentedMarketplaceItem,
      let detail = marketplaceDetail,
      installed ? detail.installable : (!detail.isSystem && !detail.isRequired)
    else { return }

    isChangingMarketplaceStack = true
    marketplaceDetailError = nil
    marketplaceActionOutcome = nil

    do {
      let message: String
      if installed {
        message = try await cli.addMarketplaceItem(executable: executable, itemID: item.id)
      } else {
        message = try await cli.removeMarketplaceItem(executable: executable, itemID: item.id)
      }
      isChangingMarketplaceStack = false
      await refreshMarketplaceDataAfterStackChange(executable: executable)
      if presentedMarketplaceItem != nil {
        await loadPresentedMarketplaceDetail()
      }
      marketplaceActionOutcome = MarketplaceActionOutcome(
        itemID: item.id,
        installed: installed,
        message: message
      )
    } catch {
      marketplaceDetailError = error.localizedDescription
      isChangingMarketplaceStack = false
    }
  }

  func clearMarketplaceNotice() {
    marketplaceActionOutcome = nil
  }

  func submitPrompt() {
    guard canAsk, let executable = executableURL else { return }

    let slug = agentSlug.trimmingCharacters(in: .whitespacesAndNewlines)
    let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
    askError = nil
    result = nil
    isAsking = true
    isStopping = false

    askTask = Task { [weak self, cli] in
      guard let self else { return }
      defer {
        self.isAsking = false
        self.isStopping = false
        self.askTask = nil
      }

      do {
        let response = try await cli.ask(
          executable: executable,
          agentSlug: slug,
          prompt: text
        )
        if self.isStopping {
          self.askError = "Request stopped."
        } else {
          self.result = response
        }
      } catch {
        self.askError =
          self.isStopping
          ? "Request stopped."
          : "Agnes could not complete the request: \(error.localizedDescription)"
      }
    }
  }

  func stopRequest() {
    guard isAsking, !isStopping else { return }
    isStopping = true
    cli.cancel()
  }

  func cancelForTermination() {
    guard hasActiveCLICommand else { return }
    if isAsking {
      isStopping = true
    }
    cli.cancel()
  }

  func clearAsk() {
    guard !isAsking else { return }
    prompt = ""
    result = nil
    askError = nil
  }

  private var normalizedMarketplaceQuery: String? {
    let query = marketplaceQuery.trimmingCharacters(in: .whitespacesAndNewlines)
    return query.isEmpty ? nil : query
  }

  private func resolveExecutable() -> URL? {
    let preferred = preferredExecutablePath.trimmingCharacters(in: .whitespacesAndNewlines)
    let resolved = ExecutableLocator.locate(preferredPath: preferred.isEmpty ? nil : preferred)
    executableURL = resolved
    return resolved
  }

  private func invalidateCLISelection() {
    executableURL = nil
    cliStatus = .idle
    marketplaceItems = []
    marketplaceStackItems = []
    marketplaceTotal = 0
    marketplaceBrowseError = nil
    marketplaceStackError = nil
    marketplaceActionOutcome = nil
    presentedMarketplaceItem = nil
    marketplaceDetail = nil
    marketplaceDetailError = nil
    result = nil
    askError = nil
  }

  private var cliUnavailableMessage: String {
    switch cliStatus {
    case .unavailable(let message): message
    case .idle, .checking, .ready:
      "Agnes CLI was not found. Choose its executable in Settings."
    }
  }

  private func resolveAndVerifyExecutable() async -> URL? {
    guard let executable = resolveExecutable() else {
      let message = "Agnes CLI was not found. Choose its executable in Settings."
      cliStatus = .unavailable(message)
      return nil
    }

    do {
      let version = try await cli.version(executable: executable)
      cliStatus = .ready(version: version)
      return executable
    } catch {
      cliStatus = .unavailable(error.localizedDescription)
      return nil
    }
  }

  private func refreshMarketplaceShelf(_ shelf: MarketplaceShelf, executable: URL) async {
    setMarketplaceError(nil, for: shelf)

    do {
      switch shelf {
      case .browse:
        let response = try await cli.searchMarketplace(
          executable: executable,
          query: normalizedMarketplaceQuery,
          type: marketplaceTypeFilter.cliValue,
          source: marketplaceSourceFilter.cliValue,
          sort: marketplaceSort,
          limit: 48
        )
        marketplaceItems = response.items
        marketplaceTotal = response.total
      case .stack:
        let response = try await cli.myStack(executable: executable)
        marketplaceStackItems = response.items
      }
      refreshPresentedItemFromMarketplaceResults()
    } catch {
      // Preserve the last good data for this shelf; the error is shelf-scoped.
      setMarketplaceError(error.localizedDescription, for: shelf)
    }
  }

  private func refreshMarketplaceDataAfterStackChange(executable: URL) async {
    isRefreshingMarketplace = true
    await refreshMarketplaceShelf(.browse, executable: executable)
    await refreshMarketplaceShelf(.stack, executable: executable)
    isRefreshingMarketplace = false
  }

  private func setMarketplaceError(_ message: String?, for shelf: MarketplaceShelf) {
    switch shelf {
    case .browse: marketplaceBrowseError = message
    case .stack: marketplaceStackError = message
    }
  }

  private func refreshPresentedItemFromMarketplaceResults() {
    guard let current = presentedMarketplaceItem else { return }
    if let updated = (marketplaceStackItems + marketplaceItems).first(where: {
      $0.id == current.id
    }) {
      presentedMarketplaceItem = updated
    }
  }
}
