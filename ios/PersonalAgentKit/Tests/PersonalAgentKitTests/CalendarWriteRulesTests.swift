import Foundation
import Testing
@testable import PersonalAgentKit

// The device-side write rules of design §3.1, §3.2 and §3.3. They are pure
// functions for one reason: these are the decisions where being wrong writes an
// event the user never authorised, or refuses one they did — and a decision
// inside the EventKit adapter is a decision no test can hold. The failure cases
// were written first, and the shapes are §5.1's: a calendar that is gone, one
// that is read-only, one that was renamed out from under the action, a
// duplicate just inside and just outside the window, and a day that does not
// exist.

private let shanghai = TimeZone(identifier: "Asia/Shanghai")!

private func calendar(_ zone: TimeZone) -> Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = zone
    calendar.locale = Locale(identifier: "en_US_POSIX")
    return calendar
}

private func entry(
    _ identifier: String, title: String, source: String? = "iCloud",
    writable: Bool = true, subscribed: Bool = false
) -> CalendarDirectoryEntry {
    CalendarDirectoryEntry(
        calendarIdentifier: identifier, title: title, sourceTitle: source,
        allowsContentModifications: writable, isSubscribed: subscribed
    )
}

private func draft(
    _ title: String, start: Date, allDay: Bool = false,
    calendarIdentifier: String? = nil, calendarTitle: String? = nil,
    skipLocalDedup: Bool = false
) -> CalendarEventDraft {
    CalendarEventDraft(
        title: title, start: start, end: start.addingTimeInterval(3600),
        allDay: allDay, location: nil, notes: nil,
        calendarIdentifier: calendarIdentifier, calendarTitle: calendarTitle,
        timeZoneIdentifier: nil, startDate: nil, endDate: nil,
        skipLocalDedup: skipLocalDedup
    )
}

private func candidate(
    _ id: String, _ title: String, in calendar: String, at start: Date
) -> CalendarDuplicateCandidate {
    CalendarDuplicateCandidate(
        eventIdentifier: id, calendarIdentifier: calendar, title: title, start: start
    )
}

private let t0 = Date(timeIntervalSince1970: 1_783_000_000)

@Suite("The write's target calendar")
struct CalendarWriteTargetTests {

    private let directory = [
        entry("CAL-TRIP", title: "出游计划"),
        entry("CAL-DAILY", title: "日常安排"),
        entry("CAL-HOLIDAY", title: "中国节假日", source: "订阅", writable: false, subscribed: true),
        entry("CAL-WORK", title: "工作", writable: false),
    ]

    @Test("the action's identifier resolves to its calendar")
    func resolvesByIdentifier() {
        #expect(
            CalendarWriteRules.target(
                identifier: "CAL-TRIP", title: "出游计划", in: directory
            ) == .write(directory[0])
        )
    }

    @Test("an identifier this device does not have is refused, never redirected")
    func missingIdentifierIsRefused() {
        // The failure that matters: falling back to the default calendar would
        // put the event somewhere the user did not name, and nothing on screen
        // would say so.
        guard case .refuse(let detail) = CalendarWriteRules.target(
            identifier: "CAL-GONE", title: "出游计划", in: directory
        ) else {
            Issue.record("a missing calendar must be refused")
            return
        }
        #expect(detail.contains("not on this device"))
    }

    @Test("a read-only calendar is refused before the write, not by it")
    func readOnlyIsRefused() {
        for identifier in ["CAL-WORK", "CAL-HOLIDAY"] {
            let title = directory.first { $0.calendarIdentifier == identifier }!.title
            guard case .refuse(let detail) = CalendarWriteRules.target(
                identifier: identifier, title: title, in: directory
            ) else {
                Issue.record("\(identifier) is not writable and must be refused")
                return
            }
            #expect(detail.contains("read-only"))
        }
    }

    @Test("a calendar renamed since issuance is refused")
    func renamedIsRefused() {
        // The identifier still resolves, but it now points at a calendar the
        // user calls something else. PRD §8's rename case: writing there is
        // writing somewhere they did not choose.
        guard case .refuse(let detail) = CalendarWriteRules.target(
            identifier: "CAL-TRIP", title: "旅游计划", in: directory
        ) else {
            Issue.record("a renamed calendar must be refused")
            return
        }
        #expect(detail.contains("renamed"))
        #expect(detail.contains("旅游计划"))
        #expect(detail.contains("出游计划"))
    }

    @Test("an action with no name to compare still resolves")
    func noTitleSkipsTheRenameCheck() {
        // The rename check can only run against a name. Absent is not a
        // mismatch, and refusing on absence would refuse every action from a
        // server that stopped sending the field.
        #expect(
            CalendarWriteRules.target(
                identifier: "CAL-TRIP", title: nil, in: directory
            ) == .write(directory[0])
        )
    }

    @Test("an empty directory is a refusal, not a default")
    func emptyDirectoryIsRefused() {
        // The state before the first directory sync. Falling back here is how
        // an event lands in the default calendar while the server believes it
        // authorised 出游计划.
        guard case .refuse = CalendarWriteRules.target(
            identifier: "CAL-TRIP", title: "出游计划", in: []
        ) else {
            Issue.record("an empty directory must be refused")
            return
        }
    }
}

