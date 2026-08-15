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

  func testManualAgentSlugAskCanBeCancelled() async throws {
    let executable = try makeExecutable()
    defer { try? FileManager.default.removeItem(at: executable.deletingLastPathComponent()) }
    let cli = ModelTestCLI(blockAsk: true)
    let model = AppModel(cli: cli)
    model.preferredExecutablePath = executable.path
    await model.bootstrap()
    model.agentSlug = "manual-analyst"
    model.prompt = "Keep working"

    XCTAssertTrue(model.canAsk)
    model.submitPrompt()
    await cli.waitUntilAskStarted()
    model.stopRequest()

    XCTAssertEqual(cli.capturedCancelCount(), 1)
    XCTAssertTrue(model.isStopping)
    for _ in 0..<20 where model.isAsking {
      await Task.yield()
    }
    XCTAssertFalse(model.isAsking)
    XCTAssertEqual(model.askError, "Request stopped.")
    XCTAssertEqual(cli.capturedAskInvocation()?.agentSlug, "manual-analyst")
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

  struct AskInvocation: Equatable {
    let agentSlug: String
    let prompt: String
  }

  private let lock = NSLock()
  private let blockAsk: Bool
  private let versionError: Error?
  private var installed: Bool
  private var nextSearchError: Error?
  private var nextMyStackError: Error?
  private var searchInvocations: [SearchInvocation] = []
  private var myStackInvocationCount = 0
  private var addedItemIDs: [String] = []
  private var removedItemIDs: [String] = []
  private var askInvocation: AskInvocation?
  private var askContinuation: CheckedContinuation<AskResult, Error>?
  private var askStarted = false
  private var askStartWaiters: [CheckedContinuation<Void, Never>] = []
  private var cancelCount = 0

  init(
    initiallyInstalled: Bool = false,
    blockAsk: Bool = false,
    versionError: Error? = nil
  ) {
    installed = initiallyInstalled
    self.blockAsk = blockAsk
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

  func ask(executable: URL, agentSlug: String, prompt: String) async throws -> AskResult {
    lock.withLock { askInvocation = .init(agentSlug: agentSlug, prompt: prompt) }
    guard blockAsk else { return AskResult(answer: "Done", toolNames: []) }
    return try await withCheckedThrowingContinuation { continuation in
      let waiters = lock.withLock {
        askContinuation = continuation
        askStarted = true
        let waiters = askStartWaiters
        askStartWaiters.removeAll()
        return waiters
      }
      for waiter in waiters {
        waiter.resume()
      }
    }
  }

  func cancel() {
    let continuation = lock.withLock {
      cancelCount += 1
      let continuation = askContinuation
      askContinuation = nil
      return continuation
    }
    continuation?.resume(throwing: CancellationError())
  }

  func failNextMarketplaceSearch(with error: Error) {
    lock.withLock { nextSearchError = error }
  }

  func failNextMyStack(with error: Error) {
    lock.withLock { nextMyStackError = error }
  }

  func waitUntilAskStarted() async {
    await withCheckedContinuation { continuation in
      let shouldResume = lock.withLock {
        guard !askStarted else { return true }
        askStartWaiters.append(continuation)
        return false
      }
      if shouldResume {
        continuation.resume()
      }
    }
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

  func capturedAskInvocation() -> AskInvocation? {
    lock.withLock { askInvocation }
  }

  func capturedCancelCount() -> Int {
    lock.withLock { cancelCount }
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
