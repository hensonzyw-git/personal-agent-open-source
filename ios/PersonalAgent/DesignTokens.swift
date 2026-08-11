import SwiftUI

/// The v0.2 design's semantic colours, spacing and numeric styling.
///
/// Every value here comes from `个人Agent-iOS-UI-v0.2.html` §1p「语义色对照」and its
/// footnote. The rule the draft states in §7 and repeats in §1p is that the app has
/// **one** accent colour and that status colours are only ever used on labels and
/// text — never as a gradient, never as a second brand colour. Naming them
/// semantically rather than by hue is what keeps that rule enforceable: a view asks
/// for `.danger`, not for red, so light and dark can diverge without the call site
/// knowing.
///
/// The literals live in `Assets.xcassets`, so each token already carries its dark
/// variant. Nothing in this file should hard-code a hex value.
extension Color {
    /// `#D97757` / `#E08A6B`. Brightened in dark mode to hold contrast.
    static let accentBrand = Color("AccentColor")
    /// `#D97757` / `#C26A4C`. Darkened in dark mode so white text stays readable.
    static let userBubble = Color("UserBubble")
    /// `#A8502C` / `#E6A184`. Accent applied to text, e.g. 写后回读一致.
    static let accentText = Color("AccentText")
    /// `#FDFCF9` / `#1A1817`. The screen behind everything.
    static let screenBackground = Color("ScreenBackground")
    /// `#FFFEFB` / `#232120`. Receipt, decision and review cards.
    static let cardSurface = Color("CardSurface")
    /// `#F1EEE7` / `#2A2724`. Chips, pills, the composer field.
    static let surface = Color("Surface")
    /// `#8C2F26` / `#E8907F`. Failure, and D1's solid border.
    static let danger = Color("Danger")
    /// `#8A5C00` / `#D4A44A`. Awaiting the user: clarification, duplicate, review.
    static let pending = Color("Pending")
    /// `#1C1917` / `#F5F1EA`. Primary text.
    static let ink = Color("Ink")
}

/// The same tokens reached through a leading dot at a `ShapeStyle` position, which
/// is what `foregroundStyle`, `fill` and `stroke` resolve against. Without this the
/// call sites would have to spell out `Color.danger` while SwiftUI's own `.red` stays
/// terse — and the awkward one is the one that stops being used.
extension ShapeStyle where Self == Color {
    static var accentBrand: Color { Color.accentBrand }
    static var userBubble: Color { Color.userBubble }
    static var accentText: Color { Color.accentText }
    static var screenBackground: Color { Color.screenBackground }
    static var cardSurface: Color { Color.cardSurface }
    static var surface: Color { Color.surface }
    static var danger: Color { Color.danger }
    static var pending: Color { Color.pending }
    static var ink: Color { Color.ink }
}

/// Spacing and radius scale. The draft uses a small set of repeated values; naming
/// them stops the current situation where 6/8/10/12 appear as bare literals and no
/// one can tell which are deliberate.
enum Metric {
    static let cardPadding: CGFloat = 14
    static let cardRadius: CGFloat = 16
    static let cardSpacing: CGFloat = 8
    static let bubbleRadius: CGFloat = 20
    static let chipRadius: CGFloat = 11
    static let rowSpacing: CGFloat = 12
    static let hairline: CGFloat = 0.5
}

extension View {
    /// §7: 金额与数值统一使用等宽表格数字, so columns of amounts line up.
    func tabularNumbers() -> some View {
        monospacedDigit()
    }

    /// Puts a `List` or `Form` on the design's own background instead of the system
    /// grouped one.
    ///
    /// This is not a cosmetic preference. iOS's dark grouped background is
    /// `#1C1C1E` — R28 G28 B30, a **cool** grey — while this design is warm
    /// throughout (`#232120` cards, R35 G33 B32). Left alone, the review and status
    /// screens render in the opposite colour temperature from the chat screen, which
    /// is what makes a palette read as broken rather than merely inconsistent.
    ///
    /// It deliberately changes only the surface. Row structure, sections and system
    /// list behaviour are untouched, so the structural work `1j`/`1k` still call for
    /// stays a separate change.
    func designSystemListSurface() -> some View {
        scrollContentBackground(.hidden)
            .background(Color.screenBackground)
    }
}
