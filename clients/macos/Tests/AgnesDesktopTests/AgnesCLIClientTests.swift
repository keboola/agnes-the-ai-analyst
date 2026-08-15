import Foundation
import XCTest

@testable import AgnesDesktop

final class AgnesCLIClientTests: XCTestCase {
  private let executable = URL(fileURLWithPath: "/tmp/agnes")

  func testVersionUsesDirectVersionArgument() async throws {
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: "agnes 1.2.3\n", standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    let version = try await client.version(executable: executable)

    XCTAssertEqual(version, "agnes 1.2.3")
    let invocations = runner.capturedInvocations()
    XCTAssertEqual(
      invocations,
      [
        .init(arguments: ["--version"], environment: expectedEnvironment)
      ])
  }

  func testSearchMarketplaceUsesDirectJSONArgumentsAndDecodesCards() async throws {
    let body = """
      {
        "items": [
          {
            "id": "curated-foundry-ai/pdf-generator",
            "source": "curated",
            "type": "plugin",
            "name": "pdf-generator",
            "display_name": "PDF Generator",
            "installed": true,
            "stack_count": "4"
          }
        ],
        "total": 1
      }
      """
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: body, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    let result = try await client.searchMarketplace(
      executable: executable,
      query: "pdf",
      type: .plugin,
      source: .curated,
      sort: .mostUsed,
      limit: 50
    )

    let invocations = runner.capturedInvocations()
    XCTAssertEqual(
      invocations.first?.arguments,
      [
        "marketplace", "search", "pdf", "--type", "plugin", "--source", "curated", "--sort",
        "most_used", "--limit", "50", "--json",
      ])
    XCTAssertEqual(
      result.total,
      1
    )
    XCTAssertEqual(result.items.first?.source, .curated)
    XCTAssertEqual(result.items.first?.displayName, "PDF Generator")
    XCTAssertEqual(result.items.first?.stackCount, 4)
  }

  func testSearchMarketplaceKeepsHostileLookingQueryAsOneArgumentAndClampsLimit() async throws {
    let query = "pdf; rm -rf / $(whoami) && echo pwned"
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: #"{"items":[],"total":0}"#, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    _ = try await client.searchMarketplace(
      executable: executable,
      query: query,
      type: nil,
      source: nil,
      sort: .recent,
      limit: 10_000
    )

