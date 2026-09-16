import Foundation

/// One event the local duplicate check considers, reduced to what the rule
/// reads. EventKit hands back whole `EKEvent`s; this is the seam that lets the
/// rule be tested without a device.
public struct CalendarDuplicateCandidate: Sendable, Equatable {
    public let eventIdentifier: String
    public let calendarIdentifier: String
    public let title: String?
    public let start: Date

    public init(
        eventIdentifier: String, calendarIdentifier: String,
        title: String?, start: Date
    ) {
        self.eventIdentifier = eventIdentifier
        self.calendarIdentifier = calendarIdentifier
        self.title = title
        self.start = start
    }
}

/// The device-side write rules of design §3.1, §3.2 and §3.3, as pure
/// functions — the same split as `CalendarMirrorRules`, and for the same
/// reason: a rule buried in the EventKit adapter is a rule no test can hold,
/// and these three are the ones where getting it wrong writes an event the
/// user never authorised, or refuses one they did.
public enum CalendarWriteRules {
    /// The local duplicate window, unchanged since v1: the same title within
    /// ±5 minutes of the proposed start.
    public static let duplicateWindow: TimeInterval = 5 * 60

    /// What the device decided about the calendar a v2 action names.
    public enum Target: Sendable, Equatable {
        case write(CalendarDirectoryEntry)
        /// Who the device is refusing to write for, in the detail the server
        /// records as the zero-write evidence.
        case refuse(String)
    }

    /// Resolve the action's calendar (§3.2). Every refusal here is a
    /// **zero-write** refusal: the device never falls back to another calendar,
    /// because a日程 that lands in a calendar the user did not name is worse
    /// than one that did not land at all — it is a silent wrong answer, and the
    /// user has no reason to look for it.
    ///
    /// `title` is the name the server's routing matched on. Comparing it with
    /// the calendar's current name catches a rename between issuance and
    /// execution (PRD §8): the identifier still resolves, but it now points at
    /// a calendar the user calls something else, and writing there would be
    /// writing somewhere they did not choose.
    public static func target(
        identifier: String, title: String?, in directory: [CalendarDirectoryEntry]
    ) -> Target {
        guard let entry = directory.first(where: {
            $0.calendarIdentifier == identifier
        }) else {
            return .refuse("the target calendar is not on this device")
        }
        guard entry.allowsContentModifications, !entry.isSubscribed else {
            return .refuse("the target calendar is read-only on this device")
        }
        if let title, entry.title != title {
            return .refuse(
                "the target calendar was renamed: the action names 「\(title)」 "
                + "but that identifier is now 「\(entry.title)」"
            )
        }
        return .write(entry)
    }

    /// The local duplicate check (§3.1, v1-frozen semantics).
    ///
    /// `scope` narrows the check to one calendar, and it is applied here as
    /// well as in the EventKit predicate that gathered `candidates`: the
    /// predicate is an optimisation, this is the rule. A check that ran over
    /// every calendar is the gap this closes — it refuses to write 网球 into
    /// 出游计划 because the user happens to have one in 日常安排 at the same
    /// minute. `nil` keeps the v1 behaviour for a v1 action, which names no
    /// calendar.
    ///
    /// `skipLocalDedup` is the 「仍要创建」 override (§3.3). The user pressed
    /// it *because* this check found something, so running the check again
    /// would refuse the write they just authorised. Only the override endpoint
    /// can set it, and it reaches the device on the sealed action.
    ///
    /// `start` is the instant the write will actually use — `plan.start` — and
    /// it is a parameter rather than something read off `draft` so that it
    /// cannot silently diverge from the write again. The predicate's window is
    /// built from the same value; see `CalendarWritePlan`.
    public static func duplicate(
        of draft: CalendarEventDraft,
        startingAt start: Date,
        in scope: String?,
        among candidates: [CalendarDuplicateCandidate]
    ) -> String? {
        guard !draft.skipLocalDedup else { return nil }
        let scoped = scope.map { scope in
            candidates.filter { $0.calendarIdentifier == scope }
        } ?? candidates
        return scoped.first { candidate in
            candidate.title == draft.title
                && abs(candidate.start.timeIntervalSince(start)) <= duplicateWindow
        }?.eventIdentifier
    }

    /// §3.2's all-day construction, in the **device's** calendar.
    ///
    /// **Both ends are the wire's own days, and neither is adjusted.** The
    /// wire's `end_date` is already exclusive — `calendar_issue._day_pair`
    /// carries it through unchanged and refuses `end_date <= start_date` for
    /// exactly that reason — and EventKit takes a half-open span, so the two
    /// are the same convention and the conversion is the identity. Adding a
    /// day here (as this function did until 2026-09-10) made every multi-day
    /// event the agent wrote one day too long.
    ///
    /// The opposite conversion — an EventKit *inclusive* end to an exclusive
    /// wire date — lives in `CalendarMirrorRules.exclusiveDay(after:in:)` and
    /// belongs to the upload side. Two directions, one convention; the name
    /// `allDayWriteSpan` says which direction this one is.
    ///
    /// Constructing in the device calendar — never in the action's own zone —
    /// is what makes the floating date equal the authorised date when the two
    /// zones differ. The 2026-09-09 probe showed the other way round writes the
    /// wrong day, and that setting a zone on an all-day event flips `isAllDay`
    /// back to false, which is why no caller sets one.
    ///
    /// A device day whose local midnight does not exist (a DST spring-forward)
    /// resolves to the first instant that does; the calendar date is still the
    /// date asked for, which is the property that matters here.
    public static func allDayWriteSpan(
        startDate: String, endDate: String, in calendar: Calendar
    ) -> (start: Date, end: Date)? {
        guard let start = dayStart(startDate, in: calendar),
              let end = dayStart(endDate, in: calendar),
              start < end
        else { return nil }
        return (start, end)
    }

