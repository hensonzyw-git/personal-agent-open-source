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

/// Tokyo midnight of `day`, as an absolute instant. It is the shape an
/// all-day action carries for a Tokyo event written while the device is
/// somewhere else — the instant and the floating date deliberately disagree,
/// which is the only way to tell the two bases apart in a test.
private func tokyoMidnight(_ day: String) -> Date {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(identifier: "Asia/Tokyo")!
    return CalendarWriteRules.dayStart(day, in: calendar)!
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
            startingAt: t0,
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
            startingAt: t0,
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
            of: draft("网球", start: t0), startingAt: t0, in: nil,
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
            startingAt: t0,
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: justOutside.addingTimeInterval(-1))]
        )
        #expect(atEdge == "EK-1")
        let past = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            startingAt: t0,
            in: "CAL-TRIP",
            among: [candidate("EK-1", "网球", in: "CAL-TRIP", at: justOutside)]
        )
        #expect(past == nil)
    }

    @Test("a different title at the same instant is not a duplicate")
    func differentTitleDoesNotMatch() {
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            startingAt: t0,
            in: "CAL-TRIP",
            among: [candidate("EK-1", "体检", in: "CAL-TRIP", at: inside)]
        )
        #expect(found == nil)
    }

    @Test("the check runs before the window, so a candidate over the edge still counts on the near side")
    func earlierCandidateInsideTheWindowCounts() {
        let found = CalendarWriteRules.duplicate(
            of: draft("网球", start: t0, calendarIdentifier: "CAL-TRIP"),
            startingAt: t0,
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
            startingAt: t0,
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
                startingAt: t0, in: "CAL-TRIP", among: candidates
            ) == "EK-1"
        )
        #expect(
            CalendarWriteRules.duplicate(
                of: draft(
                    "网球", start: t0, calendarIdentifier: "CAL-TRIP",
                    skipLocalDedup: true
                ),
                startingAt: t0, in: "CAL-TRIP", among: candidates
            ) == nil
        )
    }

    @Test("an all-day duplicate is found against the day the device writes, not the action's instant")
    func allDayDuplicateUsesTheDeviceDay() throws {
        // The defect this replaces: the window and the comparison were built
        // from `draft.start` -- the action's absolute instant -- while the
        // write constructed the floating date in the device's calendar. For an
        // all-day event the two differ by the whole offset between the action's
        // zone and the device's, so the check looked at a window the write was
        // never going to land in.
        //
        // Tokyo all-day 10-01 issued while the device is in Shanghai: the
        // action's instant is 09-30T16:00Z, the event EventKit will hold is
        // 10-01T00:00+08:00 (the device's own midnight, §3.2).
        let device = calendar(shanghai)
        var allDay = draft(
            "机票", start: tokyoMidnight("2026-10-01"),
            allDay: true, calendarIdentifier: "CAL-TRIP"
        )
        allDay.startDate = "2026-10-01"
        allDay.endDate = "2026-10-02"
        let plan = try #require(
            CalendarWriteRules.plan(
                for: allDay, in: device
            )
        )
        let alreadyThere = candidate(
            "EK-1", "机票", in: "CAL-TRIP",
            at: try #require(CalendarWriteRules.dayStart("2026-10-01", in: device))
        )
        // Against the write's own start: found.
        #expect(
            CalendarWriteRules.duplicate(
                of: allDay, startingAt: plan.start, in: allDay.calendarIdentifier,
                among: [alreadyThere]
            ) == "EK-1"
        )
        // Against the action's instant -- the old basis -- missed by the whole
        // UTC+8 offset. Pinned so the two can never be confused again.
        #expect(
            CalendarWriteRules.duplicate(
                of: allDay, startingAt: allDay.start, in: allDay.calendarIdentifier,
                among: [alreadyThere]
            ) == nil
        )
    }

    @Test("the all-day window is built from the write's own start")
    func allDayWindowFollowsTheWrite() throws {
        let device = calendar(shanghai)
        var allDay = draft(
            "机票", start: tokyoMidnight("2026-10-01"),
            allDay: true, calendarIdentifier: "CAL-TRIP"
        )
        allDay.startDate = "2026-10-01"
        allDay.endDate = "2026-10-02"
        let plan = try #require(
            CalendarWriteRules.plan(
                for: allDay, in: device
            )
        )
        // The window is the write's start ± the window, never the action's.
        #expect(plan.dupeWindowStart == plan.start - CalendarWriteRules.duplicateWindow)
        #expect(plan.dupeWindowEnd == plan.start + CalendarWriteRules.duplicateWindow)
        #expect(plan.dupeWindowStart > allDay.start)
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
struct AllDayWriteSpanTests {

