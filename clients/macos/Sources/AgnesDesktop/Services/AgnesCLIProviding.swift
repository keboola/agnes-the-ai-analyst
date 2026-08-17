import Foundation

/// The subprocess boundary used by the desktop UI.
///
/// Commands are deliberately narrow and use JSON-capable Agnes CLI commands
/// only. The UI never reads the Agnes token or calls the server directly.
public protocol AgnesCLIProviding: Sendable {
  func version(executable: URL) async throws -> String
  func searchMarketplace(
    executable: URL,
    query: String?,
    type: MarketplaceItemType?,
    source: MarketplaceSource?,
    sort: MarketplaceSort,
    limit: Int
  ) async throws -> MarketplaceSearchResult
  func myStack(executable: URL) async throws -> MarketplaceStackResult
  func marketplaceDetail(executable: URL, itemID: String) async throws -> MarketplaceDetail
  func addMarketplaceItem(executable: URL, itemID: String) async throws -> String
  func removeMarketplaceItem(executable: URL, itemID: String) async throws -> String
  func agentUsage(executable: URL, agentSlug: String) async throws -> AgentUsage
  func runAgent(
    executable: URL,
    requestID: UUID,
    agentSlug: String,
    prompt: String
  ) async throws -> AgentRunResult
  func cancel(requestID: UUID)
  func cancelAll()
}

public enum AgnesCLIError: Error, LocalizedError, Equatable, Sendable {
  case commandFailed(exitCode: Int32, standardError: String)
  case invalidJSON(String)
  case outputLimitExceeded(stream: String, limitBytes: Int)

  public var errorDescription: String? {
    switch self {
    case .commandFailed(let exitCode, let standardError):
      let detail = standardError.trimmingCharacters(in: .whitespacesAndNewlines)
      return detail.isEmpty
        ? "Agnes CLI exited with status \(exitCode)."
        : "Agnes CLI exited with status \(exitCode): \(detail)"
    case .invalidJSON(let message):
      return "Agnes CLI returned invalid JSON: \(message)"
    case .outputLimitExceeded(let stream, let limitBytes):
      return
        "Agnes CLI \(stream) exceeded the desktop safety limit of \(limitBytes.formatted()) bytes."
    }
  }
}

/// Concrete CLI adapter. It invokes executable URLs with argument arrays, so
/// a prompt is always one argument and can never be interpreted as shell code.
public final class AgnesCLIClient: AgnesCLIProviding, @unchecked Sendable {
  private let runner: any AgnesCLIProcessRunning

  public init() {
    runner = SystemAgnesCLIProcessRunner()
  }

  init(runner: any AgnesCLIProcessRunning) {
    self.runner = runner
  }

