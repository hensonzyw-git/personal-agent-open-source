import Foundation
#if canImport(EventKit)
@preconcurrency import EventKit
#endif

/// One event as this device's calendar owns it — the shape both the device
/// action and the mirror upload travel in.
public struct CalendarEventDraft: Sendable, Equatable {
    public var title: String
    public var start: Date
    public var end: Date
    public var allDay: Bool
    public var location: String?
    public var notes: String?

    public init(
        title: String, start: Date, end: Date, allDay: Bool,
        location: String? = nil, notes: String? = nil
    ) {
        self.title = title
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
    }
}

/// One event as the mirror upload carries it — exactly the server's
/// `calendar.ingest_events` item schema. The identifiers and `lastModified`
/// are the arbitration fields, so they are non-optional: a mirror row without
/// them is not constructible, rather than a 400 at upload time.
///
/// `title` stays optional because EventKit's own title can be nil, and the
/// schema accepts null — guessing `""` would be the device inventing content.
public struct CalendarMirrorEvent: Sendable, Equatable {
    public let eventIdentifier: String
    public let calendarIdentifier: String
    public let title: String?
    public let start: Date
    public let end: Date
    public let allDay: Bool
    public let location: String?
    public let notes: String?
    /// The device-side last-modified instant; the upsert arbitrates out-of-
    /// order batches with it.
    public let lastModified: Date

    public init(
        eventIdentifier: String, calendarIdentifier: String, title: String?,
        start: Date, end: Date, allDay: Bool, location: String?, notes: String?,
        lastModified: Date
    ) {
        self.eventIdentifier = eventIdentifier
        self.calendarIdentifier = calendarIdentifier
        self.title = title
        self.start = start
        self.end = end
        self.allDay = allDay
        self.location = location
        self.notes = notes
        self.lastModified = lastModified
    }
}

/// What a save produced. `created` and `duplicate` are both success evidence;
/// the eventID is what the server records as the write's external evidence.
public enum CalendarSaveOutcome: Sendable, Equatable {
    case created(eventID: String)
    /// A same-title event within ±5 minutes already exists; nothing was saved.
    case duplicate(existingID: String)
    /// The user refused calendar access. The device's own zero-write testimony.
    case denied
    case failed(detail: String)
}

/// The seam between the device-action executor and EventKit.
///
/// A protocol because the failure shapes of §5.1 must be testable without a
/// real calendar: the fake counterpart is written from the contract, and the
/// real one is what the device reports against. The v1 write scope is
/// create-only (v1 仅创建) and single-occurrence events only (v1 仅单次).
public protocol CalendarStore: Sendable {
    /// Save one single-occurrence event, with local duplicate detection:
    /// a same-title event within ±5 minutes of the proposed start makes this
    /// a `duplicate` rather than a second event.
    func save(_ draft: CalendarEventDraft) async -> CalendarSaveOutcome

    /// Snapshot every event overlapping [since, until), for the mirror upload.
    /// `asOf` is the snapshot instant: EventKit exposes no per-event
    /// last-modified, so the snapshot itself is what the upsert arbitrates on
    /// — a later snapshot has a later stamp and wins, which is honest because
    /// the batch, not the row, is what the device vouches for.
    func snapshot(since: Date, until: Date, asOf: Date) async throws -> [CalendarMirrorEvent]
}

#if canImport(EventKit)

/// The EventKit implementation.
///
/// iOS 17+ asks `requestFullAccessToEvents` (the sync upload needs read
/// access too, and full access is the only scope that grants both); iOS ≤16
/// falls back to `requestAccess(to: .event)`. A refusal resolves `denied` —
/// it is the user's own answer on this device, which is exactly the
/// zero-write evidence the server's `failed_safe` expects.
public struct EventKitCalendarStore: CalendarStore {
    private let store: EKEventStore

    public init(store: EKEventStore = EKEventStore()) {
        self.store = store
    }

    // MARK: authorisation

    private func requestAccess() async -> Bool {
        if #available(iOS 17.0, *) {
            return (try? await store.requestFullAccessToEvents()) ?? false
        } else {
            return await withCheckedContinuation { continuation in
                store.requestAccess(to: .event) { granted, _ in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    // MARK: save

    public func save(_ draft: CalendarEventDraft) async -> CalendarSaveOutcome {
        guard await requestAccess() else {
            return .denied
        }
        // Local duplicate detection before any write: the same calendar and
        // title within ±5 minutes of the proposed start means the user (or an
        // earlier attempt) already put this event there. Auto-retry is off at
        // the server precisely because it could double-create; this is the
        // mitigation, not a guarantee.
        let windowStart = draft.start.addingTimeInterval(-5 * 60)
        let windowEnd = draft.start.addingTimeInterval(5 * 60)
        let calendars = store.calendars(for: .event)
        let predicate = store.predicateForEvents(
            withStart: windowStart, end: windowEnd, calendars: calendars
        )
        let near = store.events(matching: predicate)
        if let existing = near.first(where: { event in
            event.title == draft.title
                && abs(event.startDate.timeIntervalSince(draft.start)) <= 5 * 60
        }) {
            return .duplicate(existingID: existing.eventIdentifier)
        }

        let event = EKEvent(eventStore: store)
        event.title = draft.title
        event.startDate = draft.start
        event.endDate = draft.end
        event.isAllDay = draft.allDay
        event.location = draft.location
        event.notes = draft.notes
        event.calendar = defaultWritableCalendar()
        guard event.calendar != nil else {
            return .failed(detail: "no writable calendar")
        }
        do {
            try store.save(event, span: .thisEvent)
            return .created(eventID: event.eventIdentifier)
        } catch {
            return .failed(detail: error.localizedDescription)
        }
    }

    /// The first writable calendar, preferring the system default. Writing
    /// into a subscribed or read-only source would fail at `save` with an
    /// error that reads like a bug; choosing deliberately keeps the failure
    /// vocabulary small.
    private func defaultWritableCalendar() -> EKCalendar? {
        if let source = store.defaultCalendarForNewEvents?.source,
           let calendar = store.calendars(for: .event).first(where: {
               $0.source == source && $0.allowsContentModifications
           }) {
            return calendar
        }
        return store.defaultCalendarForNewEvents
    }

    // MARK: snapshot

    public func snapshot(since: Date, until: Date, asOf: Date) async throws -> [CalendarMirrorEvent] {
        guard await requestAccess() else {
            throw CalendarSnapshotError.accessDenied
        }
        let predicate = store.predicateForEvents(
            withStart: since, end: until, calendars: store.calendars(for: .event)
        )
        let events = store.events(matching: predicate)
        return events.map { event in
            CalendarMirrorEvent(
                eventIdentifier: event.eventIdentifier,
                calendarIdentifier: event.calendar.calendarIdentifier,
                title: event.title,
                start: event.startDate,
                end: event.endDate,
                allDay: event.isAllDay,
                location: event.location,
                notes: event.notes,
                lastModified: asOf
            )
        }
    }
}

public enum CalendarSnapshotError: Error, Equatable, Sendable {
    case accessDenied
}

#endif