    @Test("the span ends on the wire's end_date, which is already the exclusive end")
    func spanIsHalfOpenInTheDeviceCalendar() throws {
        // The defect this pins: `end_date` is exclusive on the wire, and the
        // construction added a further day to it, so every multi-day event the
        // agent wrote was one day too long. The server is unambiguous --
        // `calendar_issue._day_pair` carries `end_date` through unchanged and
        // rejects `end_date <= start_date` -- so the device's job is to hand
        // EventKit exactly `[first, end)` and touch neither end.
        let device = calendar(shanghai)
        let span = try #require(
            CalendarWriteRules.allDayWriteSpan(
                startDate: "2026-10-01", endDate: "2026-10-04", in: device
            )
        )
        #expect(CalendarMirrorRules.dayString(span.start, in: device) == "2026-10-01")
        // Exclusive end 10-04 ⇒ the last day the user named is 10-03. The
        // previous version of this test asserted 10-04 while its own comment
        // said the end was exclusive.
        #expect(
            CalendarMirrorRules.dayString(
                span.end.addingTimeInterval(-1), in: device
            ) == "2026-10-03"
        )
        #expect(CalendarMirrorRules.dayString(span.end, in: device) == "2026-10-04")
        #expect(span.start < span.end)
    }

    @Test("the construction is in the device's calendar, never the action's zone")
    func constructionUsesTheDeviceCalendar() throws {
        // §3.2 and the 2026-09-09 probe: constructing in the action's zone
        // writes the wrong day whenever the two differ. The same dates in three
        // device calendars must all land on the day that was asked for.
        for zone in ["Asia/Shanghai", "America/New_York", "Pacific/Auckland"] {
            let device = calendar(TimeZone(identifier: zone)!)
            let span = try #require(
                CalendarWriteRules.allDayWriteSpan(
                    startDate: "2026-10-01", endDate: "2026-10-02", in: device
                ),
                "\(zone) could not construct the span at all"
            )
            #expect(
                CalendarMirrorRules.dayString(span.start, in: device) == "2026-10-01",
                "\(zone) projected the start onto the wrong day"
            )
            #expect(
                CalendarMirrorRules.dayString(
                    span.end.addingTimeInterval(-1), in: device
                ) == "2026-10-01",
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
            CalendarWriteRules.allDayWriteSpan(
                startDate: "2026-03-07", endDate: "2026-03-09", in: device
            )
        )
        #expect(CalendarMirrorRules.dayString(span.start, in: device) == "2026-03-07")
        #expect(
            CalendarMirrorRules.dayString(
                span.end.addingTimeInterval(-1), in: device
            ) == "2026-03-08"
        )
    }

    @Test("a zero-day span is refused, exactly as the server refuses to issue one")
    func zeroDaySpanIsRefused() {
        // `_day_pair` rejects `last <= first` because an exclusive end equal to
        // the start describes an event with no days in it. The device must
        // refuse the same shape rather than invent the one-day event the old
        // rule constructed by adding a day -- which is how the two ends of this
        // contract were able to disagree without any test noticing.
        let device = calendar(shanghai)
        #expect(
            CalendarWriteRules.allDayWriteSpan(
                startDate: "2026-10-01", endDate: "2026-10-01", in: device
            ) == nil
        )
    }

    @Test("a day that does not exist is refused, not rounded to a real one")
    func impossibleDayIsRefused() {
        // `Calendar.date(from:)` is happy to roll 2026-02-30 into 2026-03-02.
        // That would write the event on a real day the user never named, which
        // is worse than refusing: nothing on screen would say it moved.
        //
        // Both positions are exercised with the *other* date valid: a broken
        // value that only ever appears next to an equally broken one would be
        // refused for the wrong reason and prove nothing about the parse.
        let device = calendar(shanghai)
        for day in ["2026-02-30", "2026-13-01", "2026-00-10", "2026-04-31"] {
            #expect(
                CalendarWriteRules.allDayWriteSpan(
                    startDate: day, endDate: "2026-12-31", in: device
                ) == nil,
                "\(day) is not a date and must not resolve to one"
            )
            #expect(
                CalendarWriteRules.allDayWriteSpan(
                    startDate: "2026-01-01", endDate: day, in: device
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
                CalendarWriteRules.allDayWriteSpan(
                    startDate: day, endDate: "2026-12-31", in: device
                ) == nil,
                "\(day) is not YYYY-MM-DD"
            )
            #expect(
                CalendarWriteRules.allDayWriteSpan(
                    startDate: "2026-01-01", endDate: day, in: device
                ) == nil,
                "\(day) is not YYYY-MM-DD"
            )
        }
    }

    @Test("an end before the start is refused")
    func invertedSpanIsRefused() {
        let device = calendar(shanghai)
        #expect(
            CalendarWriteRules.allDayWriteSpan(
                startDate: "2026-10-05", endDate: "2026-10-01", in: device
            ) == nil
        )
    }

    @Test("the span the write constructs uploads back as the action's own end_date")
    func constructedSpanRoundTripsThroughTheMirror() throws {
        // The write rule and the read-back rule are two conversions in opposite
        // directions, and the failure mode is that they disagree by a day
        // without either one looking wrong on its own -- which is precisely how
        // the off-by-one survived. This test holds both ends of the round trip
        // at once: what the device writes is what the mirror uploads, and what
        // the mirror uploads is what the action said.
        //
        // `CalendarMirrorRules.mirrorEvent` reuses the action's own dates for an
        // agent-created event, so the assertion is that the construction did not
        // introduce a day the action never carried.
        let device = calendar(shanghai)
        let actionEndDate = "2026-10-04"
        let span = try #require(
            CalendarWriteRules.allDayWriteSpan(
                startDate: "2026-10-01", endDate: actionEndDate, in: device
            )
        )
        let source = CalendarMirrorSource(
            eventIdentifier: "EK-ROUND-TRIP", calendarIdentifier: "CAL-TRIP",
            title: "西班牙旅行", start: span.start, end: span.end, allDay: true,
            location: nil, notes: nil, timeZoneIdentifier: nil, lastModified: t0
        )
        let uploaded = try #require(
            CalendarMirrorRules.mirrorEvent(
                source,
                agentCreated: AgentCreatedEvent(
                    eventIdentifier: "EK-ROUND-TRIP", allDay: true,
                    startDate: "2026-10-01", endDate: actionEndDate
                ),
                calendar: device
            )
        )
        #expect(uploaded.allDayStartDate == "2026-10-01")
        #expect(uploaded.allDayEndDate == actionEndDate)
        #expect(uploaded.dateAnchorUnknown == false)
    }
}

