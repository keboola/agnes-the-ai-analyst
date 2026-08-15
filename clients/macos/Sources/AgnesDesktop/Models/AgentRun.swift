import Foundation

/// Terminal state reported by the AG-UI event array emitted from
/// `agnes chat --once --json`.
public enum AgentRunOutcome: String, Equatable, Sendable {
  case completed
  case failed
  case truncated
}

/// One structured AG-UI event retained for the run inspector.
public struct AgentRunEvent: Identifiable, Equatable, Sendable {
  public let sequence: Int
  public let type: String
  public let title: String
  public let detail: String?
  public let rawJSON: String

  public var id: Int { sequence }

  public init(
    sequence: Int,
    type: String,
    title: String,
    detail: String?,
    rawJSON: String
  ) {
    self.sequence = sequence
    self.type = type
    self.title = title
    self.detail = detail
    self.rawJSON = rawJSON
  }

  public var isNotable: Bool {
    type != "TEXT_MESSAGE_CONTENT" && type != "TEXT_MESSAGE_END"
  }
}

/// Complete, inspectable result of one isolated CLI-backed agent run.
public struct AgentRunResult: Equatable, Sendable {
  public let outcome: AgentRunOutcome
  public let answer: String
  public let events: [AgentRunEvent]
  public let rawEventsJSON: String
  public let errorMessage: String?

  public init(
    outcome: AgentRunOutcome,
    answer: String,
    events: [AgentRunEvent],
    rawEventsJSON: String,
    errorMessage: String? = nil
  ) {
    self.outcome = outcome
    self.answer = answer
    self.events = events
    self.rawEventsJSON = rawEventsJSON
    self.errorMessage = errorMessage
  }

  public var toolNames: [String] {
    events.compactMap { event in
      event.type == "TOOL_CALL_START" ? event.detail : nil
    }
  }

  public var notableEvents: [AgentRunEvent] {
    events.filter(\.isNotable)
  }
}

/// UI lifecycle for a locally retained, non-durable run record.
struct AgentRunRecord: Identifiable, Equatable {
  enum State: String, Equatable {
    case running
    case completed
    case failed
    case truncated
    case stopped

    var label: String {
      switch self {
      case .running: "Running"
      case .completed: "Completed"
      case .failed: "Failed"
      case .truncated: "Incomplete"
      case .stopped: "Stopped"
      }
    }
  }

  let id: UUID
  let agentSlug: String
  let prompt: String
  let startedAt: Date
  var finishedAt: Date?
  var state: State
  var result: AgentRunResult?
  var errorMessage: String?

  var duration: TimeInterval? {
    guard let finishedAt else { return nil }
    return max(0, finishedAt.timeIntervalSince(startedAt))
  }
}

/// JSON contract of `agnes agent usage <slug> --json`.
public struct AgentUsage: Codable, Equatable, Sendable {
  public let period: String
  public let agentSlug: String
  public let inputTokens: Int
  public let outputTokens: Int
  public let cacheReadTokens: Int
  public let cacheCreationTokens: Int
  public let totalTokens: Int
  public let budgetLimit: Int?
  public let budgetRemaining: Int?

  enum CodingKeys: String, CodingKey {
    case period
    case agentSlug = "agent_slug"
    case inputTokens = "input_tokens"
    case outputTokens = "output_tokens"
    case cacheReadTokens = "cache_read_tokens"
    case cacheCreationTokens = "cache_creation_tokens"
    case totalTokens = "total_tokens"
    case budgetLimit = "budget_limit"
    case budgetRemaining = "budget_remaining"
  }
}
