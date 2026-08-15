import Foundation

@MainActor
final class AppModel: ObservableObject {
  enum Destination: String, CaseIterable, Identifiable {
    case marketplace
    case runs
    case settings

    var id: String { rawValue }

    var title: String {
      switch self {
      case .marketplace: "Marketplace"
      case .runs: "Agent Runs"
      case .settings: "Settings"
      }
    }

    var systemImage: String {
      switch self {
      case .marketplace: "storefront"
      case .runs: "terminal"
      case .settings: "gearshape"
      }
    }
  }

  enum RunInspectorTab: String, CaseIterable, Identifiable {
    case run = "Run"
    case agent = "Agent"
    case events = "Events"

    var id: String { rawValue }
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
    var title: String { rawValue.capitalized }

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
      if oldValue != agentSlug {
        agentUsage = nil
        agentUsageError = nil
      }
    }
  }
  @Published var prompt = ""
  @Published var runInspectorTab: RunInspectorTab = .run

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

  @Published private(set) var runs: [AgentRunRecord] = []
  @Published var selectedRunID: UUID?
  @Published private(set) var isRunningAgent = false
  @Published private(set) var isStoppingAgent = false
  @Published private(set) var agentUsage: AgentUsage?
  @Published private(set) var agentUsageError: String?
  @Published private(set) var isLoadingAgentUsage = false

  private static let executablePathKey = "agnesExecutablePath"
  private static let agentSlugKey = "agnesAgentSlug"
  private let cli: any AgnesCLIProviding
  private var executableURL: URL?
  private var activeRequestID: UUID?
  private var runTask: Task<Void, Never>?

  init(cli: any AgnesCLIProviding = AgnesCLIClient()) {
    self.cli = cli
    preferredExecutablePath = UserDefaults.standard.string(forKey: Self.executablePathKey) ?? ""
    agentSlug = UserDefaults.standard.string(forKey: Self.agentSlugKey) ?? ""
  }

  var visibleMarketplaceItems: [MarketplaceItem] {
    marketplaceShelf == .browse ? marketplaceItems : marketplaceStackItems
  }

  var installedMarketplaceCount: Int { marketplaceStackItems.count }
  var marketplaceActionMessage: String? { marketplaceActionOutcome?.message }

  /// Each shelf owns its own request and error state. A failed Stack refresh
  /// must not obscure a usable Browse result (and vice versa).
  var marketplaceError: String? {
    marketplaceShelf == .browse ? marketplaceBrowseError : marketplaceStackError
  }

  var resolvedExecutablePath: String? { executableURL?.path }

  var cliVersion: String? {
    guard case .ready(let version) = cliStatus else { return nil }
    return version
  }

  var selectedRun: AgentRunRecord? {
    guard let selectedRunID else { return runs.first }
    return runs.first(where: { $0.id == selectedRunID })
  }

  var activeRun: AgentRunRecord? {
    guard let activeRequestID else { return nil }
    return runs.first(where: { $0.id == activeRequestID })
  }

  var canRun: Bool {
    executableURL != nil
      && !agentSlug.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !isRunningAgent
  }

  var hasActiveMarketplaceCommand: Bool {
    isRefreshingMarketplace || isLoadingMarketplaceDetail || isChangingMarketplaceStack
  }

  var hasActiveCLICommand: Bool {
    hasActiveMarketplaceCommand || isRunningAgent || isStoppingAgent || isLoadingAgentUsage
  }

  var agentRuntimeContract: String {
    let slug = agentSlug.trimmingCharacters(in: .whitespacesAndNewlines)
    let payload: [String: Any] = [
      "agent": ["slug": slug.isEmpty ? "<agent-slug>" : slug],
      "transport": [
        "executable": resolvedExecutablePath ?? "agnes",
        "mode": "isolated-one-shot",
        "format": "ag-ui-event-array",
      ],
      "invocation": [
        "arguments": ["chat", "--agent", slug.isEmpty ? "<agent-slug>" : slug, "--once", "<prompt>", "--json"]
      ],
      "limitations": [
        "session_cleanup_is_best_effort_after_run",
        "no_agent_discovery_with_pat",
        "events_available_after_process_exit",
      ],
    ]
    guard let data = try? JSONSerialization.data(
      withJSONObject: payload,
      options: [.prettyPrinted, .sortedKeys]
    ) else { return "{}" }
    return String(decoding: data, as: UTF8.self)
  }

  func bootstrap() async {
    guard !hasActiveMarketplaceCommand else { return }

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
    guard !hasActiveMarketplaceCommand else { return }

    isRefreshingMarketplace = true
    marketplaceActionOutcome = nil
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
    guard !hasActiveMarketplaceCommand else { return }
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
      !hasActiveMarketplaceCommand,
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

  func submitRun() {
    guard canRun, let executable = executableURL else { return }

    let slug = agentSlug.trimmingCharacters(in: .whitespacesAndNewlines)
    let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
    let requestID = UUID()
    let record = AgentRunRecord(
      id: requestID,
      agentSlug: slug,
      prompt: text,
      startedAt: Date(),
      finishedAt: nil,
      state: .running,
      result: nil,
      errorMessage: nil
    )

    runs.insert(record, at: 0)
    selectedRunID = requestID
    runInspectorTab = .run
    activeRequestID = requestID
    isRunningAgent = true
    isStoppingAgent = false
    prompt = ""

    runTask = Task { [weak self, cli] in
      guard let self else { return }

      do {
        let result = try await cli.runAgent(
          executable: executable,
          requestID: requestID,
          agentSlug: slug,
          prompt: text
        )
        if self.isStoppingAgent {
          self.finishRun(
            requestID,
            state: .stopped,
            result: result,
            errorMessage: "Run stopped by you."
          )
        } else {
          let state: AgentRunRecord.State
          switch result.outcome {
          case .completed: state = .completed
          case .failed: state = .failed
          case .truncated: state = .truncated
          }
          self.finishRun(
            requestID,
            state: state,
            result: result,
            errorMessage: result.errorMessage
          )
        }
      } catch {
        self.finishRun(
          requestID,
          state: self.isStoppingAgent ? .stopped : .failed,
          result: nil,
          errorMessage: self.isStoppingAgent
            ? "Run stopped by you."
            : "Agnes CLI could not complete the run: \(error.localizedDescription)"
        )
      }

      if self.activeRequestID == requestID {
        self.activeRequestID = nil
      }
      self.isRunningAgent = false
      self.isStoppingAgent = false
      self.runTask = nil

      // Usage is an independent CLI read. A slow usage endpoint must not make
      // a completed run look active or leave its Stop button enabled.
      await self.refreshAgentUsage(for: slug, executable: executable)
    }
  }

  func stopRun() {
    guard let requestID = activeRequestID, isRunningAgent, !isStoppingAgent else { return }
    isStoppingAgent = true
    cli.cancel(requestID: requestID)
  }

  func selectRun(_ id: UUID) {
    guard runs.contains(where: { $0.id == id }) else { return }
    selectedRunID = id
    runInspectorTab = .run
  }

  func clearRuns() {
    guard !isRunningAgent else { return }
    runs = []
    selectedRunID = nil
    runInspectorTab = .agent
  }

  func refreshAgentUsage() async {
    guard let executable = executableURL else { return }
    let slug = agentSlug.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !slug.isEmpty else { return }
    await refreshAgentUsage(for: slug, executable: executable)
  }

  func cancelForTermination() {
    if let activeRequestID {
      isStoppingAgent = true
      cli.cancel(requestID: activeRequestID)
    }
    cli.cancelAll()
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
    agentUsage = nil
    agentUsageError = nil
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

  private func finishRun(
    _ id: UUID,
    state: AgentRunRecord.State,
    result: AgentRunResult?,
    errorMessage: String?
  ) {
    guard let index = runs.firstIndex(where: { $0.id == id }) else { return }
    runs[index].finishedAt = Date()
    runs[index].state = state
    runs[index].result = result
    runs[index].errorMessage = errorMessage
  }

  private func refreshAgentUsage(for slug: String, executable: URL) async {
    guard !isLoadingAgentUsage else { return }
    isLoadingAgentUsage = true
    agentUsageError = nil
    defer { isLoadingAgentUsage = false }

    do {
      let usage = try await cli.agentUsage(executable: executable, agentSlug: slug)
      if agentSlug.trimmingCharacters(in: .whitespacesAndNewlines) == slug {
        agentUsage = usage
      }
    } catch {
      if agentSlug.trimmingCharacters(in: .whitespacesAndNewlines) == slug {
        agentUsageError = error.localizedDescription
      }
    }
  }
}
