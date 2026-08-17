import Foundation
import XCTest

@testable import AgnesDesktop

@MainActor
final class AppModelTests: XCTestCase {
  private let executablePathKey = "agnesExecutablePath"
  private let agentSlugKey = "agnesAgentSlug"

  override func setUp() {
    super.setUp()
    UserDefaults.standard.removeObject(forKey: executablePathKey)
    UserDefaults.standard.removeObject(forKey: agentSlugKey)
  }

  override func tearDown() {
    UserDefaults.standard.removeObject(forKey: executablePathKey)
    UserDefaults.standard.removeObject(forKey: agentSlugKey)
    super.tearDown()
  }

  func testBootstrapLoadsBrowseAndAuthoritativeStackWithoutAgentList() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(initiallyInstalled: false)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path

    await model.bootstrap()

    XCTAssertEqual(model.cliStatus, .ready(version: "agnes test"))
    XCTAssertEqual(model.marketplaceItems.map(\.id), ["curated-foundry-ai/pdf-generator"])
    XCTAssertEqual(model.marketplaceTotal, 1)
    XCTAssertTrue(model.marketplaceStackItems.isEmpty)
    XCTAssertNil(model.marketplaceError)
    XCTAssertEqual(
      cli.capturedSearchInvocations(),
      [
        .init(
          query: nil,
          type: nil,
          source: nil,
          sort: .recent,
          limit: 48
        )
      ]
    )
    XCTAssertEqual(cli.capturedMyStackInvocationCount(), 1)
  }

  func testMarketplaceRefreshFailureKeepsThePreviousSuccessfulShelf() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(initiallyInstalled: false)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    let firstShelf = model.marketplaceItems

    cli.failNextMarketplaceSearch(with: TestError.offline)
    model.marketplaceQuery = "pdf"
    await model.refreshMarketplace()

    XCTAssertEqual(model.marketplaceItems, firstShelf)
    XCTAssertEqual(model.marketplaceTotal, 1)
    XCTAssertEqual(model.marketplaceError, TestError.offline.localizedDescription)
    XCTAssertFalse(model.isRefreshingMarketplace)
    XCTAssertEqual(cli.capturedSearchInvocations().last?.query, "pdf")
  }

  func testMyStackUsesItsOwnCLIResultAndKeepsItAfterAFailedRefresh() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(initiallyInstalled: true)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()

    XCTAssertEqual(model.installedMarketplaceCount, 1)
    XCTAssertEqual(model.marketplaceStackItems.map(\.id), ["curated-foundry-ai/pdf-generator"])

    model.marketplaceQuery = "this must not filter the stack"
    await model.selectMarketplaceShelf(.stack)

    XCTAssertEqual(model.visibleMarketplaceItems, model.marketplaceStackItems)
    XCTAssertEqual(cli.capturedSearchInvocations().count, 1)
    XCTAssertEqual(cli.capturedMyStackInvocationCount(), 2)

    cli.failNextMyStack(with: TestError.offline)
    await model.refreshMarketplace()

    XCTAssertEqual(model.marketplaceStackItems.map(\.id), ["curated-foundry-ai/pdf-generator"])
    XCTAssertEqual(model.marketplaceError, TestError.offline.localizedDescription)
    XCTAssertNil(model.marketplaceBrowseError)
  }

  func testVersionFailureIsVisibleOnTheMarketplaceSurface() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(versionError: TestError.offline)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path

    await model.bootstrap()

    let message = TestError.offline.localizedDescription
    XCTAssertEqual(model.cliStatus, .unavailable(message))
    XCTAssertEqual(model.marketplaceError, message)
    XCTAssertTrue(model.marketplaceItems.isEmpty)
    XCTAssertTrue(cli.capturedSearchInvocations().isEmpty)
  }

  func testManualAgentRunCanBeCancelledByRequestID() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(blockRun: true)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    model.agentSlug = "manual-analyst"
    model.prompt = "Keep working"

    XCTAssertTrue(model.canRun)
    model.submitRun()
    await cli.waitUntilRunStarted()
    let requestID = try XCTUnwrap(cli.capturedRunInvocation()?.requestID)
    model.stopRun()

    XCTAssertEqual(cli.capturedCancelCount(), 1)
    XCTAssertEqual(cli.capturedCancelledRequestIDs(), [requestID])
    XCTAssertTrue(model.isStoppingAgent)
    for _ in 0..<20 where model.isRunningAgent {
      await Task.yield()
    }
    XCTAssertFalse(model.isRunningAgent)
    XCTAssertEqual(model.runs.first?.state, .stopped)
    XCTAssertEqual(model.runs.first?.errorMessage, "Run stopped by you.")
    XCTAssertEqual(cli.capturedRunInvocation()?.agentSlug, "manual-analyst")
  }

  func testCompletedRunRetainsEventsAndRefreshesAgentUsage() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI()
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    model.agentSlug = "revenue-analyst"
    model.prompt = "Explain the change"

    model.submitRun()
    await waitForAgentRunToFinish(in: model)
    for _ in 0..<50 where model.agentUsage == nil {
      await Task.yield()
    }

    let run = try XCTUnwrap(model.runs.first)
    XCTAssertEqual(run.state, .completed)
    XCTAssertEqual(run.result?.answer, "Done")
    XCTAssertEqual(run.result?.toolNames, ["agnes_query"])
    XCTAssertEqual(model.selectedRunID, run.id)
    XCTAssertEqual(model.agentUsage?.agentSlug, "revenue-analyst")
    XCTAssertEqual(model.agentUsage?.totalTokens, 250)
  }

  func testMarketplaceRefreshCanRunWhileAnAgentRunIsActive() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(blockRun: true)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    model.agentSlug = "manual-analyst"
    model.prompt = "Keep working"
    model.submitRun()
    await cli.waitUntilRunStarted()

    await model.refreshMarketplace()

    XCTAssertTrue(model.isRunningAgent)
    XCTAssertEqual(cli.capturedSearchInvocations().count, 2)
    model.stopRun()
    await waitForAgentRunToFinish(in: model)
  }

  func testCompletedRunDoesNotStayActiveWhileUsageIsLoading() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(blockUsage: true)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    model.agentSlug = "manual-analyst"
    model.prompt = "Finish before usage loads"

    model.submitRun()
    await cli.waitUntilUsageStarted()

    XCTAssertFalse(model.isRunningAgent)
    XCTAssertFalse(model.isStoppingAgent)
    XCTAssertEqual(model.runs.first?.state, .completed)
    XCTAssertTrue(model.isLoadingAgentUsage)

    cli.finishUsage()
    for _ in 0..<20 where model.isLoadingAgentUsage {
      await Task.yield()
    }
    XCTAssertFalse(model.isLoadingAgentUsage)
  }

  func testAddAndRemoveRefreshTheMarketplaceStack() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(initiallyInstalled: false)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    guard let item = model.marketplaceItems.first else {
      return XCTFail("Expected a marketplace fixture")
    }

    model.presentMarketplaceItem(item)
    await waitForMarketplaceDetail(in: model)
    await model.setPresentedMarketplaceItemInstalled(true)

    XCTAssertEqual(cli.capturedAddedItemIDs(), [item.id])
    XCTAssertTrue(model.marketplaceItems.first?.installed == true)
    XCTAssertEqual(model.marketplaceStackItems.map(\.id), [item.id])
    XCTAssertEqual(model.installedMarketplaceCount, 1)
    XCTAssertEqual(model.marketplaceActionMessage, "Added to your stack.")
    XCTAssertEqual(
      model.marketplaceActionOutcome,
      .init(itemID: item.id, installed: true, message: "Added to your stack.")
    )

    await model.setPresentedMarketplaceItemInstalled(false)

    XCTAssertEqual(cli.capturedRemovedItemIDs(), [item.id])
    XCTAssertFalse(model.marketplaceItems.first?.installed == true)
    XCTAssertTrue(model.marketplaceStackItems.isEmpty)
    XCTAssertEqual(model.installedMarketplaceCount, 0)
    XCTAssertEqual(model.marketplaceActionMessage, "Removed from your stack.")
    XCTAssertEqual(
      model.marketplaceActionOutcome,
      .init(itemID: item.id, installed: false, message: "Removed from your stack.")
    )
    XCTAssertEqual(cli.capturedMyStackInvocationCount(), 3)
  }

  func testOpeningAnotherItemDoesNotReuseThePreviousStackActionOutcome() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(initiallyInstalled: false)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    guard let firstItem = model.marketplaceItems.first else {
      return XCTFail("Expected a marketplace fixture")
    }

    model.presentMarketplaceItem(firstItem)
    await waitForMarketplaceDetail(in: model)
    await model.setPresentedMarketplaceItemInstalled(true)
    XCTAssertNotNil(model.marketplaceActionOutcome)

    model.dismissMarketplaceDetail()
    model.presentMarketplaceItem(
      MarketplaceItem(
        id: "curated-example/another-plugin",
        source: .curated,
        type: .plugin,
        name: "another-plugin"
      )
    )

    XCTAssertNil(model.marketplaceActionOutcome)
  }

  private func waitForMarketplaceDetail(in model: AppModel) async {
    for _ in 0..<20 where model.marketplaceDetail == nil && model.marketplaceDetailError == nil {
      await Task.yield()
    }
    XCTAssertNotNil(model.marketplaceDetail)
  }

  private func waitForAgentRunToFinish(in model: AppModel) async {
    for _ in 0..<50 where model.isRunningAgent {
      await Task.yield()
    }
    XCTAssertFalse(model.isRunningAgent)
  }

  private func makeExecutable() throws -> URL {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let executable = directory.appendingPathComponent("agnes")
    try "#!/bin/sh\nexit 0\n".write(to: executable, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes(
      [.posixPermissions: 0o755],
      ofItemAtPath: executable.path
    )
    return executable
  }
}