  public func version(executable: URL) async throws -> String {
    let output = try await run(executable: executable, arguments: ["--version"])
    return output.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines)
  }

  public func searchMarketplace(
    executable: URL,
    query: String? = nil,
    type: MarketplaceItemType? = nil,
    source: MarketplaceSource? = nil,
    sort: MarketplaceSort = .recent,
    limit: Int = 24
  ) async throws -> MarketplaceSearchResult {
    var arguments = ["marketplace", "search"]
    if let query, !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
      arguments.append(query)
    }
    if let type {
      arguments.append(contentsOf: ["--type", type.rawValue])
    }
    if let source {
      arguments.append(contentsOf: ["--source", source.rawValue])
    }
    arguments.append(contentsOf: ["--sort", sort.rawValue])
    arguments.append(contentsOf: ["--limit", String(min(max(limit, 1), 100)), "--json"])

    let output = try await run(executable: executable, arguments: arguments)
    return try AgnesCLIOutputParser.parseMarketplaceSearch(output.standardOutput)
  }

  public func myStack(executable: URL) async throws -> MarketplaceStackResult {
    let output = try await run(executable: executable, arguments: ["my-stack", "show", "--json"])
    return try AgnesCLIOutputParser.parseMarketplaceStack(output.standardOutput)
  }

  public func marketplaceDetail(executable: URL, itemID: String) async throws -> MarketplaceDetail {
    let output = try await run(
      executable: executable,
      arguments: ["marketplace", "detail", "--json", itemID]
    )
    return try AgnesCLIOutputParser.parseMarketplaceDetail(output.standardOutput)
  }

  public func addMarketplaceItem(executable: URL, itemID: String) async throws -> String {
    let output = try await run(executable: executable, arguments: ["marketplace", "add", itemID])
    return output.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines)
  }

  public func removeMarketplaceItem(executable: URL, itemID: String) async throws -> String {
    let output = try await run(
      executable: executable, arguments: ["marketplace", "remove", itemID])
    return output.standardOutput.trimmingCharacters(in: .whitespacesAndNewlines)
  }

  public func agentUsage(executable: URL, agentSlug: String) async throws -> AgentUsage {
    let output = try await run(
      executable: executable,
      arguments: ["agent", "usage", agentSlug, "--json"]
    )
    return try AgnesCLIOutputParser.parseAgentUsage(output.standardOutput)
  }

  public func runAgent(
    executable: URL,
    requestID: UUID,
    agentSlug: String,
    prompt: String
  ) async throws -> AgentRunResult {
    let output = try await runner.run(
      requestID: requestID,
      executable: executable,
      arguments: ["chat", "--agent", agentSlug, "--once", prompt, "--json"],
      environment: [
        "NO_COLOR": "1",
        "AGNES_NO_UPDATE_CHECK": "1",
      ]
    )
    if output.standardOutputTruncated {
      return AgentRunResult(
        outcome: .truncated,
        answer: "",
        events: [],
        rawEventsJSON: output.standardOutput,
        errorMessage: AgnesCLIError.outputLimitExceeded(
          stream: "standard output",
          limitBytes: SystemAgnesCLIProcessRunner.defaultStandardOutputLimit
        ).localizedDescription
      )
    }
    if output.standardErrorTruncated {
      throw AgnesCLIError.outputLimitExceeded(
        stream: "standard error",
        limitBytes: SystemAgnesCLIProcessRunner.defaultStandardErrorLimit
      )
    }
    do {
      let result = try AgnesCLIOutputParser.parseAgentRun(output.standardOutput)
      if output.exitCode != 0, result.outcome == .completed {
        throw AgnesCLIError.commandFailed(
          exitCode: output.exitCode,
          standardError: output.standardError
        )
      }
      return result
    } catch {
      guard output.exitCode != 0 else { throw error }
      throw AgnesCLIError.commandFailed(
        exitCode: output.exitCode,
        standardError: output.standardError
      )
    }
  }

  public func cancel(requestID: UUID) {
    runner.cancel(requestID: requestID)
  }

  public func cancelAll() {
    runner.cancelAll()
  }

  private func run(executable: URL, arguments: [String]) async throws -> AgnesCLIProcessOutput {
    let output = try await runner.run(
      requestID: UUID(),
      executable: executable,
      arguments: arguments,
      environment: [
        "NO_COLOR": "1",
        "AGNES_NO_UPDATE_CHECK": "1",
      ]
    )
    if output.standardOutputTruncated {
      throw AgnesCLIError.outputLimitExceeded(
        stream: "standard output",
        limitBytes: SystemAgnesCLIProcessRunner.defaultStandardOutputLimit
      )
    }
    if output.standardErrorTruncated {
      throw AgnesCLIError.outputLimitExceeded(
        stream: "standard error",
        limitBytes: SystemAgnesCLIProcessRunner.defaultStandardErrorLimit
      )
    }
    guard output.exitCode == 0 else {
      throw AgnesCLIError.commandFailed(
        exitCode: output.exitCode,
        standardError: output.standardError
      )
    }
    return output
  }
}

struct AgnesCLIProcessOutput: Sendable {
  let exitCode: Int32
  let standardOutput: String
  let standardError: String
  let standardOutputTruncated: Bool
  let standardErrorTruncated: Bool

  init(
    exitCode: Int32,
    standardOutput: String,
    standardError: String,
    standardOutputTruncated: Bool = false,
    standardErrorTruncated: Bool = false
  ) {
    self.exitCode = exitCode
    self.standardOutput = standardOutput
    self.standardError = standardError
    self.standardOutputTruncated = standardOutputTruncated
    self.standardErrorTruncated = standardErrorTruncated
  }
}

protocol AgnesCLIProcessRunning: Sendable {
  func run(
    requestID: UUID,
    executable: URL,
    arguments: [String],
    environment: [String: String]
  ) async throws -> AgnesCLIProcessOutput
  func cancel(requestID: UUID)
  func cancelAll()
}

enum AgnesCLIOutputParser {
  static func parseMarketplaceSearch(_ text: String) throws -> MarketplaceSearchResult {
    do {
      return try JSONDecoder().decode(MarketplaceSearchResult.self, from: Data(text.utf8))
    } catch {
      throw AgnesCLIError.invalidJSON(error.localizedDescription)
    }
  }

  static func parseMarketplaceDetail(_ text: String) throws -> MarketplaceDetail {
    do {
      return try JSONDecoder().decode(MarketplaceDetail.self, from: Data(text.utf8))
    } catch {
      throw AgnesCLIError.invalidJSON(error.localizedDescription)
    }
  }

  static func parseMarketplaceStack(_ text: String) throws -> MarketplaceStackResult {
    do {
      return try JSONDecoder().decode(MarketplaceStackResult.self, from: Data(text.utf8))
    } catch {
      throw AgnesCLIError.invalidJSON(error.localizedDescription)
    }
  }

  static func parseAgentUsage(_ text: String) throws -> AgentUsage {
    do {
      return try JSONDecoder().decode(AgentUsage.self, from: Data(text.utf8))
    } catch {
      throw AgnesCLIError.invalidJSON(error.localizedDescription)
    }
  }

