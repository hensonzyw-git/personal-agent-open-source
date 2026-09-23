import Foundation
import Testing

@testable import PersonalAgentKit

/// §3h 第 4 条: 「稍后处理」是一次贪睡，不是一个终态。A deferred card stops counting
/// on its own `review_date` and re-enters the 待办 count at the next 0:00 — i.e.
/// once the date is behind today. These tests pin that rule and the boundary.
@Suite("The §3h 待办 count for deferred reviews")
struct ReviewPendingCountTests {
    private func summary(
        _ status: ReviewStatus, date: String = "2026-08-10"
    ) -> ReviewSummary {
        ReviewSummary(
            reviewID: "rev-\(date)-\(status.wire)",
            reviewDate: date,
            status: status,
            itemCount: 1,
            createdAt: "\(date)T00:00:00+00:00",
            reviewedAt: status == .reviewed ? "\(date)T12:00:00+00:00" : nil
        )
    }

    @Test("pending and unrecognised always count")
    func pendingAndUnrecognisedCount() {
        let items = [
            summary(.pending, date: "2026-08-09"),
            summary(.unrecognised("future_status"), date: "2026-08-09"),
        ]
        #expect(ReviewPendingCount.count(items, today: "2026-08-12") == 2)
    }

    @Test("reviewed never counts")
    func reviewedNeverCounts() {
        let items = [
            summary(.reviewed, date: "2026-08-08"),
            summary(.reviewed, date: "2026-08-11"),
        ]
        #expect(ReviewPendingCount.count(items, today: "2026-08-12") == 0)
    }

    @Test("a deferred card does not count on its own day — 贪睡")
    func deferredDoesNotCountOnItsDay() {
        let items = [summary(.deferred, date: "2026-08-12")]
        #expect(ReviewPendingCount.count(items, today: "2026-08-12") == 0)
    }

    @Test("a deferred card re-enters the count at the next 0:00")
    func deferredRecountsAfterItsDay() {
        let items = [summary(.deferred, date: "2026-08-11")]
        #expect(ReviewPendingCount.count(items, today: "2026-08-12") == 1)
    }

    @Test("a deferred card counts against an older date too")
    func deferredCountsWhenBehindByMoreThanADay() {
        let items = [summary(.deferred, date: "2026-08-05")]
        #expect(ReviewPendingCount.count(items, today: "2026-08-12") == 1)
    }

    @Test("the boundary is strict: the day after the review_date counts, the day itself does not")
    func boundaryAtMidnight() {
        let today = "2026-08-12"
        let dayOf = summary(.deferred, date: "2026-08-12")
        let nextDay = summary(.deferred, date: "2026-08-11")

        #expect(ReviewPendingCount.count([dayOf], today: today) == 0)
        #expect(ReviewPendingCount.count([nextDay], today: today) == 1)
    }

    @Test("a future-dated deferred card is still 贪睡")
    func futureDatedDeferredIsStillSnoozing() {
        let items = [summary(.deferred, date: "2026-08-13")]
        #expect(ReviewPendingCount.count(items, today: "2026-08-12") == 0)
    }
}
