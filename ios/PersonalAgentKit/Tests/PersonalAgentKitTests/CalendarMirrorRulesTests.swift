import Foundation
import Testing
@testable import PersonalAgentKit

// Design §13 step 6, items 4 and 5: the upload now carries the calendar
// directory, the zone, the all-day dates and the truncation markers, and the
// device records which events it created for the agent.
//
// The decisions are pure functions in `CalendarMirrorRules` and
// `CalendarMirrorWire` precisely so they can be pinned here. What a device
// test would prove instead is that EventKit agrees with this file — valuable,
// but a different claim, and not one this suite makes.

private let shanghai = TimeZone(identifier: "Asia/Shanghai")!

/// The budget suite's fixed instant. Its own constant rather than a share of
/// `MirrorSyncEngineTests`': a fixture that both files reach for is a fixture
/// neither owns, and changing one suite's idea of "now" would silently move
/// the other's window.
private let t0 = Date(timeIntervalSince1970: 1_783_000_000)

/// The device calendar the mirror's date arithmetic runs in.
private func deviceCalendar(_ zone: TimeZone = shanghai) -> Calendar {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = zone
    return calendar
}

private func date(
    _ y: Int, _ m: Int, _ d: Int, _ h: Int = 0, _ min: Int = 0, _ s: Int = 0,
    in zone: TimeZone = shanghai
) -> Date {
    var components = DateComponents()
    components.year = y; components.month = m; components.day = d
    components.hour = h; components.minute = min; components.second = s
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = zone
    return calendar.date(from: components)!
}

private func source(
    id: String = "EK-1",
    allDay: Bool,
    start: Date,
    end: Date,
    title: String? = "日程",
    location: String? = nil,
    notes: String? = nil,
    timeZoneIdentifier: String? = nil,
    calendar: String = "CAL-1"
) -> CalendarMirrorSource {
    CalendarMirrorSource(
        eventIdentifier: id, calendarIdentifier: calendar, title: title,
        start: start, end: end, allDay: allDay, location: location,
        notes: notes, timeZoneIdentifier: timeZoneIdentifier, lastModified: start
    )
}

/// The serialised row, which is what actually goes on the wire. Asserting on
/// the dictionary proves the key is missing; asserting on the text proves the
/// encoder did not write `"timezone": null` from a nil somewhere.
private func encoded(_ event: CalendarMirrorEvent) -> String {
    let data = try! JSONSerialization.data(
        withJSONObject: CalendarMirrorWire.event(event), options: [.sortedKeys]
    )
    return String(data: data, encoding: .utf8)!
}

/// One field's value as it survives a round trip through the wire format.
/// `nil` covers both "absent" and "explicitly null", so a caller that needs
/// to tell them apart asserts on `encoded`'s text instead.
private func wireValue(_ event: CalendarMirrorEvent, _ key: String) -> Any? {
    let data = try! JSONSerialization.data(withJSONObject: CalendarMirrorWire.event(event))
    let object = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
    return object[key]
}

@Suite("The mirror's read-back rules")
struct CalendarMirrorReadBackTests {

    // MARK: all-day