    /// Everything the store needs for one action, derived **once**.
    ///
    /// This type exists because of one defect: the duplicate check built its
    /// window from `draft.start` (the action's absolute instant) while the write
    /// constructed the device's floating day. For a timed event the two agree
    /// and nothing showed; for an all-day event they differ by the whole offset
    /// between the action's zone and the device's, so the check looked in a
    /// window the write would never land in and a genuine duplicate went
    /// unwritten-but-created.
    ///
    /// Two fields of one value cannot disagree. The store computes a `plan` and
    /// uses `start`/`end` for the write and `dupeWindowStart`/`dupeWindowEnd`
    /// for the predicate, and `CalendarWriteRules.duplicate` is handed the same
    /// `start` — so the only way to reintroduce the defect is to stop using the
    /// plan, which the call sites can be read for.
    public struct CalendarWritePlan: Sendable, Equatable {
        /// The instants EventKit is given. Half-open: `end` is exclusive.
        public let start: Date
        public let end: Date
        /// The window the duplicate predicate scans, built from `start`.
        public let dupeWindowStart: Date
        public let dupeWindowEnd: Date
        /// The zone a **timed** event displays in; nil for all-day (§3.2).
        public let zoneIdentifier: String?
    }

    /// The plan for `draft`, or `nil` when the action cannot be written.
    ///
    /// `nil` is a **zero-write refusal**, not a degraded write: an all-day
    /// action carrying half a date pair, or a pair the server would not have
    /// issued, must not fall back to the action's absolute instants — that is a
    /// floating date written from an instant, which is the wrong-day failure
    /// §3.2 exists to prevent.
    public static func plan(
        for draft: CalendarEventDraft,
        in calendar: Calendar,
        window: TimeInterval = duplicateWindow
    ) -> CalendarWritePlan? {
        let basis: (start: Date, end: Date)?
        if draft.allDay, draft.startDate != nil || draft.endDate != nil {
            guard let startDate = draft.startDate, let endDate = draft.endDate
            else { return nil }
            guard let span = allDayWriteSpan(
                startDate: startDate, endDate: endDate, in: calendar
            ) else { return nil }
            basis = span
        } else {
            // A timed event, and a v1 all-day action that carries no floating
            // dates at all: both keep the v1 construction from the action's own
            // instants.
            basis = (draft.start, draft.end)
        }
        // No ordering check on the fallback branch: a timed event's instants
        // are the server's, and refusing a shape this rule has no opinion about
        // would refuse writes that used to land. The all-day branch is guarded
        // where the dates are, by `allDayWriteSpan`.
        guard let basis else { return nil }
        return CalendarWritePlan(
            start: basis.start,
            end: basis.end,
            dupeWindowStart: basis.start.addingTimeInterval(-window),
            dupeWindowEnd: basis.start.addingTimeInterval(window),
            zoneIdentifier: displayZone(for: draft)
        )
    }

    /// The zone to attach to the EventKit event, if any.
    ///
    /// **Never for an all-day event.** The 2026-09-09 probe proved EventKit
    /// flips `isAllDay` back to false the moment a zone is set, which turns a
    /// floating date into a timed instant — the write would look fine and mean
    /// something else. For a timed event the zone is display metadata: the
    /// instant already carries the correct offset, and the zone is what makes
    /// it render by the local rules, DST included.
    ///
    /// A rule rather than an `if` at the call site, because that is where it
    /// can be tested: the store's own line is not reachable from any offline
    /// test, and this is the one field whose misuse is silent.
    public static func displayZone(for draft: CalendarEventDraft) -> String? {
        draft.allDay ? nil : draft.timeZoneIdentifier
    }

    /// The first instant of `YYYY-MM-DD` in `calendar`. Shape-checked as well
    /// as parsed: the decode layer already refuses a date outside the schema's
    /// pattern, and a second check here costs nothing next to writing an event
    /// on a day nobody named.
    static func dayStart(_ day: String, in calendar: Calendar) -> Date? {
        let parts = day.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4, parts[1].count == 2,
              parts[2].count == 2,
              let year = Int(parts[0]), let month = Int(parts[1]),
              let dayNumber = Int(parts[2])
        else { return nil }
        var components = DateComponents()
        components.year = year
        components.month = month
        components.day = dayNumber
        guard let date = calendar.date(from: components),
              let rendered = CalendarMirrorRules.dayString(date, in: calendar)
        else { return nil }
        // Round-tripped: `2026-02-30` must not resolve to 2026-03-02, which is
        // a real day the user never asked for.
        return rendered == day ? date : nil
    }
}
