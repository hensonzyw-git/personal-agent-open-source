import Foundation

/// One event this device created on the agent's behalf, as the device
/// remembers it (design §8, Q10).
///
/// The design and `.qoder/rules/calendar.md` both say the device persists "the
/// event ids it created". The id alone is not enough for the other rule that
/// binds the same mechanism: an all-day event the agent created uploads the
/// **action's** `start_date`/`end_date`, with no EventKit read-back path
/// (§3.2, frozen). At snapshot time the action is long gone — the reply it
/// travelled on is the only hand-off — so the dates have to have been written
/// down when the event was, or the rule is unimplementable. This record is the
/// smallest superset of the id set that can satisfy both: the id, whether it
/// was all-day, and, for all-day events, the two dates the action carried.
///
/// Nothing here is an anchor-timezone map. Henson's 2026-09-10 decision
/// (timezone metadata has no functional use for all-day events) stands: the
/// mirror's all-day `timezone` is always null.
public struct AgentCreatedEvent: Codable, Sendable, Equatable {
    public let eventIdentifier: String
    public let allDay: Bool
    public let startDate: String?
    public let endDate: String?

    public init(
        eventIdentifier: String, allDay: Bool, startDate: String?, endDate: String?
    ) {
        self.eventIdentifier = eventIdentifier
        self.allDay = allDay
        self.startDate = startDate
        self.endDate = endDate
    }

    /// The record for a write this device just completed. The dates are the
    /// action's, not a read-back: the action is the authorization record.
    public init(createdBy action: DeviceEventAction, eventID: String) {
        self.eventIdentifier = eventID
        self.allDay = action.event.allDay
        self.startDate = action.event.startDate
        self.endDate = action.event.endDate
    }

    /// Whether this record can supply the all-day dates §3.2 asks for. A
    /// record without them is still worth keeping — it is what makes
    /// `created_by_agent` true — it just cannot replace the device's own
    /// projection, and the mirror says so with `date_anchor_unknown` instead
    /// of inventing a date.
    public var hasAllDaySpan: Bool {
        allDay && startDate != nil && endDate != nil
    }
}

/// One calendar in the device's directory (design §2.1). Metadata for every
/// normal event calendar, including subscribed ones — the server needs those
/// to recognise and refuse them — while events are uploaded only from
/// non-subscribed calendars (§2.4).
public struct CalendarDirectoryEntry: Sendable, Equatable {
    public let calendarIdentifier: String
    public let title: String
    public let sourceTitle: String?
    public let allowsContentModifications: Bool
    public let isSubscribed: Bool

    public init(
        calendarIdentifier: String, title: String, sourceTitle: String?,
        allowsContentModifications: Bool, isSubscribed: Bool
    ) {
        self.calendarIdentifier = calendarIdentifier
        self.title = title
        self.sourceTitle = sourceTitle
        self.allowsContentModifications = allowsContentModifications
        self.isSubscribed = isSubscribed
    }

    /// Whether this calendar's events are mirrored (§2.4). Subscribed
    /// calendars appear in the directory so the server can recognise and
    /// refuse them, but their events never leave the device: the user
    /// subscribed to someone else's calendar, not to publishing it.
    ///
    /// The birthday source is already absent from the directory, so this is
    /// only the half of §2.4's filter that survives to here.
    public var uploadsEvents: Bool { !isSubscribed }
}

/// One event exactly as EventKit hands it back, before any of the mirror's
/// decisions are applied. Raw on purpose: `CalendarMirrorRules` is where the
/// decisions live, so they can be tested without a device.
public struct CalendarMirrorSource: Sendable, Equatable {
    public let eventIdentifier: String
    public let calendarIdentifier: String
    public let title: String?
    public let start: Date
    public let end: Date
    public let allDay: Bool
    public let location: String?
    public let notes: String?
    /// EventKit's own zone for a timed event, when it states one. Nil means
    /// the event is floating, and the upload must not invent a zone for it.
    public let timeZoneIdentifier: String?
    public let lastModified: Date

    public init(
        eventIdentifier: String, calendarIdentifier: String, title: String?,
        start: Date, end: Date, allDay: Bool, location: String?, notes: String?,
        timeZoneIdentifier: String?, lastModified: Date
    ) {
        self.eventIdentifier = eventIdentifier
        self.calendarIdentifier = calendarIdentifier
        self.title = title
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
        self.timeZoneIdentifier = timeZoneIdentifier
        self.lastModified = lastModified
    }
}

/// The mirror's frozen decisions, as pure functions (design §3.2, §5.2, §6).
///
/// They live here rather than inside the EventKit adapter because a decision
/// buried in a device-only code path is a decision no test can hold. The
/// adapter's whole job is to hand over what EventKit said; every rule about
/// what that means is in this file.
public enum CalendarMirrorRules {
    /// Mirror thresholds, in Unicode **code points** (design §6, R1-F7).
    /// Creation-input limits (80/200/500) are a different contract and are
    /// deliberately not reused here.
    public static let titleLimit = 200
    public static let locationLimit = 500
    public static let notesLimit = 4096