@Suite("The local duplicate check's scope")
struct CalendarDedupScopeTests {

    private let inside = t0
    private let justOutside = t0.addingTimeInterval(5 * 60 + 1)

    @Test("the same title in the target calendar is a duplicate")
    func sameCalendarMatches() {
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: inside)]
        )
        #expect(found == "EK-1")
    }

    @Test("the same title in another calendar is not — the gap §3.1 closes")
    func anotherCalendarDoesNotMatch() {
        // The v1 check scanned every calendar, so a 网球 already sitting in
        // 日常安排 refused a write the user asked for in 出游计划. That refusal
        // is a wrong answer with no way to tell it apart from a real duplicate.
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-DAILY", at: inside)]
        )
        #expect(found == nil)
    }

    @Test("a v1 action with no binding keeps the whole-calendar check")
    func v1ScopeIsEverything() {
        // `nil` scope is not "no check": an action that names no calendar has
        // no scope to narrow to, and the v1 behaviour is what it should get.
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0), in: nil,
            among: [candidate("EK-1", "网球", in: "CAL-DAILY", at: inside)]
        )
        #expect(found == "EK-1")
    }

    @Test("exactly five minutes away is a duplicate; a second more is not")
    func theWindowBoundary() {
        // The window is the v1 one, unchanged, and its edges are what the
        // server's retry behaviour leans on: inclusive at 5:00, exclusive past
        // it. Off-by-one here is a duplicate event or a spurious refusal.
        let atEdge = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: justOutside.addingTimeInterval(-1))]
        )
        #expect(atEdge == "EK-1")
        let past = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: justOutside)]
        )
        #expect(past == nil)
    }

    @Test("a different title at the same instant is not a duplicate")
    func differentTitleDoesNotMatch() {
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "体检", in: "CAL-TRIP", at: inside)]
        )
        #expect(found == nil)
    }

    @Test("the check runs before the window, so a candidate over the edge still counts on the near side")
    func earlierCandidateInsideTheWindowCounts() {
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: t0.addingTimeInterval(-4 * 60))]
        )
        #expect(found == "EK-1")
    }

    @Test("the 「仍要创建」 override writes even though the check would find this")
    func skipLocalDedupWins() {
        // The user pressed the button *because* the device reported a
        // duplicate. Running the check again would refuse the write they just
        // authorised — the override would do nothing at all.
        let found = CalendarWriteRules.duplicate(
            of: draft(
                "网球", start: t0, calendarIdentifier: "CAL-TRIP",
                skipLocalDedup: true
            ),
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: inside)]
        )
        #expect(found == nil)
    }

    @Test("the override does not change what a normal action finds")
    func overrideIsPerAction() {
        let candidates = [candidate("EK-1", "网球", in: "CAL-TRIP", at: inside)]
        #expect(
            CalendarWriteRules.duplicate(
                of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
                in: "CAL-TRIP", among: candidates
            ) == "EK-1"
        )
        #expect(
            CalendarWriteRules.duplicate(
                of: draft(
                    "网球", start: t0, calendarIdentifier: "CAL-TRIP",
                    skipLocalDedup: true
                ),
                in: "CAL-TRIP", among: candidates
            ) == nil
        )
    }
}

@Suite("The zone the write attaches")
struct WriteDisplayZoneTests {

    @Test("a timed event attaches the action's zone")
    func timedKeepsItsZone() {
        var timed = draft("网球", start: t0)
        timed.timeZoneIdentifier = "Asia/Tokyo"
        #expect(CalendarWriteRules.displayZone(for: timed) == "Asia/Tokyo")
    }

    @Test("an all-day event attaches none, whatever the action said")
    func allDayNeverGetsAZone() {
        // The probe's finding, pinned where a test can reach it: EventKit flips
        // `isAllDay` back to false the moment a zone is set, so an all-day
        // event with one is a timed event with a floating date's clothes on.
        // The server writes `timezone: null` for all-day, and this holds even
        // if a future server writes something else.
        var allDay = draft("西班牙旅行", start: t0, allDay: true)
        allDay.timeZoneIdentifier = "Asia/Tokyo"
        #expect(CalendarWriteRules.displayZone(for: allDay) == nil)
    }

    @Test("a floating timed event attaches none either")
    func floatingTimedHasNoZone() {
        // Absent is not a default: the server could not determine a zone, and
        // the device must not name one it does not have.
        #expect(CalendarWriteRules.displayZone(for: draft("网球", start: t0)) == nil)
    }
}

@Suite("The all-day span the device constructs")
struct AllDaySpanTests {