    @Test("an all-day event the agent created uploads the action's dates")
    func agentCreatedAllDayUsesTheActionDates() throws {
        let action = try DeviceEventAction.decode(from: [
            "action_id": "018f0000-0000-7000-8000-00000000cafe",
            "tool": "calendar.create_event",
            "event": [
                "title": "出游", "start": "2026-10-01T00:00:00+08:00",
                "end": "2026-10-03T00:00:00+08:00", "all_day": true,
                "calendar": "出游计划",
                "calendar_identifier": "CAL-1", "calendar_title": "出游计划",
                "timezone": NSNull(), "start_date": "2026-10-01",
                "end_date": "2026-10-03",
            ],
        ])
        // EventKit's read-back: the floating date re-anchored to the device
        // zone, last day 23:59:59. The rule is that this must NOT be what
        // travels — the action is the authorization record.
        let readBack = source(
            allDay: true,
            start: date(2026, 10, 1), end: date(2026, 10, 1, 23, 59, 59)
        )
        let record = AgentCreatedEvent(createdBy: action, eventID: "EK-1")
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                readBack, agentCreated: record, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.allDayStartDate == "2026-10-01")
        #expect(mirrored.allDayEndDate == "2026-10-03")
        // The agent constructed this event from the action, so the dates are
        // not a guess and must not be marked as one.
        #expect(mirrored.dateAnchorUnknown == false)
        #expect(mirrored.createdByAgent)
    }

    @Test("an external all-day event uploads the device's projection, marked unconfirmed")
    func externalAllDayUsesTheDeviceProjection() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: true,
                    start: date(2026, 10, 1), end: date(2026, 10, 1, 23, 59, 59)
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.allDayStartDate == "2026-10-01")
        // Exclusive end: EventKit's last day is 10-01, so the wire's is 10-02.
        #expect(mirrored.allDayEndDate == "2026-10-02")
        #expect(mirrored.dateAnchorUnknown)
        #expect(!mirrored.createdByAgent)
    }

    @Test("a multi-day external all-day event ends the day after its last day")
    func externalMultiDaySpan() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: true,
                    start: date(2026, 10, 1), end: date(2026, 10, 3, 23, 59, 59)
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.allDayStartDate == "2026-10-01")
        #expect(mirrored.allDayEndDate == "2026-10-04")
    }

    @Test("an all-day event never carries a timezone key")
    func allDayNeverCarriesAZone() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: true,
                    start: date(2026, 10, 1), end: date(2026, 10, 1, 23, 59, 59),
                    timeZoneIdentifier: "America/New_York"
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.timeZoneIdentifier == nil)
        #expect(!encoded(mirrored).contains("timezone"))
    }

    @Test("an all-day event whose dates are not before each other fails the window")
    func invertedAllDaySpanIsRefused() {
        // A single-day event reported as ending before it starts. The device
        // cannot describe it, and the caller must fail the window rather than
        // drop it — an omitted event plus `window_complete` is a deletion.
        let mirrored = CalendarMirrorRules.mirrorEvent(
            source(allDay: true, start: date(2026, 10, 4), end: date(2026, 10, 1)),
            agentCreated: nil, calendar: deviceCalendar()
        )
        #expect(mirrored == nil)
    }

    // MARK: timed

    @Test("a timed event passes EventKit's own zone through")
    func timedPassesTheZoneThrough() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false,
                    start: date(2026, 10, 1, 9, 0, in: TimeZone(identifier: "Asia/Tokyo")!),
                    end: date(2026, 10, 1, 10, 0, in: TimeZone(identifier: "Asia/Tokyo")!),
                    timeZoneIdentifier: "Asia/Tokyo"
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.timeZoneIdentifier == "Asia/Tokyo")
        #expect(mirrored.allDayStartDate == nil)
        #expect(mirrored.dateAnchorUnknown == false)
        // Read back through the JSON rather than matching the text: the
        // serialiser escapes the `/` in an IANA name, so a text assertion
        // would be pinning the escaping and not the value.
        #expect(wireValue(mirrored, "timezone") as? String == "Asia/Tokyo")
    }

    @Test("a floating timed event sends no timezone key at all")
    func floatingTimedEventOmitsTheZoneKey() throws {
        // The distinction the server's ingest makes: an absent `timezone` is
        // the v1 shape and is accepted as it always was; an explicit null on a
        // timed event is refused. So this must be *absent*, and the device
        // must not substitute `TimeZone.current` to have something to send.
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    timeZoneIdentifier: nil
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.timeZoneIdentifier == nil)
        #expect(!encoded(mirrored).contains("timezone"))
    }

    @Test("an untouched row still encodes as the v1 shape")
    func v1ShapedRowEncodesWithoutV2Keys() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    timeZoneIdentifier: "Asia/Shanghai"
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        let text = encoded(mirrored)
        for key in [
            "created_by_agent", "date_anchor_unknown", "start_date", "end_date",
            "title_over_limit", "location_over_limit", "notes_over_limit",
        ] {
            #expect(!text.contains(key), "\(key) must not be sent when false")
        }
        #expect(text.contains("\"timezone\""))
    }
}

@Suite("The mirror's length thresholds")
struct CalendarMirrorThresholdTests {

