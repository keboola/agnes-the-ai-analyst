import Foundation

/// Foundation `Process` implementation with request-scoped cancellation.
///
/// Marketplace reads and an agent run may execute concurrently. A Stop action
/// addresses only the UUID assigned to that run, so a delayed SIGINT cannot
/// hit a later or unrelated CLI process.
final class SystemAgnesCLIProcessRunner: AgnesCLIProcessRunning, @unchecked Sendable {
  static let defaultStandardOutputLimit = 8 * 1_024 * 1_024
  static let defaultStandardErrorLimit = 1 * 1_024 * 1_024

  private let lock = NSLock()
  private var activeProcesses: [UUID: ProcessSlot] = [:]
  private var pendingCancellationIDs: Set<UUID> = []
  private let standardOutputLimit: Int
  private let standardErrorLimit: Int

  init(
    standardOutputLimit: Int = defaultStandardOutputLimit,
    standardErrorLimit: Int = defaultStandardErrorLimit
  ) {
    precondition(standardOutputLimit > 0 && standardErrorLimit > 0)
    self.standardOutputLimit = standardOutputLimit
    self.standardErrorLimit = standardErrorLimit
  }

  func run(
    requestID: UUID,
    executable: URL,
    arguments: [String],
    environment: [String: String]
  ) async throws -> AgnesCLIProcessOutput {
    try await withCheckedThrowingContinuation { continuation in
      let process = Process()
      let slot = ProcessSlot(process: process)
      let stdout = Pipe()
      let stderr = Pipe()
      process.executableURL = executable
      process.arguments = arguments
      process.standardOutput = stdout
      process.standardError = stderr
      process.environment = ProcessInfo.processInfo.environment.merging(environment) { _, new in new
      }

      // Pipes must be consumed while the process is running. Waiting to read
      // until termination can deadlock once a pipe buffer fills.
      let readers = DispatchGroup()
      let outputBox = OutputBox()
      readers.enter()
      DispatchQueue.global(qos: .userInitiated).async {
        outputBox.setStandardOutput(
          Self.readAndDrain(stdout.fileHandleForReading, limit: self.standardOutputLimit)
        )
        readers.leave()
      }
      readers.enter()
      DispatchQueue.global(qos: .userInitiated).async {
        outputBox.setStandardError(
          Self.readAndDrain(stderr.fileHandleForReading, limit: self.standardErrorLimit)
        )
        readers.leave()
      }

      process.terminationHandler = { [weak self] terminatedProcess in
        readers.notify(queue: .global(qos: .userInitiated)) {
          self?.clearActiveProcess(requestID: requestID, process: terminatedProcess)
          let capturedOutput = outputBox.snapshot()
          continuation.resume(
            returning: AgnesCLIProcessOutput(
              exitCode: terminatedProcess.terminationStatus,
              standardOutput: String(decoding: capturedOutput.standardOutput.data, as: UTF8.self),
              standardError: String(decoding: capturedOutput.standardError.data, as: UTF8.self),
              standardOutputTruncated: capturedOutput.standardOutput.truncated,
              standardErrorTruncated: capturedOutput.standardError.truncated
            )
          )
        }
      }

      setActiveProcess(slot, requestID: requestID)
      do {
        try process.run()
        if lock.withLock({ activeProcesses[requestID]?.cancelRequested == true }) {
          process.interrupt()
        }
      } catch {
        clearActiveProcess(requestID: requestID, process: process)
        stdout.fileHandleForWriting.closeFile()
        stderr.fileHandleForWriting.closeFile()
        continuation.resume(throwing: error)
      }
    }
  }

  func cancel(requestID: UUID) {
    let process = lock.withLock { () -> Process? in
      guard let slot = activeProcesses[requestID] else {
        // `AppModel` publishes the run before its Task reaches `Process.run`.
        // Retain this exact UUID so an immediate Stop cannot be lost.
        pendingCancellationIDs.insert(requestID)
        return nil
      }
      slot.cancelRequested = true
      return slot.process
    }
    guard let process, process.isRunning else { return }
    process.interrupt()
  }

  func cancelAll() {
    let processes = lock.withLock { () -> [Process] in
      for slot in activeProcesses.values {
        slot.cancelRequested = true
      }
      return activeProcesses.values.map(\.process)
    }
    for process in processes where process.isRunning {
      process.interrupt()
    }
  }

  private func setActiveProcess(_ slot: ProcessSlot, requestID: UUID) {
    lock.withLock {
      precondition(activeProcesses[requestID] == nil, "Duplicate Agnes CLI request id")
      slot.cancelRequested = pendingCancellationIDs.remove(requestID) != nil
      activeProcesses[requestID] = slot
    }
  }

  private func clearActiveProcess(requestID: UUID, process: Process) {
    lock.withLock {
      if activeProcesses[requestID]?.process === process {
        activeProcesses.removeValue(forKey: requestID)
      }
    }
  }

  /// Retains at most `limit` bytes but keeps draining the pipe to EOF so a
  /// verbose child cannot deadlock on a full pipe buffer.
  private static func readAndDrain(_ handle: FileHandle, limit: Int) -> BoundedRead {
    var retained = Data()
    var truncated = false

    while true {
      let chunk: Data
      do {
        chunk = try handle.read(upToCount: 64 * 1_024) ?? Data()
      } catch {
        break
      }
      guard !chunk.isEmpty else { break }

      let remaining = max(0, limit - retained.count)
      if remaining > 0 {
        retained.append(chunk.prefix(remaining))
      }
      if chunk.count > remaining {
        truncated = true
      }
    }

    return BoundedRead(data: retained, truncated: truncated)
  }
}

private final class ProcessSlot: @unchecked Sendable {
  let process: Process
  var cancelRequested = false

  init(process: Process) {
    self.process = process
  }
}

private final class OutputBox: @unchecked Sendable {
  private let lock = NSLock()
  private var standardOutput = BoundedRead(data: Data(), truncated: false)
  private var standardError = BoundedRead(data: Data(), truncated: false)

  func setStandardOutput(_ value: BoundedRead) {
    lock.withLock { standardOutput = value }
  }

  func setStandardError(_ value: BoundedRead) {
    lock.withLock { standardError = value }
  }

  func snapshot() -> (standardOutput: BoundedRead, standardError: BoundedRead) {
    lock.withLock { (standardOutput, standardError) }
  }
}

private struct BoundedRead: Sendable {
  let data: Data
  let truncated: Bool
}
