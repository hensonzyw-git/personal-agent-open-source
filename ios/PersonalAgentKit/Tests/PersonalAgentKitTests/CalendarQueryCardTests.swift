import Foundation
import Testing

@testable import PersonalAgentKit

/// The calendar list card's two halves: the row decoder's refusals, and the
/// rendering rules it shares with the server.
///
/// The rendering rules exist twice on purpose -- once in Python for the
/// deterministic `answer`, once here for the card (design §13 step 6). They are
/// held in step by the *fields*, not by shared code, so the tests that matter
/// are the ones that would catch a drift between the two: the vector pin in
/// `ChatTimelineTests` asserts these lines against the server's own summary
/// string, and the cases below pin the rules themselves.
///
/// Every row in this file goes through the real decoder -- the helpers build
/// JSON, never a struct literal -- so the invariant checks cannot be bypassed
/// by the test that is supposed to be exercising them.
private func envelope(
    events: [String],
    recordCount: Int,
    nextCursor: String? = nil,
    mirrorStale: Bool = false,
    dataAsOf: String = "2026-10-06T07:30:00+08:00",
    sourceSystem: String = "apple_calendar_mirror",
    status: String = "ok"
) -> String {
    let cursor = nextCursor.map { "\"\($0)\"" } ?? "null"
    return """
    {"status":"\(status)","events":[\(events.joined(separator: ","))],\
    "record_count":\(recordCount),"next_cursor":\(cursor),\
    "data_as_of":"\(dataAsOf)","mirror_stale":\(mirrorStale),\
    "source_system":"\(sourceSystem)"}
    """
}

private func row(
    title: String? = "打喷嚏",
    titleJSON: String? = nil,
    allDay: Bool = false,
    start: String = "2026-10-02T19:00:00+09:00",
    end: String = "2026-10-02T21:00:00+09:00",
    timezone: String? = "Asia/Tokyo",
    timezoneJSON: String? = nil,
    startDate: String? = nil,
    endDate: String? = nil,
    dateAnchorUnknown: Bool = false,
    titleOverLimit: Bool = false,
    locationOverLimit: Bool = false,
    notesOverLimit: Bool = false,
    calendarTitle: String? = "演出&活动",
    createdByAgent: Bool = false,
    locationJSON: String = "null",
    notesJSON: String = "null"
) -> String {
    func json(_ value: String?) -> String {
        value.map { "\"\($0)\"" } ?? "null"
    }
    return """
    {"event_identifier":"evt-1","calendar_identifier":"uuid-1",\
    "calendar_title":\(json(calendarTitle)),\
    "title":\(titleJSON ?? json(title)),"start":"\(start)","end":"\(end)",\
    "all_day":\(allDay),"timezone":\(timezoneJSON ?? json(timezone)),\
    "start_date":\(json(startDate)),"end_date":\(json(endDate)),\
    "date_anchor_unknown":\(dateAnchorUnknown),\
    "title_over_limit":\(titleOverLimit),\
    "location_over_limit":\(locationOverLimit),\
    "notes_over_limit":\(notesOverLimit),\
    "location":\(locationJSON),"notes":\(notesJSON),\
    "created_by_agent":\(createdByAgent)}
    """
}

private func decode(_ json: String) throws -> CalendarQueryResult {
    try JSONDecoder().decode(CalendarQueryResult.self, from: Data(json.utf8))
}

private func decodeRow(_ json: String) throws -> CalendarQueryResult.EventRow {
    try #require(try decode(envelope(events: [json], recordCount: 1)).events.first)
}

@Suite("A calendar row is rendered by the rules the two languages share")
struct CalendarQueryRenderingTests {
    @Test("an all-day range comes from its dates, and the stored end is exclusive")
    func allDayRangeIsExclusive() throws {
        let row = try decodeRow(
            row(
                title: "东京出差", allDay: true,
                start: "2026-10-01T00:00:00+09:00", end: "2026-10-04T00:00:00+09:00",
                timezone: nil, startDate: "2026-10-01", endDate: "2026-10-04"
            )
        )
        // 10-04 ends the range covering 10-01..10-03. Folding the epoch instant
        // instead is how a Tokyo 10-01 became a 09-30 in an earlier summary.
        #expect(row.when == "10-01 至 10-03 全天")
        #expect(row.line == "东京出差（10-01 至 10-03 全天）")
    }