    @Test("a field exactly at the threshold is a complete value, not a truncation")
    func exactlyAtTheThresholdIsKept() throws {
        let title = String(repeating: "字", count: CalendarMirrorRules.titleLimit)
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    title: title
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.title == title)
        #expect(!mirrored.titleOverLimit)
        #expect(!encoded(mirrored).contains("title_over_limit"))
    }

    @Test("one code point over the threshold nulls the field and raises the flag")
    func oneOverTheThresholdIsNulled() throws {
        let title = String(repeating: "字", count: CalendarMirrorRules.titleLimit + 1)
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    title: title
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        // D3: null the field *and* say so. Sending the long value would have
        // the server refuse the whole batch (§5.2).
        #expect(mirrored.title == nil)
        #expect(mirrored.titleOverLimit)
        let text = encoded(mirrored)
        #expect(text.contains("\"title\":null"))
        #expect(text.contains("\"title_over_limit\":true"))
    }

    @Test("the count is code points, so an emoji counts as one")
    func emojiCountsAsOneCodePoint() throws {
        // 200 emoji are 200 code points and 200 Python `len()` — but fewer
        // than 200 Swift `String.count` would count as grapheme clusters only
        // when they combine. The flag must agree with the server's arithmetic.
        let emoji = String(repeating: "😀", count: CalendarMirrorRules.titleLimit)
        #expect(emoji.unicodeScalars.count == CalendarMirrorRules.titleLimit)
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    title: emoji
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(mirrored.title == emoji)
        #expect(!mirrored.titleOverLimit)
    }

    @Test("each field carries its own threshold")
    func eachFieldHasItsOwnThreshold() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    title: "短标题",
                    location: String(repeating: "地", count: CalendarMirrorRules.locationLimit + 1),
                    notes: String(repeating: "记", count: CalendarMirrorRules.notesLimit + 1)
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        // A short title stays; an over-long location and notes do not.
        #expect(mirrored.title == "短标题")
        #expect(!mirrored.titleOverLimit)
        #expect(mirrored.location == nil)
        #expect(mirrored.locationOverLimit)
        #expect(mirrored.notes == nil)
        #expect(mirrored.notesOverLimit)
    }

    @Test("a nil field is absent, not over-limit")
    func nilIsNotOverLimit() throws {
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    title: nil, location: nil, notes: nil
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        #expect(!mirrored.titleOverLimit)
        #expect(!mirrored.locationOverLimit)
        #expect(!mirrored.notesOverLimit)
    }
}

@Suite("The calendar directory")
struct CalendarDirectoryTests {

    @Test("a subscribed calendar is listed but its events are not uploaded")
    func subscribedCalendarsAreListedButNotMirrored() {
        let subscribed = CalendarDirectoryEntry(
            calendarIdentifier: "CAL-SUB", title: "节假日", sourceTitle: "公共日历",
            allowsContentModifications: false, isSubscribed: true
        )
        let local = CalendarDirectoryEntry(
            calendarIdentifier: "CAL-1", title: "日常安排", sourceTitle: "iCloud",
            allowsContentModifications: true, isSubscribed: false
        )
        #expect(!subscribed.uploadsEvents)
        #expect(local.uploadsEvents)
    }

    @Test("a directory entry encodes the schema's five fields, source_title only when known")
    func directoryEntryEncoding() throws {
        let data = try JSONSerialization.data(
            withJSONObject: CalendarMirrorWire.directoryEntry(CalendarDirectoryEntry(
                calendarIdentifier: "CAL-1", title: "日常安排", sourceTitle: nil,
                allowsContentModifications: true, isSubscribed: false
            )),
            options: [.sortedKeys]
        )
        let text = String(data: data, encoding: .utf8)!
        #expect(text.contains("\"calendar_identifier\":\"CAL-1\""))
        #expect(text.contains("\"allows_content_modifications\":true"))
        #expect(text.contains("\"is_subscribed\":false"))
        // A source title EventKit does not have is left out rather than sent
        // as null — the schema's minLength and the server's display both
        // treat "no source" as nothing to say.
        #expect(!text.contains("source_title"))
    }
}

@Suite("The upload's byte budget")
struct CalendarUploadBudgetTests {

    /// Sizes every row the same, so a batch's shape is decided by the budget
    /// and not by the fixture's text.
    private func uploader(
        batchSize: Int = 200, byteBudget: Int = 1000
    ) -> CalendarSyncUploader {
        CalendarSyncUploader(
            batchSize: batchSize, byteBudget: byteBudget, measure: { _ in 100 }
        )
    }

    private func events(_ count: Int) -> [CalendarMirrorEvent] {
        (0..<count).map { index in
            CalendarMirrorEvent(
                eventIdentifier: "EK-\(index)", calendarIdentifier: "CAL-1",
                title: "e", start: t0, end: t0.addingTimeInterval(60), allDay: false,
                location: nil, notes: nil, lastModified: t0
            )
        }
    }

    @Test("the byte budget closes a batch before the count limit is reached")
    func byteBudgetClosesFirst() {
        let budget = 1000
        let chunks = uploader(batchSize: 200, byteBudget: budget).chunk(events(20), now: t0)
        // The invariant, not a magic batch size: no batch is over budget, the
        // whole window still travels, and the bound that closed these batches
        // was the byte budget (nine rows, against a 200-row limit).
        #expect(chunks.count > 1)
        for chunk in chunks {
            #expect(chunk.events.count * 101 + 64 <= budget)
            #expect(chunk.events.count < 200)
        }
        #expect(chunks.flatMap { $0.events }.count == 20)
        #expect(chunks.map { $0.events.count }.reduce(0, +) == 20)
    }