    XCTAssertEqual(
      runner.capturedInvocations().first?.arguments,
      ["marketplace", "search", query, "--sort", "recent", "--limit", "100", "--json"]
    )
  }

  func testMyStackUsesExactJSONArgumentsAndNormalizesInstalledCards() async throws {
    let body = """
      {
        "curated": [
          {
            "marketplace_id": "foundry-ai",
            "marketplace_slug": "foundry",
            "plugin_name": "pdf-generator",
            "manifest_name": "PDF Generator",
            "description": 42,
            "version": 3,
            "enabled": "true",
            "is_system": false,
            "is_required": false
          },
          {
            "marketplace_id": "available",
            "plugin_name": "not-installed",
            "enabled": false
          },
          {
            "marketplace_id": "required",
            "plugin_name": "always-on",
            "enabled": 0,
            "is_required": "yes"
          },
          {
            "marketplace_id": "system",
            "plugin_name": "managed",
            "enabled": false,
            "is_system": 1
          },
          {"marketplace_id":"missing-name", "enabled": true}
        ],
        "store": [
          {
            "entity_id": "01234567-89ab-cdef-0123-456789abcdef",
            "type": "skill",
            "name": "document-review",
            "description": true,
            "category": "Docs",
            "version": 4,
            "owner_username": "ada",
            "invocation_name": "document-review-by-ada",
            "install_count": "8",
            "photo_url": "https://example.test/photo.png",
            "installed_at": 123,
            "visibility_status": "public"
          },
          {"entity_id":"missing-name", "type":"agent"}
        ]
      }
      """
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: body, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    let result = try await client.myStack(executable: executable)

    XCTAssertEqual(
      runner.capturedInvocations().first?.arguments,
      ["my-stack", "show", "--json"]
    )
    XCTAssertEqual(
      result.items.map(\.id),
      [
        "curated-foundry-ai/pdf-generator",
        "curated-required/always-on",
        "curated-system/managed",
        "flea-01234567-89ab-cdef-0123-456789abcdef",
      ]
    )
    XCTAssertEqual(result.items[0].displayName, "PDF Generator")
    XCTAssertEqual(result.items[0].description, "42")
    XCTAssertEqual(result.items[0].version, "3")
    XCTAssertTrue(result.items[1].installed)
    XCTAssertTrue(result.items[1].isRequired)
    XCTAssertTrue(result.items[2].installed)
    XCTAssertTrue(result.items[2].isSystem)
    XCTAssertEqual(result.items[3].source, .flea)
    XCTAssertEqual(result.items[3].type, .skill)
    XCTAssertEqual(result.items[3].displayName, "document-review")
    XCTAssertEqual(result.items[3].description, "true")
    XCTAssertEqual(result.items[3].stackCount, 8)
    XCTAssertEqual(result.items[3].added, "123")
  }

  func testMarketplaceDetailUsesJSONAndDecodesMixedEntries() async throws {
    let body = """
      {
        "source": "flea",
        "entity_id": "abc123",
        "plugin_name": "pdf-extractor",
        "manifest_name": "pdf-extractor-by-ada",
        "installed": "true",
        "installable": 1,
        "stack_count": "3",
        "commands": ["/extract", {"name":"inspect", "description":42}],
        "mcps": [{"name":"local-tools", "type":"stdio"}],
        "files": [{"path":"SKILL.md", "size":"128"}],
        "docs": [{"name":"Guide", "url":"https://example.test/guide"}],
        "use_cases": ["Extract a PDF", {"title":"Inspect", "prompt":"/inspect"}],
        "sample_interaction": {"user":"Help", "assistant":"Done"}
      }
      """
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: body, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    let detail = try await client.marketplaceDetail(
      executable: executable,
      itemID: "flea-abc123; echo unsafe"
    )

    XCTAssertEqual(
      runner.capturedInvocations().first?.arguments,
      ["marketplace", "detail", "--json", "flea-abc123; echo unsafe"]
    )
    XCTAssertEqual(detail.source, .flea)
    XCTAssertEqual(detail.installed, true)
    XCTAssertEqual(detail.installable, true)
    XCTAssertEqual(detail.stackCount, 3)
    XCTAssertEqual(detail.commands.map(\.name), ["/extract", "inspect"])
    XCTAssertEqual(detail.commands.last?.description, "42")
    XCTAssertEqual(detail.files.first, MarketplaceFile(path: "SKILL.md", size: 128))
    XCTAssertEqual(detail.useCases.map(\.title), ["Extract a PDF", "Inspect"])
  }

  func testMarketplaceAddAndRemovePassItemIDAsOneArgumentAndReturnMessage() async throws {
    let itemID = "curated-foundry-ai/pdf-generator; touch /tmp/nope"
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: "Added to your stack.\n", standardError: "")),
      .success(.init(exitCode: 0, standardOutput: "Removed from your stack.\n", standardError: "")),
    ])
    let client = AgnesCLIClient(runner: runner)

    let addMessage = try await client.addMarketplaceItem(executable: executable, itemID: itemID)
    let removeMessage = try await client.removeMarketplaceItem(
      executable: executable, itemID: itemID)

    XCTAssertEqual(addMessage, "Added to your stack.")
    XCTAssertEqual(removeMessage, "Removed from your stack.")
    XCTAssertEqual(
      runner.capturedInvocations().map(\.arguments),
      [
        ["marketplace", "add", itemID],
        ["marketplace", "remove", itemID],
      ])
  }

  func testMarketplaceReadAndWriteSurfaceNonzeroCommandErrors() async {
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 1, standardOutput: "", standardError: "not signed in")),
      .success(.init(exitCode: 2, standardOutput: "", standardError: "forbidden")),
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(
      try await client.searchMarketplace(
        executable: executable,
        query: nil,
        type: nil,
        source: .curated,
        sort: .recent,
        limit: 24
      )
    ) { error in
      XCTAssertEqual(
        error as? AgnesCLIError,
        .commandFailed(exitCode: 1, standardError: "not signed in")
      )
    }
    await assertThrowsErrorAsync(
      try await client.addMarketplaceItem(executable: executable, itemID: "curated-mkt/plugin")
    ) { error in
      XCTAssertEqual(
        error as? AgnesCLIError, .commandFailed(exitCode: 2, standardError: "forbidden"))
    }
  }

  func testMarketplaceSearchReportsInvalidJSON() async {
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: "not json", standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(
      try await client.searchMarketplace(
        executable: executable,
        query: nil,
        type: nil,
        source: .flea,
        sort: .recent,
        limit: 24
      )
    ) { error in
      guard let cliError = error as? AgnesCLIError else {
        return XCTFail("Expected AgnesCLIError, got \(error)")
      }
      guard case .invalidJSON = cliError else {
        return XCTFail("Expected invalid JSON error, got \(error)")
      }
    }
  }

  func testMyStackRejectsInvalidEnvelopeSchema() async {
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: #"{"curated":{},"store":[]}"#, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(try await client.myStack(executable: executable)) { error in
      guard case .invalidJSON = error as? AgnesCLIError else {
        return XCTFail("Expected invalid JSON error, got \(error)")
      }
    }
  }

  func testMyStackRejectsAnEmptyEnvelopeInsteadOfSilentlyShowingAnEmptyStack() async {
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: #"{}"#, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(try await client.myStack(executable: executable)) { error in
      guard case .invalidJSON = error as? AgnesCLIError else {
        return XCTFail("Expected invalid JSON error, got \(error)")
      }
    }
  }

  func testMarketplaceSearchRejectsAnInvalidItemsEnvelope() async {
    let runner = StubRunner(outputs: [
      .success(
        .init(
          exitCode: 0,
          standardOutput: #"{"items":{},"total":1}"#,
          standardError: ""
        ))
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(
      try await client.searchMarketplace(
        executable: executable,
        query: nil,
        type: nil,
        source: nil,
        sort: .recent,
        limit: 24
      )
    ) { error in
      guard case .invalidJSON = error as? AgnesCLIError else {
        return XCTFail("Expected invalid JSON error, got \(error)")
      }
    }
  }

  func testAskKeepsHostileLookingPromptAsOneArgument() async throws {
    let prompt = "hello; rm -rf / $(whoami) && echo pwned"
    let events = """
      [
        {"type":"RUN_STARTED"},
        {"type":"TEXT_MESSAGE_CONTENT","delta":"Safe answer"},
        {"type":"RUN_FINISHED"}
      ]
      """
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 0, standardOutput: events, standardError: ""))
    ])
    let client = AgnesCLIClient(runner: runner)

    let result = try await client.ask(executable: executable, agentSlug: "sales", prompt: prompt)

    XCTAssertEqual(result.answer, "Safe answer")
    let invocations = runner.capturedInvocations()
    XCTAssertEqual(
      invocations.first?.arguments,
      [
        "chat", "--agent", "sales", "--once", prompt, "--json",
      ])
    XCTAssertEqual(invocations.first?.arguments.count, 6)
  }

  func testAskRejectsNonZeroExitWithoutAttemptingToParseOutput() async {
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 2, standardOutput: "not json", standardError: "not logged in"))
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(
      try await client.ask(executable: executable, agentSlug: "sales", prompt: "hi")
    ) { error in
      XCTAssertEqual(
        error as? AgnesCLIError, .commandFailed(exitCode: 2, standardError: "not logged in"))
    }
  }

  func testCancelForwardsToTheActiveProcessRunner() async throws {
    let runner = BlockingRunner()
    let client = AgnesCLIClient(runner: runner)
    let ask = Task {
      try await client.ask(executable: self.executable, agentSlug: "sales", prompt: "hi")
    }
    await runner.waitUntilStarted()

    client.cancel()

    await assertThrowsErrorAsync(try await ask.value) { error in
      XCTAssertEqual(
        error as? AgnesCLIError, .commandFailed(exitCode: 130, standardError: "cancelled"))
    }
    let cancelCount = runner.capturedCancelCount()
    XCTAssertEqual(cancelCount, 1)
  }
}

