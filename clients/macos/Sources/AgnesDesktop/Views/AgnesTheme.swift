import AppKit
import SwiftUI

/// Shared visual vocabulary for the native macOS client.
///
/// The palette deliberately follows Agnes' light web surface: a cool canvas,
/// quiet white cards, slate text, and a single sky-blue action color.
enum AgnesTheme {
  static let canvas = Color(red: 243 / 255, green: 246 / 255, blue: 250 / 255)
  static let surface = Color.white
  static let surfaceMuted = Color(red: 248 / 255, green: 250 / 255, blue: 252 / 255)
  static let text = Color(red: 15 / 255, green: 23 / 255, blue: 42 / 255)
  static let textSecondary = Color(red: 71 / 255, green: 85 / 255, blue: 105 / 255)
  static let textMuted = Color(red: 100 / 255, green: 116 / 255, blue: 139 / 255)
  static let border = Color(red: 226 / 255, green: 232 / 255, blue: 240 / 255)
  static let action = Color(red: 2 / 255, green: 132 / 255, blue: 199 / 255)
  static let actionHover = Color(red: 3 / 255, green: 105 / 255, blue: 161 / 255)
  static let actionTint = Color(red: 224 / 255, green: 242 / 255, blue: 254 / 255)

  static let cardRadius: CGFloat = 14
  static let compactSpacing: CGFloat = 8
  static let spacing: CGFloat = 16
  static let sectionSpacing: CGFloat = 24

  static let headerGradient = LinearGradient(
    colors: [surface, Color(red: 247 / 255, green: 250 / 255, blue: 255 / 255)],
    startPoint: .top,
    endPoint: .bottom
  )

  static let actionGradient = LinearGradient(
    colors: [action, actionHover],
    startPoint: .top,
    endPoint: .bottom
  )

  /// A small repository-owned mark for native surfaces that need a brand cue.
  static var mark: Image {
    guard
      let url = Bundle.module.url(forResource: "AgnesMark", withExtension: "svg"),
      let image = NSImage(contentsOf: url)
    else {
      return Image(systemName: "sparkles")
    }
    return Image(nsImage: image)
  }
}

struct AgnesCardModifier: ViewModifier {
  var padding: CGFloat

  func body(content: Content) -> some View {
    content
      .padding(padding)
      .background(AgnesTheme.surface, in: RoundedRectangle(cornerRadius: AgnesTheme.cardRadius))
      .overlay {
        RoundedRectangle(cornerRadius: AgnesTheme.cardRadius)
          .stroke(AgnesTheme.border, lineWidth: 1)
      }
      .shadow(color: .black.opacity(0.035), radius: 10, y: 3)
  }
}

extension View {
  func agnesCard(padding: CGFloat = AgnesTheme.spacing) -> some View {
    modifier(AgnesCardModifier(padding: padding))
  }
}

struct AgnesPrimaryButtonStyle: ButtonStyle {
  @Environment(\.isEnabled) private var isEnabled

  func makeBody(configuration: Configuration) -> some View {
    configuration.label
      .font(.system(size: 14, weight: .semibold))
      .foregroundStyle(.white)
      .padding(.horizontal, AgnesTheme.spacing)
      .padding(.vertical, 9)
      .background(AgnesTheme.actionGradient, in: RoundedRectangle(cornerRadius: 9))
      .opacity(isEnabled ? (configuration.isPressed ? 0.82 : 1) : 0.45)
      .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
  }
}

struct AgnesSecondaryButtonStyle: ButtonStyle {
  @Environment(\.isEnabled) private var isEnabled

  func makeBody(configuration: Configuration) -> some View {
    configuration.label
      .font(.system(size: 14, weight: .medium))
      .foregroundStyle(AgnesTheme.text)
      .padding(.horizontal, AgnesTheme.spacing)
      .padding(.vertical, 9)
      .background(AgnesTheme.surface, in: RoundedRectangle(cornerRadius: 9))
      .overlay {
        RoundedRectangle(cornerRadius: 9).stroke(AgnesTheme.border, lineWidth: 1)
      }
      .opacity(isEnabled ? (configuration.isPressed ? 0.72 : 1) : 0.45)
      .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
  }
}
