import Foundation

/// The human-facing result of one Agnes agent turn.
public struct AskResult: Equatable, Sendable {
  public let answer: String
  public let toolNames: [String]

  public init(answer: String, toolNames: [String]) {
    self.answer = answer
    self.toolNames = toolNames
  }
}