private enum TestError: LocalizedError {
  case offline

  var errorDescription: String? {
    "Marketplace is temporarily unavailable."
  }
}

private final class ModelTestCLI: AgnesCLIProviding, @unchecked Sendable {
  struct SearchInvocation: Equatable {
    let query: String?
    let type: MarketplaceItemType?
    let source: MarketplaceSource?
    let sort: MarketplaceSort
    let limit: Int
  }

  struct RunInvocation: Equatable {
    let requestID: UUID
    let agentSlug: String
    let prompt: String
  }

  private let lock = NSLock()
  private let blockRun: Bool
  private let blockUsage: Bool
  private let versionError: Error?
  private var installed: Bool
  private var nextSearchError: Error?
  private var nextMyStackError: Error?
  private var searchInvocations: [SearchInvocation] = []
  private var myStackInvocationCount = 0
  private var addedItemIDs: [String] = []
  private var removedItemIDs: [String] = []
  private var runInvocation: RunInvocation?
  private var runContinuation: CheckedContinuation<AgentRunResult, Error>?
  private var runStarted = false
  private var runStartWaiters: [CheckedContinuation<Void, Never>] = []
  private var cancelCount = 0
  private var cancelledRequestIDs: [UUID] = []
  private var usageContinuation: CheckedContinuation<AgentUsage, Never>?
  private var usageStarted = false
  private var usageStartWaiters: [CheckedContinuation<Void, Never>] = []