final class AgnesCLIOutputParserTests: XCTestCase {
  func testParserConcatenatesDeltasAndCollectsTools() throws {
    let text = """
      [
        {"type":"RUN_STARTED"},
        {"type":"TEXT_MESSAGE_CONTENT","delta":"Hello"},
        {"type":"TOOL_CALL_START","name":"agnes_query"},
        {"type":"TEXT_MESSAGE_CONTENT","delta":" world"},
        {"type":"RUN_FINISHED"}
      ]
      """

    XCTAssertEqual(
      try AgnesCLIOutputParser.parseAsk(text),
      AskResult(answer: "Hello world", toolNames: ["agnes_query"])
    )
  }

  func testParserFallsBackToFinalContentWhenNoVisibleDeltaArrived() throws {
    let text = """
      [
        {"type":"TEXT_MESSAGE_CONTENT","delta":" \\n"},
        {"type":"TEXT_MESSAGE_END","content":"Final answer"},
        {"type":"RUN_FINISHED"}
      ]
      """

    XCTAssertEqual(
      try AgnesCLIOutputParser.parseAsk(text),
      AskResult(answer: "Final answer", toolNames: [])
    )
  }

  func testParserStripsOnlyACompleteTrailingNextActionsBlock() throws {
    let text = """
      [
        {"type":"TEXT_MESSAGE_CONTENT","delta":"Answer.\\n\\n```next_actions\\n- Follow up\\n```"},
        {"type":"RUN_FINISHED"}
      ]
      """

    XCTAssertEqual(try AgnesCLIOutputParser.parseAsk(text).answer, "Answer.")
  }

