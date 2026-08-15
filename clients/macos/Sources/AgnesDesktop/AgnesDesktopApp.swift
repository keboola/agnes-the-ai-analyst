import AppKit
import SwiftUI

@MainActor
final class AgnesApplicationDelegate: NSObject, NSApplicationDelegate {
  weak var model: AppModel?

  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
    true
  }

  func applicationWillTerminate(_ notification: Notification) {
    model?.cancelForTermination()
  }
}

@main
struct AgnesDesktopApp: App {
  @NSApplicationDelegateAdaptor(AgnesApplicationDelegate.self) private var appDelegate
  @StateObject private var model = AppModel()

  var body: some Scene {
    WindowGroup("Agnes") {
      ContentView(model: model)
        .frame(minWidth: 1_000, minHeight: 680)
        .onAppear {
          appDelegate.model = model
        }
    }
    .defaultSize(width: 1_180, height: 780)
  }
}