    // The count limit, the last-batch flag and the empty window are already
    // pinned by `CalendarSyncUploaderTests` in DeviceActionTests.swift. Only
    // what the byte budget adds is tested here.

    @Test("an event larger than the whole budget goes alone rather than being dropped")
    func oversizedEventStillTravels() {
        // The schema's per-field maximum keeps one row near 14 KiB against a
        // 128 KiB budget, so this is unreachable in production. If it ever is
        // reachable, leaving the event out is the one outcome that must not
        // happen: the next batch carries `window_complete`, and the server
        // would read the omission as a deletion.
        let huge = CalendarSyncUploader(
            batchSize: 200, byteBudget: 50, measure: { _ in 100 }
        )
        let chunks = huge.chunk(events(2), now: t0)
        #expect(chunks.flatMap { $0.events }.count == 2)
        #expect(chunks.map { $0.events.count } == [1, 1])
    }

    @Test("the real encoder sizes a row within the design's per-event bound")
    func realEncoderStaysUnderTheBound() throws {
        // The one row that could threaten the budget: every text field at its
        // maximum, in CJK — 3 bytes per code point.
        let mirrored = try #require(
            CalendarMirrorRules.mirrorEvent(
                source(
                    allDay: false, start: date(2026, 10, 1, 9), end: date(2026, 10, 1, 10),
                    title: String(repeating: "字", count: CalendarMirrorRules.titleLimit),
                    location: String(
                        repeating: "地", count: CalendarMirrorRules.locationLimit
                    ),
                    notes: String(repeating: "记", count: CalendarMirrorRules.notesLimit)
                ),
                agentCreated: nil, calendar: deviceCalendar()
            )
        )
        let size = CalendarMirrorWire.encodedSize(of: mirrored)
        #expect(size < CalendarMirrorWire.maximumRowBytes)
        #expect(size < 128 * 1024)
    }
}

@Suite("The device's record of what it created")
struct AgentCreatedEventLogTests {

    @Test("a record survives a round trip")
    func recordRoundTrips() async {
        let defaults = UserDefaults(suiteName: "agent-created-\(UUID().uuidString)")!
        let log = UserDefaultsAgentCreatedEventLog(defaults: defaults)
        await log.record(AgentCreatedEvent(
            eventIdentifier: "EK-1", allDay: true,
            startDate: "2026-10-01", endDate: "2026-10-03"
        ))
        let all = await log.all()
        #expect(all["EK-1"]?.allDay == true)
        #expect(all["EK-1"]?.startDate == "2026-10-01")
        #expect(all["EK-1"]?.endDate == "2026-10-03")
    }

    @Test("re-recording an event replaces it rather than duplicating it")
    func reRecordingReplaces() async {
        let defaults = UserDefaults(suiteName: "agent-created-\(UUID().uuidString)")!
        let log = UserDefaultsAgentCreatedEventLog(defaults: defaults)
        await log.record(AgentCreatedEvent(
            eventIdentifier: "EK-1", allDay: true, startDate: "2026-10-01", endDate: "2026-10-02"
        ))
        await log.record(AgentCreatedEvent(
            eventIdentifier: "EK-1", allDay: true, startDate: "2026-10-05", endDate: "2026-10-06"
        ))
        let all = await log.all()
        #expect(all.count == 1)
        #expect(all["EK-1"]?.startDate == "2026-10-05")
    }

    @Test("the log is bounded, oldest first")
    func theLogIsBounded() async {
        let defaults = UserDefaults(suiteName: "agent-created-\(UUID().uuidString)")!
        let log = UserDefaultsAgentCreatedEventLog(defaults: defaults, limit: 3)
        for index in 0..<5 {
            await log.record(AgentCreatedEvent(
                eventIdentifier: "EK-\(index)", allDay: false,
                startDate: nil, endDate: nil
            ))
        }
        let all = await log.all()
        #expect(all.count == 3)
        #expect(all["EK-4"] != nil)
        #expect(all["EK-0"] == nil)
    }

    @Test("an unreadable store reads as no records, never as an error")
    func unreadableStoreReadsAsEmpty() async {
        let defaults = UserDefaults(suiteName: "agent-created-\(UUID().uuidString)")!
        defaults.set(Data("not json".utf8), forKey: "calendar.agentCreatedEvents")
        let log = UserDefaultsAgentCreatedEventLog(defaults: defaults)
        // The degradation the schema documents: no record means the event
        // uploads as external, which is honest. Throwing would instead stop
        // the whole sync over a local cache.
        #expect(await log.all().isEmpty)
    }
}