  func testParserRejectsRunErrorAndPreservesPartialAnswer() {
    let text = """
      [
        {"type":"TEXT_MESSAGE_CONTENT","delta":"Partial"},
        {"type":"RUN_ERROR","message":"tool failed"},
        {"type":"RUN_FINISHED"}
      ]
      """

    XCTAssertThrowsError(try AgnesCLIOutputParser.parseAsk(text)) { error in
      XCTAssertEqual(
        error as? AgnesCLIError,
        .runFailed(message: "tool failed", partialAnswer: "Partial")
      )
    }
  }

  func testAskPreservesRunErrorFromValidNonzeroJSONOutput() async {
    let events = """
      [
        {"type":"TEXT_MESSAGE_CONTENT","delta":"Partial"},
        {"type":"RUN_ERROR","message":"tool failed"}
      ]
      """
    let runner = StubRunner(outputs: [
      .success(.init(exitCode: 1, standardOutput: events, standardError: "CLI error"))
    ])
    let client = AgnesCLIClient(runner: runner)

    await assertThrowsErrorAsync(
      try await client.ask(
        executable: URL(fileURLWithPath: "/tmp/agnes"),
        agentSlug: "sales",
        prompt: "hi"
      )
    ) { error in
      XCTAssertEqual(
        error as? AgnesCLIError,
        .runFailed(message: "tool failed", partialAnswer: "Partial")
      )
    }
  }

  func testParserRejectsMissingRunFinished() {
    let text = """
      [{"type":"TEXT_MESSAGE_CONTENT","delta":"Partial"}]
      """

    XCTAssertThrowsError(try AgnesCLIOutputParser.parseAsk(text)) { error in
      XCTAssertEqual(error as? AgnesCLIError, .truncatedStream(partialAnswer: "Partial"))
    }
  }
}

final class ExecutableLocatorTests: XCTestCase {
  func testPreferredPathWinsOverPathAndGUISearchDirectories() throws {
    let root = try makeTemporaryDirectory()
    defer { try? FileManager.default.removeItem(at: root) }
    let preferred = try makeExecutable(named: "preferred-agnes", in: root)
    let pathDirectory = root.appendingPathComponent("path")
    try FileManager.default.createDirectory(at: pathDirectory, withIntermediateDirectories: true)
    _ = try makeExecutable(named: "agnes", in: pathDirectory)

    let located = ExecutableLocator.locate(
      preferredPath: preferred.path,
      environment: ["PATH": pathDirectory.path],
      fileManager: .default,
      homeDirectory: root,
      guiSearchDirectories: []
    )

    XCTAssertEqual(located?.path, preferred.path)
  }