    /// Apply the frozen read-back algorithm to one event.
    ///
    /// `nil` means the event cannot be described in the v2 wire at all — a
    /// date that will not render, or an all-day event whose start date is not
    /// before its end. The caller must **fail the window**, never drop the
    /// event: a batch that omits an event and still carries `window_complete`
    /// tells the server the user deleted it.
    public static func mirrorEvent(
        _ source: CalendarMirrorSource,
        agentCreated: AgentCreatedEvent?,
        calendar: Calendar
    ) -> CalendarMirrorEvent? {
        let title = capped(source.title, limit: titleLimit)
        let location = capped(source.location, limit: locationLimit)
        let notes = capped(source.notes, limit: notesLimit)
        let createdByAgent = agentCreated != nil

        guard source.allDay else {
            // Timed: the zone is EventKit's own, passed through untouched
            // (§5.1). A floating event has no zone to state, and the upload
            // omits the key rather than naming one EventKit never gave — the
            // server reads silence as the v1 shape and renders it as it
            // always did, which is honest; `TimeZone.current` would be this
            // device asserting a fact it does not have.
            return CalendarMirrorEvent(
                eventIdentifier: source.eventIdentifier,
                calendarIdentifier: source.calendarIdentifier,
                title: title.value,
                start: source.start,
                end: source.end,
                allDay: false,
                location: location.value,
                notes: notes.value,
                lastModified: source.lastModified,
                timeZoneIdentifier: source.timeZoneIdentifier,
                allDayStartDate: nil,
                allDayEndDate: nil,
                dateAnchorUnknown: false,
                createdByAgent: createdByAgent,
                titleOverLimit: title.capped,
                locationOverLimit: location.capped,
                notesOverLimit: notes.capped
            )
        }

        // All-day (§3.2, probe H2). The event stores a floating date, so no
        // zone may be attached and no absolute instant may be converted back
        // into one.
        let span: (start: String, end: String, anchorUnknown: Bool)
        if let agentCreated, agentCreated.hasAllDaySpan,
           let start = agentCreated.startDate, let end = agentCreated.endDate {
            // Created by this agent: the action is the authorization record,
            // and the frozen write algorithm makes the floating date equal the
            // action's date by construction. No EventKit read-back.
            span = (start, end, false)
        } else {
            // External (no local record, e.g. 【飞行计划】): the device
            // calendar's projection, proven stable across device timezones —
            // and marked unconfirmed, because a projection is not evidence of
            // where the date belongs.
            guard let first = dayString(source.start, in: calendar),
                  let lastInclusive = dayString(source.end, in: calendar),
                  let exclusive = exclusiveDay(after: lastInclusive, in: calendar)
            else { return nil }
            span = (first, exclusive, true)
        }
        guard span.start < span.end else { return nil }

        return CalendarMirrorEvent(
            eventIdentifier: source.eventIdentifier,
            calendarIdentifier: source.calendarIdentifier,
            title: title.value,
            start: source.start,
            end: source.end,
            allDay: true,
            location: location.value,
            notes: notes.value,
            lastModified: source.lastModified,
            // Always null for all-day: the event has a date, not an instant,
            // so it has no anchor zone to record (probe-frozen).
            timeZoneIdentifier: nil,
            allDayStartDate: span.start,
            allDayEndDate: span.end,
            dateAnchorUnknown: span.anchorUnknown,
            createdByAgent: createdByAgent,
            titleOverLimit: title.capped,
            locationOverLimit: location.capped,
            notesOverLimit: notes.capped
        )
    }

    /// Null the field and raise the flag when it exceeds its threshold
    /// (design §6, D3 = null + marker). Exactly at the threshold is legal: a
    /// complete 200-code-point title is a real title, not a truncation.
    ///
    /// The count is `unicodeScalars.count`, never `String.count`: the latter
    /// counts grapheme clusters, which disagrees with Python's `len(str)` on
    /// emoji and combined CJK — the precise ambiguity the review named.
    static func capped(_ value: String?, limit: Int) -> (value: String?, capped: Bool) {
        guard let value else { return (nil, false) }
        guard value.unicodeScalars.count > limit else { return (value, false) }
        return (nil, true)
    }

    /// `YYYY-MM-DD` in `calendar`'s own timezone.
    static func dayString(_ date: Date, in calendar: Calendar) -> String? {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        guard let year = parts.year, let month = parts.month, let day = parts.day
        else { return nil }
        return String(format: "%04d-%02d-%02d", year, month, day)
    }

    /// The exclusive end date: the day after the last inclusive one.
    ///
    /// EventKit reports an all-day event's end as the last day, 23:59:59
    /// inside the floating frame; its date is the last inclusive day, and the
    /// wire's `end_date` is exclusive (Q5).
    static func exclusiveDay(after day: String, in calendar: Calendar) -> String? {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var components = DateComponents()
        components.year = parts[0]
        components.month = parts[1]
        components.day = parts[2]
        guard let start = calendar.date(from: components),
              let next = calendar.date(byAdding: .day, value: 1, to: start)
        else { return nil }
        return dayString(next, in: calendar)
    }
}