@Suite("The one write basis the store uses")
struct CalendarWritePlanTests {

    private func allDayDraft(
        _ title: String, startDate: String, endDate: String, start: Date,
        calendarIdentifier: String? = "CAL-TRIP"
    ) -> CalendarEventDraft {
        var allDay = draft(
            title, start: start, allDay: true,
            calendarIdentifier: calendarIdentifier
        )
        allDay.startDate = startDate
        allDay.endDate = endDate
        return allDay
    }

    @Test("a timed event's basis is the action's own instants")
    func timedBasisIsTheAction() throws {
        let device = calendar(shanghai)
        let plan = try #require(
            CalendarWriteRules.plan(for: draft("网球", start: t0), in: device)
        )
        #expect(plan.start == t0)
        #expect(plan.end == t0.addingTimeInterval(3600))
        #expect(plan.dupeWindowStart == t0 - CalendarWriteRules.duplicateWindow)
        #expect(plan.dupeWindowEnd == t0 + CalendarWriteRules.duplicateWindow)
    }

    @Test("an all-day event's basis is the device's floating midnight, not the action's instant")
    func allDayBasisIsTheDeviceDay() throws {
        // The whole point of the split: the duplicate check and the write read
        // the same value. Tokyo's 10-01 is 09-30T16:00Z, and on a Shanghai
        // device the event lands at 10-01T00:00+08:00. A store that built its
        // window from one and its write from the other checked a window the
        // write was never going to land in.
        let device = calendar(shanghai)
        let allDay = allDayDraft(
            "机票", startDate: "2026-10-01", endDate: "2026-10-02",
            start: tokyoMidnight("2026-10-01")
        )
        let plan = try #require(CalendarWriteRules.plan(for: allDay, in: device))
        #expect(
            CalendarMirrorRules.dayString(plan.start, in: device) == "2026-10-01"
        )
        #expect(
            CalendarMirrorRules.dayString(plan.end, in: device) == "2026-10-02"
        )
        #expect(plan.start != allDay.start)
    }

    @Test("a half-carrying all-day action is refused rather than falling back")
    func halfAnAllDayPairIsRefused() throws {
        // One date present and the other absent: the decode layer refuses it,
        // and one arriving here anyway must not quietly fall back to the
        // action's absolute instants -- that is a floating date written from an
        // instant, which is the wrong-day failure §3.2 exists to prevent.
        let device = calendar(shanghai)
        for (startDate, endDate) in [("2026-10-01", nil), (nil, "2026-10-02")] {
            var half = draft("机票", start: tokyoMidnight("2026-10-01"), allDay: true)
            half.startDate = startDate
            half.endDate = endDate
            #expect(
                CalendarWriteRules.plan(for: half, in: device) == nil,
                "startDate=\(startDate ?? "nil") endDate=\(endDate ?? "nil") was given a basis instead of being refused"
            )
        }
    }

    @Test("a v1 all-day action with no dates keeps the v1 construction")
    func v1AllDayKeepsTheActionInstants() throws {
        // The pre-§3.2 shape: no floating dates at all. It must not be refused
        // -- there are v1 actions in flight and in history -- and it keeps
        // exactly the construction the v1 build made.
        let device = calendar(shanghai)
        let plan = try #require(
            CalendarWriteRules.plan(
                for: draft("机票", start: t0, allDay: true), in: device
            )
        )
        #expect(plan.start == t0)
    }

    @Test("an unusable all-day pair is refused, and never becomes a window")
    func unusableAllDayIsRefused() {
        let device = calendar(shanghai)
        let zeroDay = allDayDraft(
            "机票", startDate: "2026-10-01", endDate: "2026-10-01", start: t0
        )
        #expect(CalendarWriteRules.plan(for: zeroDay, in: device) == nil)
    }

    @Test("the zone the plan carries is the display zone, and all-day has none")
    func planCarriesTheDisplayZone() throws {
        let device = calendar(shanghai)
        var timed = draft("网球", start: t0)
        timed.timeZoneIdentifier = "Asia/Tokyo"
        #expect(
            try #require(CalendarWriteRules.plan(for: timed, in: device))
                .zoneIdentifier == "Asia/Tokyo"
        )
        // Probe-frozen: setting a zone on an all-day event flips `isAllDay`
        // back to false, so the plan must carry none however the action was
        // stamped.
        var allDay = allDayDraft(
            "机票", startDate: "2026-10-01", endDate: "2026-10-02", start: t0
        )
        allDay.timeZoneIdentifier = "Asia/Tokyo"
        #expect(
            try #require(CalendarWriteRules.plan(for: allDay, in: device))
                .zoneIdentifier == nil
        )
    }
}
