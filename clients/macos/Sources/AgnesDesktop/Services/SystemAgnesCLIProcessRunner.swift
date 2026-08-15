import Foundation

/// Foundation `Process` implementation. `Process.interrupt()` sends SIGINT,
/// matching the CLI's documented cancellation path for an in-flight ask.
final class SystemAgnesCLIProcessRunner: AgnesCLIProcessRunning, @unchecked Sendable {
  private let lock = NSLock()
  private var activeProcess: Process?

  func run(
    executable: URL,
    arguments: [String],
    environment: [String: String]
  ) async throws -> AgnesCLIProcessOutput {
    try await withCheckedThrowingContinuation { continuation in
      let process = Process()
      let stdout = Pipe()
      let stderr = Pipe()
      process.executableURL = executable
      process.arguments = arguments
      process.standardOutput = stdout
      process.standardError = stderr
      process.environment = ProcessInfo.processInfo.environment.merging(environment) { _, new in new
      }

      // Pipes must be consumed while the process is running. Waiting to
      // read until termination can deadlock once a pipe's buffer fills.
      let readers = DispatchGroup()
      let outputBox = OutputBox()
      readers.enter()
      DispatchQueue.global(qos: .userInitiated).async {
        outputBox.standardOutput = stdout.fileHandleForReading.readDataToEndOfFile()
        readers.leave()
      }
      readers.enter()
      DispatchQueue.global(qos: .userInitiated).async {
        outputBox.standardError = stderr.fileHandleForReading.readDataToEndOfFile()
        readers.leave()
      }

      process.terminationHandler = { [weak self] terminatedProcess in
        readers.notify(queue: .global(qos: .userInitiated)) {
          self?.clearActiveProcess(terminatedProcess)
          continuation.resume(
            returning: AgnesCLIProcessOutput(
              exitCode: terminatedProcess.terminationStatus,
              standardOutput: String(decoding: outputBox.standardOutput, as: UTF8.self),
              standardError: String(decoding: outputBox.standardError, as: UTF8.self)
            )
          )
        }
      }

      setActiveProcess(process)
      do {
        try process.run()
      } catch {
        clearActiveProcess(process)
        stdout.fileHandleForWriting.closeFile()
        stderr.fileHandleForWriting.closeFile()
        continuation.resume(throwing: error)
      }
    }
  }

  func cancel() {
    let process = lock.withLock { activeProcess }
    guard let process, process.isRunning else { return }
    process.interrupt()
  }

  private func setActiveProcess(_ process: Process) {
    lock.lock()
    activeProcess = process
    lock.unlock()
  }

  private func clearActiveProcess(_ process: Process) {
    lock.lock()
    if activeProcess === process {
      activeProcess = nil
    }
    lock.unlock()
  }
}

private final class OutputBox: @unchecked Sendable {
  var standardOutput = Data()
  var standardError = Data()
}