    @Test("an all-day event covering one day says one date")
    func allDaySingleDay() throws {
        let row = try decodeRow(
            row(
                title: "体检", allDay: true,
                start: "2026-10-05T00:00:00+09:00", end: "2026-10-06T00:00:00+09:00",
                timezone: nil, startDate: "2026-10-05", endDate: "2026-10-06"
            )
        )
        #expect(row.when == "10-05 全天")
    }

    @Test("an unconfirmed all-day anchor is shown, then annotated")
    func unconfirmedAnchorIsAnnotated() throws {
        let row = try decodeRow(
            row(
                title: "上海-东京", allDay: true,
                start: "2026-10-08T00:00:00+09:00", end: "2026-10-09T00:00:00+09:00",
                timezone: nil, startDate: "2026-10-08", endDate: "2026-10-09",
                dateAnchorUnknown: true
            )
        )
        // The date is still the row's content; the doubt is stated, not hidden.
        #expect(row.when == "10-08 全天")
        #expect(row.line == "上海-东京（10-08 全天，日期归属未确认）")
    }

    @Test("a timed event is rendered in its own zone, and the zone is named")
    func timedEventKeepsItsOwnZone() throws {
        let row = try decodeRow(row())
        // 19:00 Tokyo, never folded to 18:00 Shanghai.
        #expect(row.line == "打喷嚏（10-02 19:00 日本时间 开始）")
    }

    @Test("a timed event with no zone is read as the reference zone, unlabelled")
    func timedEventWithoutAZone() throws {
        let row = try decodeRow(
            row(
                start: "2026-10-02T19:00:00+08:00", end: "2026-10-02T21:00:00+08:00",
                timezone: nil
            )
        )
        #expect(row.line == "打喷嚏（10-02 19:00 开始）")
    }

    @Test("a zone outside the table is named by its offset at that instant")
    func unknownZoneFallsBackToItsOffset() throws {
        let row = try decodeRow(
            row(
                start: "2026-10-02T19:00:00+09:00", end: "2026-10-02T21:00:00+09:00",
                timezone: "Asia/Kathmandu"
            )
        )
        // Exactly named rather than silently rendered as if it were local.
        #expect(row.line == "打喷嚏（10-02 15:45 UTC+05:45 开始）")
    }

    @Test("a zone this client cannot construct is still named, never folded")
    func unconstructableZoneIsNamed() throws {
        let row = try decodeRow(
            row(
                start: "2026-10-02T19:00:00+09:00", end: "2026-10-02T21:00:00+09:00",
                timezone: "Mars/Olympus"
            )
        )
        #expect(row.line == "打喷嚏（10-02 18:00 Mars/Olympus 开始）")
    }

    @Test("a dropped title is named as dropped, not as absent")
    func droppedTitleIsNamed() throws {
        let row = try decodeRow(
            row(title: nil, titleJSON: "null", titleOverLimit: true)
        )
        #expect(row.displayTitle == "标题过长未同步")
    }

    @Test("a genuinely absent title is not the same fact as a dropped one")
    func absentTitleIsNotADroppedOne() throws {
        let row = try decodeRow(row(title: nil, titleJSON: "null"))
        #expect(row.displayTitle == "（无标题日程）")
    }

    @Test("the annotations keep the server's order and its wording")
    func annotationsKeepTheServersOrder() throws {
        let row = try decodeRow(
            row(
                allDay: true,
                start: "2026-10-08T00:00:00+09:00", end: "2026-10-09T00:00:00+09:00",
                timezone: nil, startDate: "2026-10-08", endDate: "2026-10-09",
                dateAnchorUnknown: true,
                locationOverLimit: true, notesOverLimit: true
            )
        )
        #expect(row.annotations == ["日期归属未确认", "地点过长未同步", "备注过长未同步"])
        #expect(row.line == "打喷嚏（10-08 全天，日期归属未确认、地点过长未同步、备注过长未同步）")
    }

    @Test("a start this client cannot parse is shown as its own text")
    func unparseableStartStillShows() throws {
        let row = try decodeRow(
            row(start: "2026-10-02 19:00", end: "2026-10-02 21:00", timezone: nil)
        )
        #expect(row.line == "打喷嚏（2026-10-02 19:00 开始）")
    }
}