  static func parseAgentRun(_ text: String) throws -> AgentRunResult {
    let events: [[String: Any]]
    let parsedObject: Any
    do {
      parsedObject = try JSONSerialization.jsonObject(with: Data(text.utf8))
      guard let parsed = parsedObject as? [[String: Any]] else {
        throw AgnesCLIError.invalidJSON("expected an array of AG-UI events")
      }
      events = parsed
    } catch let error as AgnesCLIError {
      throw error
    } catch {
      throw AgnesCLIError.invalidJSON(error.localizedDescription)
    }

    var deltas = ""
    var finalContent: String?
    var runError: String?
    var finished = false
    var inspectedEvents: [AgentRunEvent] = []

    for (offset, event) in events.enumerated() {
      let type = (event["type"] as? String) ?? "UNKNOWN"
      switch type {
      case "TEXT_MESSAGE_CONTENT":
        if let delta = event["delta"] as? String {
          deltas += delta
        }
      case "TEXT_MESSAGE_END":
        if let content = event["content"] as? String, !content.isEmpty {
          finalContent = content
        }
      case "RUN_ERROR":
        runError = (event["message"] as? String) ?? "run error"
      case "RUN_FINISHED":
        finished = true
      default:
        break
      }
      inspectedEvents.append(inspectedEvent(sequence: offset + 1, type: type, event: event))
    }

    let rawAnswer =
      deltas.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      ? (finalContent ?? deltas)
      : deltas
    let answer = stripCompleteNextActionsTrailer(from: rawAnswer)

    let outcome: AgentRunOutcome
    let errorMessage: String?
    if let runError {
      outcome = .failed
      errorMessage = runError
    } else if finished {
      outcome = .completed
      errorMessage = nil
    } else {
      outcome = .truncated
      errorMessage = "The event stream ended without RUN_FINISHED or RUN_ERROR."
    }

    return AgentRunResult(
      outcome: outcome,
      answer: answer,
      events: inspectedEvents,
      rawEventsJSON: prettyJSON(parsedObject),
      errorMessage: errorMessage
    )
  }

  private static func inspectedEvent(
    sequence: Int,
    type: String,
    event: [String: Any]
  ) -> AgentRunEvent {
    let title: String
    let detail: String?

    switch type {
    case "RUN_STARTED":
      title = "Run started"
      detail = nil
    case "TEXT_MESSAGE_CONTENT":
      title = "Answer streamed"
      detail = compact(event["delta"])
    case "TEXT_MESSAGE_END":
      title = "Answer completed"
      if let content = event["content"] as? String {
        detail = "\(content.count) characters"
      } else {
        detail = nil
      }
    case "TOOL_CALL_START":
      title = "Tool call"
      detail = compact(event["name"]) ?? "tool"
    case "TOOL_CALL_END":
      title = "Tool result"
      detail = compact(event["result"])
    case "RUN_FINISHED":
      title = "Run finished"
      detail = nil
    case "RUN_ERROR":
      title = "Run error"
      detail = compact(event["message"]) ?? "Unknown run error"
    default:
      title = type.replacingOccurrences(of: "_", with: " ").capitalized
      detail = nil
    }

    return AgentRunEvent(
      sequence: sequence,
      type: type,
      title: title,
      detail: detail,
      rawJSON: prettyJSON(event)
    )
  }

  private static func compact(_ value: Any?) -> String? {
    guard let value else { return nil }
    let raw: String
    if let string = value as? String {
      raw = string
    } else {
      raw = prettyJSON(value).replacingOccurrences(of: "\n", with: " ")
    }
    let collapsed = raw.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    guard !collapsed.isEmpty else { return nil }
    return collapsed.count > 220 ? String(collapsed.prefix(217)) + "…" : collapsed
  }

  private static func prettyJSON(_ value: Any) -> String {
    guard JSONSerialization.isValidJSONObject(value),
      let data = try? JSONSerialization.data(
        withJSONObject: value,
        options: [.prettyPrinted, .sortedKeys]
      )
    else {
      return String(describing: value)
    }
    return String(decoding: data, as: UTF8.self)
  }

  /// Removes only a complete *trailing* next_actions fenced block. An
  /// unfinished fence remains visible rather than silently hiding content.
  private static func stripCompleteNextActionsTrailer(from text: String) -> String {
    let opener = "```next_actions"
    var searchStart = text.startIndex

    while let opening = text.range(
      of: opener, options: .caseInsensitive, range: searchStart..<text.endIndex)
    {
      var headerEnd = opening.upperBound
      while headerEnd < text.endIndex,
        text[headerEnd] == " " || text[headerEnd] == "\t" || text[headerEnd] == "\r"
      {
        headerEnd = text.index(after: headerEnd)
      }
      guard headerEnd < text.endIndex, text[headerEnd] == "\n" else {
        searchStart = opening.upperBound
        continue
      }

      let bodyStart = text.index(after: headerEnd)
      guard let closing = text.range(of: "```", range: bodyStart..<text.endIndex) else {
        searchStart = opening.upperBound
        continue
      }
      let suffix = text[closing.upperBound...]
      guard suffix.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        searchStart = opening.upperBound
        continue
      }
      return String(text[..<opening.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    return text
  }
}
