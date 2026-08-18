import PersonalAgentKit
import SwiftUI

/// §1a / §1j: the thin bar under the navigation bar that indexes anything still
/// waiting on Henson.
///
/// §1q's argument for why this is not a session list, restated because the whole
/// design depends on it holding: entries are produced by an operation's state, not
/// created or named by the user; only non-terminal ones appear; and it keeps no
/// history. The test it must pass is that **its count returns to zero when nothing
/// is outstanding** — a session list can never do that, and this bar disappears
/// entirely at zero rather than rendering "0 项".
///
/// It is a glance indicator, not a navigation target: the review card itself now
/// lives in the Timeline (`1j`), so there is no separate page left for this bar
/// to open. The chevron and tap were removed with the page.
struct AmbientOperationBar: View {
    let pendingCount: Int

    var body: some View {
        if pendingCount > 0 {
            HStack(spacing: 8) {
                Circle()
                    .fill(Color.pending)
                    .frame(width: 7, height: 7)
                Text("\(pendingCount) 项待你处理")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.ink)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 9)
            .background(Color.surface)
        }
    }
}

/// How many review cards this client cannot conclude are finished.
///
/// Lives in `PersonalAgentKit` (`ReviewPendingCount`) so the 贪睡 rule — a
/// `deferred` card stops counting on its day and re-enters at the next 0:00 —
/// is testable. This thin wrapper keeps the call site readable.
func pendingReviewCount(_ summaries: [ReviewSummary]) -> Int {
    ReviewPendingCount.count(summaries)
}

#Preview("指示条 · 有待处理") {
    VStack(spacing: 0) {
        AmbientOperationBar(pendingCount: 2)
        Spacer()
    }
    .background(Color.screenBackground)
}

#Preview("指示条 · 归零则消失") {
    VStack(spacing: 0) {
        AmbientOperationBar(pendingCount: 0)
        Text("空闲时这里什么都没有 —— §1q 的判据")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .padding()
        Spacer()
    }
    .background(Color.screenBackground)
}