@Suite("A calendar query body this build cannot trust is refused, not drawn")
struct CalendarQueryRefusalTests {
    @Test("another domain's result is not a calendar card")
    func anotherSourceSystemIsRefused() throws {
        #expect(throws: (any Error).self) {
            try decode(
                envelope(events: [], recordCount: 0, sourceSystem: "personal_ledger")
            )
        }
    }

    @Test("a failed read never renders as a calendar with nothing in it")
    func aFailedQueryIsRefused() throws {
        // 「这个时间段没有日程」 is a claim about the calendar, and a body that
        // says its own query failed is not evidence for it.
        #expect(throws: (any Error).self) {
            try decode(envelope(events: [], recordCount: 0, status: "error"))
        }
    }

    @Test("an all-day row must carry the dates that are its content")
    func allDayWithoutDatesIsRefused() throws {
        #expect(throws: (any Error).self) {
            try decodeRow(
                row(allDay: true, timezone: nil, startDate: nil, endDate: nil)
            )
        }
    }

    @Test("an all-day row must not carry a timezone")
    func allDayWithATimezoneIsRefused() throws {
        #expect(throws: (any Error).self) {
            try decodeRow(
                row(
                    allDay: true, timezone: "Asia/Tokyo",
                    startDate: "2026-10-01", endDate: "2026-10-02"
                )
            )
        }
    }

    @Test("a timed row must not carry all-day dates")
    func timedWithDatesIsRefused() throws {
        #expect(throws: (any Error).self) {
            try decodeRow(row(startDate: "2026-10-01", endDate: "2026-10-02"))
        }
    }

    @Test("a timed row has no date attribution to doubt")
    func timedWithAnUnknownAnchorIsRefused() throws {
        #expect(throws: (any Error).self) {
            try decodeRow(row(dateAnchorUnknown: true))
        }
    }

    @Test("a truncation flag next to the text it describes is refused")
    func flagWithItsTextPresentIsRefused() throws {
        // The flag is the only surviving evidence of dropped text. Flag *and*
        // text says the mirror is in a state it cannot be in.
        #expect(throws: (any Error).self) {
            try decodeRow(row(title: "打喷嚏", titleOverLimit: true))
        }
        #expect(throws: (any Error).self) {
            try decodeRow(row(locationOverLimit: true, locationJSON: "\"涩谷\""))
        }
        #expect(throws: (any Error).self) {
            try decodeRow(row(notesOverLimit: true, notesJSON: "\"带伞\""))
        }
        // The other direction is legitimate and must not be refused: a flag
        // with nothing beside it is exactly what a dropped field looks like.
        let dropped = try decodeRow(
            row(titleJSON: "null", titleOverLimit: true, locationOverLimit: true)
        )
        #expect(dropped.annotations == ["地点过长未同步"])
    }

    @Test("a page larger than the total it is a page of is refused")
    func pageLargerThanTheTotalIsRefused() throws {
        // The card states 「另有 N 条未列出」 from this difference; a page bigger
        // than the total would render a negative count.
        #expect(throws: (any Error).self) {
            try decode(envelope(events: [row(), row()], recordCount: 1))
        }
    }
}

@Suite("Which projection a query_result is depends on the tool, not the body")
struct CalendarQueryOutcomeTests {
    private func receipt(tool: String?, queryResult: String) -> String {
        """
        {"operation_id":"op_1","state":"succeeded","cancel_requested":false,\
        "client_detached":false,"tool":\(tool.map { "\"\($0)\"" } ?? "null"),\
        "domain":"calendar","record_id":null,"failure_reason":null,\
        "duplicate_check_id":null,"query_result":\(queryResult)}
        """
    }

    private func parse(_ json: String) throws -> OperationReceipt {
        try JSONDecoder().decode(OperationReceipt.self, from: Data(json.utf8))
    }

    @Test("a governed calendar read succeeds as the list card")
    func calendarQueryBecomesTheCard() throws {
        let receipt = try parse(
            receipt(
                tool: "calendar.query_events",
                queryResult: envelope(events: [row()], recordCount: 1)
            )
        )
        guard case .answeredWithCalendarQuery(let result, let tool) = receipt.outcome
        else {
            Issue.record("expected the calendar card, got \(receipt.outcome)")
            return
        }
        #expect(tool == "calendar.query_events")
        #expect(result.events.count == 1)
        #expect(result.recordCount == 1)
        // And it is a settlement that releases the durable idempotency slot,
        // exactly like the ledger's query card.
        #expect(receipt.outcome.isSettled)
        #expect(receipt.outcome.releasesPendingSlot)
        #expect(!receipt.outcome.provesWrite)
    }