    @Test("the span runs from the first day's start to the day after the last")
    func spanIsHalfOpenInTheDeviceCalendar() throws {
        let device = calendar(shanghai)
        let span = try #require(
            CalendarWriteRules.allDaySpan(
                startDate: "2026-10-01", endDate: "2026-10-03", in: device
            )
        )
        #expect(CalendarMirrorRules.dayString(span.start, in: device) == "2026-10-01")
        // The wire's `end_date` is exclusive, so the last day the user named is
        // 10-03 and EventKit must be handed a span ending on 10-04 at 00:00.
        #expect(CalendarMirrorRules.dayString(span.end.addingTimeInterval(-1), in: device) == "2026-10-03")
        #expect(span.start < span.end)
    }

    @Test("the construction is in the device's calendar, never the action's zone")
    func constructionUsesTheDeviceCalendar() throws {
        // §3.2 and the 2026-09-09 probe: constructing in the action's zone
        // writes the wrong day whenever the two differ. The same dates in two
        // device calendars must both land on the day that was asked for.
        for zone in ["Asia/Shanghai", "America/New_York", "Pacific/Auckland"] {
            let device = calendar(TimeZone(identifier: zone)!)
            let span = try #require(
                CalendarWriteRules.allDaySpan(
                    startDate: "2026-10-01", endDate: "2026-10-01", in: device
                ),
                "\(zone) could not construct the span at all"
            )
            #expect(
                CalendarMirrorRules.dayString(span.start, in: device) == "2026-10-01",
                "\(zone) projected the start onto the wrong day"
            )
            #expect(
                CalendarMirrorRules.dayString(span.end.addingTimeInterval(-1), in: device) == "2026-10-01",
                "\(zone) projected the end onto the wrong day"
            )
        }
    }

    @Test("a span across a DST change keeps both endpoints on their named days")
    func spanAcrossDST() throws {
        // US DST begins 2026-03-08. A multi-day event over it is the case that
        // breaks any arithmetic done in seconds.
        let device = calendar(TimeZone(identifier: "America/New_York")!)
        let span = try #require(
            CalendarWriteRules.allDaySpan(
                startDate: "2026-03-07", endDate: "2026-03-08", in: device
            )
        )
        #expect(CalendarMirrorRules.dayString(span.start, in: device) == "2026-03-07")
        #expect(CalendarMirrorRules.dayString(span.end.addingTimeInterval(-1), in: device) == "2026-03-08")
    }

    @Test("a day that does not exist is refused, not rounded to a real one")
    func impossibleDayIsRefused() {
        // `Calendar.date(from:)` is happy to roll 2026-02-30 into 2026-03-02.
        // That would write the event on a real day the user never named, which
        // is worse than refusing: nothing on screen would say it moved.
        let device = calendar(shanghai)
        for day in ["2026-02-30", "2026-13-01", "2026-00-10", "2026-04-31"] {
            #expect(
                CalendarWriteRules.allDaySpan(
                    startDate: day, endDate: day, in: device
                ) == nil,
                "\(day) is not a date and must not resolve to one"
            )
        }
    }

    @Test("a malformed date is refused")
    func malformedDateIsRefused() {
        let device = calendar(shanghai)
        for day in ["2026-1-1", "26-01-01", "2026/01/01", "", "2026-01-01T00:00:00Z"] {
            #expect(
                CalendarWriteRules.allDaySpan(
                    startDate: day, endDate: "2026-12-31", in: device
                ) == nil,
                "\(day) is not YYYY-MM-DD"
            )
        }
    }

    @Test("an end before the start is refused")
    func invertedSpanIsRefused() {
        let device = calendar(shanghai)
        #expect(
            CalendarWriteRules.allDaySpan(
                startDate: "2026-10-05", endDate: "2026-10-01", in: device
            ) == nil
        )
        // Equal dates are a one-day event, not an empty one: start 10-01 00:00,
        // end 10-02 00:00.
        #expect(
            CalendarWriteRules.allDaySpan(
                startDate: "2026-10-01", endDate: "2026-10-01", in: device
            ) != nil
        )
    }

    @Test("the constructed span is what the mirror uploads for its own event")
    func constructedSpanRoundTripsThroughTheMirror() throws {
        // The write rule and the read-back rule have to agree, or an event the
        // agent created uploads a date the user never sees. The mirror does not
        // read EventKit back for own events, but the two must still be talking
        // about the same day — this is where a drift between them would show.
        let device = calendar(shanghai)
        let span = try #require(
            CalendarWriteRules.allDaySpan(
                startDate: "2026-10-01", endDate: "2026-10-03", in: device
            )
        )
        #expect(CalendarMirrorRules.dayString(span.start, in: device) == "2026-10-01")
        let lastInclusive = try #require(
            CalendarMirrorRules.dayString(span.end.addingTimeInterval(-1), in: device)
        )
        #expect(
            CalendarMirrorRules.exclusiveDay(after: lastInclusive, in: device) == "2026-10-04"
        )
    }
}