  func testPathWinsBeforeGUIFallbackAndDefaultIncludesCommonMacPaths() throws {
    let root = try makeTemporaryDirectory()
    defer { try? FileManager.default.removeItem(at: root) }
    let pathDirectory = root.appendingPathComponent("path")
    let guiDirectory = root.appendingPathComponent("gui")
    try FileManager.default.createDirectory(at: pathDirectory, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: guiDirectory, withIntermediateDirectories: true)
    let onPath = try makeExecutable(named: "agnes", in: pathDirectory)
    _ = try makeExecutable(named: "agnes", in: guiDirectory)

    let located = ExecutableLocator.locate(
      preferredPath: nil,
      environment: ["PATH": pathDirectory.path],
      fileManager: .default,
      homeDirectory: root,
      guiSearchDirectories: [guiDirectory.path]
    )

    XCTAssertEqual(located?.path, onPath.path)
    XCTAssertTrue(ExecutableLocator.defaultGUISearchDirectories.contains("/opt/homebrew/bin"))
    XCTAssertTrue(ExecutableLocator.defaultGUISearchDirectories.contains("/usr/local/bin"))
  }

  private func makeTemporaryDirectory() throws -> URL {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    return directory
  }

  private func makeExecutable(named name: String, in directory: URL) throws -> URL {
    let file = directory.appendingPathComponent(name)
    try "#!/bin/sh\\nexit 0\\n".write(to: file, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
    return file
  }
}

private let expectedEnvironment = [
  "NO_COLOR": "1",
  "AGNES_NO_UPDATE_CHECK": "1",
]

private final class StubRunner: AgnesCLIProcessRunning, @unchecked Sendable {
  struct Invocation: Equatable {
    let arguments: [String]
    let environment: [String: String]
  }

  private var queuedOutputs: [Result<AgnesCLIProcessOutput, Error>]
  private var invocations: [Invocation] = []
  private let lock = NSLock()

  init(outputs: [Result<AgnesCLIProcessOutput, Error>]) {
    queuedOutputs = outputs
  }

  func run(
    executable: URL,
    arguments: [String],
    environment: [String: String]
  ) async throws -> AgnesCLIProcessOutput {
    let next = lock.withLock {
      invocations.append(.init(arguments: arguments, environment: environment))
      return queuedOutputs.removeFirst()
    }
    return try next.get()
  }

  func cancel() {}

  func capturedInvocations() -> [Invocation] {
    lock.withLock { invocations }
  }
}

private final class BlockingRunner: AgnesCLIProcessRunning, @unchecked Sendable {
  private let lock = NSLock()
  private var continuation: CheckedContinuation<AgnesCLIProcessOutput, Never>?
  private var cancelCount = 0
  private var started = false
  private var startWaiters: [CheckedContinuation<Void, Never>] = []

  func run(
    executable: URL,
    arguments: [String],
    environment: [String: String]
  ) async throws -> AgnesCLIProcessOutput {
    return await withCheckedContinuation { continuation in
      let waiters = lock.withLock {
        self.continuation = continuation
        started = true
        let waiters = startWaiters
        startWaiters.removeAll()
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
      let continuation = self.continuation
      self.continuation = nil
      return continuation
    }
    continuation?.resume(
      returning: .init(exitCode: 130, standardOutput: "", standardError: "cancelled"))
  }

  func waitUntilStarted() async {
    await withCheckedContinuation { continuation in
      let shouldResume = lock.withLock {
        guard !started else { return true }
        startWaiters.append(continuation)
        return false
      }
      if shouldResume {
        continuation.resume()
      }
    }
  }

  func capturedCancelCount() -> Int {
    lock.withLock { cancelCount }
  }
}

extension XCTestCase {
  fileprivate func assertThrowsErrorAsync<T>(
    _ expression: @autoclosure () async throws -> T,
    _ handler: (Error) -> Void
  ) async {
    do {
      _ = try await expression()
      XCTFail("Expected expression to throw")
    } catch {
      handler(error)
    }
  }
}
