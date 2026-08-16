import Foundation

/// Finds an installed `agnes` executable in the environment available to a
/// GUI application, which is often narrower than a user's interactive shell.
public enum ExecutableLocator {
  public static func locate(preferredPath: String?) -> URL? {
    locate(
      preferredPath: preferredPath,
      environment: ProcessInfo.processInfo.environment,
      fileManager: .default,
      homeDirectory: FileManager.default.homeDirectoryForCurrentUser,
      guiSearchDirectories: defaultGUISearchDirectories
    )
  }

  static let defaultGUISearchDirectories = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
  ]

  static func locate(
    preferredPath: String?,
    environment: [String: String],
    fileManager: FileManager,
    homeDirectory: URL,
    guiSearchDirectories: [String]
  ) -> URL? {
    if let preferredPath {
      let trimmedPath = preferredPath.trimmingCharacters(in: .whitespacesAndNewlines)
      let url = URL(fileURLWithPath: trimmedPath)
      if !trimmedPath.isEmpty, fileManager.isExecutableFile(atPath: url.path) {
        return url
      }
    }

    let pathDirectories = (environment["PATH"] ?? "")
      .split(separator: ":", omittingEmptySubsequences: true)
      .map(String.init)
    let userDirectories = [
      homeDirectory.appendingPathComponent("bin").path,
      homeDirectory.appendingPathComponent(".local/bin").path,
    ]
    let directories = unique(pathDirectories + guiSearchDirectories + userDirectories)

    for directory in directories {
      let candidate = URL(fileURLWithPath: directory).appendingPathComponent("agnes")
      if fileManager.isExecutableFile(atPath: candidate.path) {
        return candidate
      }
    }
    return nil
  }

  private static func unique(_ paths: [String]) -> [String] {
    var seen = Set<String>()
    return paths.filter { seen.insert($0).inserted }
  }
}
