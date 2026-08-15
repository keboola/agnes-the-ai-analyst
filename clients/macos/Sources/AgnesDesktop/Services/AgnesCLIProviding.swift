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
  func ask(executable: URL, agentSlug: String, prompt: String) async throws -> AskResult
  func cancel()
}

public enum AgnesCLIError: Error, LocalizedError, Equatable, Sendable {
  case commandFailed(exitCode: Int32, standardError: String)
  case invalidJSON(String)
  case runFailed(message: String, partialAnswer: String?)
  case truncatedStream(partialAnswer: String?)

  public var errorDescription: String? {
    switch self {
    case .commandFailed(let exitCode, let standardError):
      let detail = standardError.trimmingCharacters(in: .whitespacesAndNewlines)
      return detail.isEmpty
        ? "Agnes CLI exited with status \(exitCode)."
        : "Agnes CLI exited with status \(exitCode): \(detail)"
    case .invalidJSON(let message):
      return "Agnes CLI returned invalid JSON: \(message)"
    case .runFailed(let message, let partialAnswer):
      guard let partialAnswer, !partialAnswer.isEmpty else {
        return "Agent run failed: \(message)"
      }
      return "Agent run failed: \(message)\n\nPartial answer:\n\(partialAnswer)"
    case .truncatedStream(let partialAnswer):
      guard let partialAnswer, !partialAnswer.isEmpty else {
        return "The agent stream ended without a completion event."
      }
      return
        "The agent stream ended without a completion event.\n\nPartial answer:\n\(partialAnswer)"
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

  public func ask(executable: URL, agentSlug: String, prompt: String) async throws -> AskResult {
    let output = try await runner.run(
      executable: executable,
      arguments: ["chat", "--agent", agentSlug, "--once", prompt, "--json"],
      environment: [
        "NO_COLOR": "1",
        "AGNES_NO_UPDATE_CHECK": "1",
      ]
    )
    do {
      let result = try AgnesCLIOutputParser.parseAsk(output.standardOutput)
      guard output.exitCode == 0 else {
        throw AgnesCLIError.commandFailed(
          exitCode: output.exitCode,
          standardError: output.standardError
        )
      }
      return result
    } catch let error as AgnesCLIError {
      // The CLI uses a non-zero exit for RUN_ERROR or a truncated
      // stream. Keep those structured diagnostics and partial answer.
      switch error {
      case .runFailed, .truncatedStream:
        throw error
      case .commandFailed, .invalidJSON:
        guard output.exitCode != 0 else { throw error }
        throw AgnesCLIError.commandFailed(
          exitCode: output.exitCode,
          standardError: output.standardError
        )
      }
    }
  }

  public func cancel() {
    runner.cancel()
  }

  private func run(executable: URL, arguments: [String]) async throws -> AgnesCLIProcessOutput {
    let output = try await runner.run(
      executable: executable,
      arguments: arguments,
      environment: [
        "NO_COLOR": "1",
        "AGNES_NO_UPDATE_CHECK": "1",
      ]
    )
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
}

protocol AgnesCLIProcessRunning: Sendable {
  func run(
    executable: URL,
    arguments: [String],
    environment: [String: String]
  ) async throws -> AgnesCLIProcessOutput
  func cancel()
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

  static func parseAsk(_ text: String) throws -> AskResult {
    let events: [[String: Any]]
    do {
      guard let parsed = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [[String: Any]]
      else {
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
    var toolNames: [String] = []
    var runError: String?
    var finished = false

    for event in events {
      guard let type = event["type"] as? String else { continue }
      switch type {
      case "TEXT_MESSAGE_CONTENT":
        if let delta = event["delta"] as? String {
          deltas += delta
        }
      case "TEXT_MESSAGE_END":
        if let content = event["content"] as? String, !content.isEmpty {
          finalContent = content
        }
      case "TOOL_CALL_START":
        if let name = event["name"] as? String, !name.isEmpty {
          toolNames.append(name)
        }
      case "RUN_ERROR":
        runError = (event["message"] as? String) ?? "run error"
      case "RUN_FINISHED":
        finished = true
      default:
        continue
      }
    }

    let rawAnswer =
      deltas.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      ? (finalContent ?? deltas)
      : deltas
    let answer = stripCompleteNextActionsTrailer(from: rawAnswer)

    if let runError {
      throw AgnesCLIError.runFailed(
        message: runError,
        partialAnswer: answer.isEmpty ? nil : answer
      )
    }
    guard finished else {
      throw AgnesCLIError.truncatedStream(partialAnswer: answer.isEmpty ? nil : answer)
    }
    return AskResult(answer: answer, toolNames: toolNames)
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