  init(
    initiallyInstalled: Bool = false,
    blockRun: Bool = false,
    blockUsage: Bool = false,
    versionError: Error? = nil
  ) {
    installed = initiallyInstalled
    self.blockRun = blockRun
    self.blockUsage = blockUsage
    self.versionError = versionError
  }

  func version(executable: URL) async throws -> String {
    if let versionError { throw versionError }
    return "agnes test"
  }

  func searchMarketplace(
    executable: URL,
    query: String?,
    type: MarketplaceItemType?,
    source: MarketplaceSource?,
    sort: MarketplaceSort,
    limit: Int
  ) async throws -> MarketplaceSearchResult {
    let state = lock.withLock { () -> (Bool, Error?) in
      searchInvocations.append(
        .init(query: query, type: type, source: source, sort: sort, limit: limit)
      )
      let error = nextSearchError
      nextSearchError = nil
      return (installed, error)
    }
    if let error = state.1 { throw error }
    return MarketplaceSearchResult(items: [try marketplaceItem(installed: state.0)])
  }

  func marketplaceDetail(executable: URL, itemID: String) async throws -> MarketplaceDetail {
    try makeMarketplaceDetail(installed: lock.withLock { installed })
  }

  func myStack(executable: URL) async throws -> MarketplaceStackResult {
    let state = lock.withLock { () -> (Bool, Error?) in
      myStackInvocationCount += 1
      let error = nextMyStackError
      nextMyStackError = nil
      return (installed, error)
    }
    if let error = state.1 { throw error }
    return MarketplaceStackResult(
      items: state.0 ? [try marketplaceItem(installed: true)] : []
    )
  }

  func addMarketplaceItem(executable: URL, itemID: String) async throws -> String {
    lock.withLock {
      addedItemIDs.append(itemID)
      installed = true
    }
    return "Added to your stack."
  }

  func removeMarketplaceItem(executable: URL, itemID: String) async throws -> String {
    lock.withLock {
      removedItemIDs.append(itemID)
      installed = false
    }
    return "Removed from your stack."
  }

  func agentUsage(executable: URL, agentSlug: String) async throws -> AgentUsage {
    guard blockUsage else { return makeUsage(agentSlug: agentSlug) }
    return await withCheckedContinuation { continuation in
      let waiters = lock.withLock {
        usageContinuation = continuation
        usageStarted = true
        let waiters = usageStartWaiters
        usageStartWaiters.removeAll()
        return waiters
      }
      for waiter in waiters {
        waiter.resume()
      }
    }
  }

  private func makeUsage(agentSlug: String) -> AgentUsage {
    AgentUsage(
      period: "2026-08",
      agentSlug: agentSlug,
      inputTokens: 120,
      outputTokens: 80,
      cacheReadTokens: 40,
      cacheCreationTokens: 10,
      totalTokens: 250,
      budgetLimit: 1000,
      budgetRemaining: 750
    )
  }