    @Test("a calendar query carrying the ledger's projection is not a card")
    func calendarToolWithTheOtherProjectionIsIndeterminate() throws {
        // The tool decides, not the shape: this body is well-formed *Finance*
        // JSON, and it must still not be drawn as a calendar row.
        let financeBody = """
        {"status":"ok","view":"total","record_count":1,"filters_applied":{},\
        "source_system":"personal_ledger","personal_spend_total_cny":"12.00"}
        """
        let receipt = try parse(
            receipt(tool: "calendar.query_events", queryResult: financeBody)
        )
        #expect(receipt.outcome == .indeterminate(state: "succeeded"))
    }

    @Test("a ledger query carrying the mirror's projection is not a card")
    func financeToolWithTheCalendarProjectionIsIndeterminate() throws {
        let receipt = try parse(
            receipt(
                tool: "finance.query_expenses",
                queryResult: envelope(events: [row()], recordCount: 1)
            )
        )
        #expect(receipt.outcome == .indeterminate(state: "succeeded"))
        #expect(receipt.queryResult == nil)
        #expect(receipt.calendarQuery == nil)
    }

    @Test("a calendar query with no result at all is not a success")
    func calendarQueryWithoutAResultIsIndeterminate() throws {
        let receipt = try parse(
            """
            {"operation_id":"op_1","state":"succeeded","cancel_requested":false,\
            "client_detached":false,"tool":"calendar.query_events","domain":"calendar",\
            "record_id":null,"failure_reason":null,"duplicate_check_id":null}
            """
        )
        #expect(receipt.outcome == .indeterminate(state: "succeeded"))
    }

    @Test("a body with no recorded tool is never decoded as a calendar card")
    func missingToolIsIndeterminate() throws {
        // History recorded before tool recording exists carries no tool. That is
        // a different fact from "a calendar query", and it fails closed.
        let receipt = try parse(
            receipt(
                tool: nil,
                queryResult: envelope(events: [row()], recordCount: 1)
            )
        )
        #expect(receipt.outcome == .indeterminate(state: "succeeded"))
    }

    @Test("history draws the same card the live receipt drew")
    func timelineEventDrawsTheSameCard() throws {
        let event = """
        {"event_id":"evt_1","event_type":"operation_result","operation_id":"op_1",\
        "created_at":"2026-10-06T07:30:00+08:00","content":{"state":"succeeded",\
        "tool":"calendar.query_events","domain":"calendar","record_id":null,\
        "query_result":\(envelope(events: [row()], recordCount: 1))}}
        """
        let decoded = try JSONDecoder().decode(
            TimelineEvent.self, from: Data(event.utf8)
        )
        guard case .operationResult(let outcome, _, let toolEvidence) = decoded.kind
        else {
            Issue.record("expected an operation_result, got \(decoded.kind)")
            return
        }
        #expect(toolEvidence == .known("calendar.query_events"))
        guard case .answeredWithCalendarQuery(let result, let tool) = outcome else {
            Issue.record("expected the calendar card, got \(outcome)")
            return
        }
        #expect(tool == "calendar.query_events")
        #expect(result.events.first?.line == "打喷嚏（10-02 19:00 日本时间 开始）")
    }

    @Test("a historical event with no tool recorded stays indeterminate")
    func timelineEventWithoutAToolStaysIndeterminate() throws {
        let event = """
        {"event_id":"evt_1","event_type":"operation_result","operation_id":"op_1",\
        "created_at":"2026-10-06T07:30:00+08:00","content":{"state":"succeeded",\
        "record_id":null,"query_result":\(envelope(events: [row()], recordCount: 1))}}
        """
        let decoded = try JSONDecoder().decode(
            TimelineEvent.self, from: Data(event.utf8)
        )
        guard case .operationResult(let outcome, _, let toolEvidence) = decoded.kind
        else {
            Issue.record("expected an operation_result, got \(decoded.kind)")
            return
        }
        #expect(toolEvidence == .unknown)
        #expect(outcome == .indeterminate(state: "succeeded"))
    }
}