  func runAgent(
    executable: URL,
    requestID: UUID,
    agentSlug: String,
    prompt: String
  ) async throws -> AgentRunResult {
    lock.withLock {
      runInvocation = .init(requestID: requestID, agentSlug: agentSlug, prompt: prompt)
    }
    guard blockRun else { return completedRunResult() }
    return try await withCheckedThrowingContinuation { continuation in
      let waiters = lock.withLock {
        runContinuation = continuation
        runStarted = true
        let waiters = runStartWaiters
        runStartWaiters.removeAll()
        return waiters
      }
      for waiter in waiters {
        waiter.resume()
      }
    }
  }

  func cancel(requestID: UUID) {
    let continuation: CheckedContinuation<AgentRunResult, Error>? = lock.withLock {
      guard runInvocation?.requestID == requestID else { return nil }
      cancelCount += 1
      cancelledRequestIDs.append(requestID)
      let continuation = runContinuation
      runContinuation = nil
      return continuation
    }
    continuation?.resume(throwing: CancellationError())
  }

  func cancelAll() {
    let requestID = lock.withLock { runInvocation?.requestID }
    if let requestID {
      cancel(requestID: requestID)
    }
  }

  func failNextMarketplaceSearch(with error: Error) {
    lock.withLock { nextSearchError = error }
  }

  func failNextMyStack(with error: Error) {
    lock.withLock { nextMyStackError = error }
  }

  func waitUntilRunStarted() async {
    await withCheckedContinuation { continuation in
      let shouldResume = lock.withLock {
        guard !runStarted else { return true }
        runStartWaiters.append(continuation)
        return false
      }
      if shouldResume {
        continuation.resume()
      }
    }
  }

  func waitUntilUsageStarted() async {
    await withCheckedContinuation { continuation in
      let shouldResume = lock.withLock {
        guard !usageStarted else { return true }
        usageStartWaiters.append(continuation)
        return false
      }
      if shouldResume {
        continuation.resume()
      }
    }
  }

  func finishUsage() {
    let state: (CheckedContinuation<AgentUsage, Never>?, String) = lock.withLock {
      let continuation = usageContinuation
      usageContinuation = nil
      return (continuation, runInvocation?.agentSlug ?? "manual-analyst")
    }
    state.0?.resume(returning: makeUsage(agentSlug: state.1))
  }

  func capturedSearchInvocations() -> [SearchInvocation] {
    lock.withLock { searchInvocations }
  }

  func capturedMyStackInvocationCount() -> Int {
    lock.withLock { myStackInvocationCount }
  }

  func capturedAddedItemIDs() -> [String] {
    lock.withLock { addedItemIDs }
  }

  func capturedRemovedItemIDs() -> [String] {
    lock.withLock { removedItemIDs }
  }

  func capturedRunInvocation() -> RunInvocation? {
    lock.withLock { runInvocation }
  }

  func capturedCancelCount() -> Int {
    lock.withLock { cancelCount }
  }

  func capturedCancelledRequestIDs() -> [UUID] {
    lock.withLock { cancelledRequestIDs }
  }

  private func completedRunResult() -> AgentRunResult {
    AgentRunResult(
      outcome: .completed,
      answer: "Done",
      events: [
        AgentRunEvent(
          sequence: 1,
          type: "TOOL_CALL_START",
          title: "Tool call",
          detail: "agnes_query",
          rawJSON: #"{"type":"TOOL_CALL_START","name":"agnes_query"}"#
        ),
        AgentRunEvent(
          sequence: 2,
          type: "RUN_FINISHED",
          title: "Run finished",
          detail: nil,
          rawJSON: #"{"type":"RUN_FINISHED"}"#
        ),
      ],
      rawEventsJSON: #"[{"type":"RUN_FINISHED"}]"#
    )
  }
}

private func marketplaceItem(installed: Bool) throws -> MarketplaceItem {
  let json = """
    {
      "id": "curated-foundry-ai/pdf-generator",
      "source": "curated",
      "type": "plugin",
      "name": "pdf-generator",
      "display_name": "PDF Generator",
      "installed": \(installed)
    }
    """
  return try JSONDecoder().decode(MarketplaceItem.self, from: Data(json.utf8))
}

private func makeMarketplaceDetail(installed: Bool) throws -> MarketplaceDetail {
  let json = """
    {
      "source": "curated",
      "marketplace_id": "foundry-ai",
      "plugin_name": "pdf-generator",
      "manifest_name": "pdf-generator",
      "installed": \(installed),
      "installable": true
    }
    """
  return try JSONDecoder().decode(MarketplaceDetail.self, from: Data(json.utf8))
}
