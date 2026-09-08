import Foundation

/// One RFC 3339 instant on the wire. The server's ingest schema declares
/// `format: date-time`, so the device sends what EventKit owns as absolute
/// instants, not local wall-clock text.
///
/// `Date` renders in UTC with a fixed formatter — no locale, no timezone
/// database — because a locale-dependent formatter would produce a string the
/// server's format checker may refuse, and a wall-clock string would change
/// meaning across a timezone change between upload attempts.
public enum RFC3339 {
    /// `ISO8601DateFormatter` is documented thread-safe for formatting once
    /// configured, but it is not `Sendable`, so access goes through this lock
    /// rather than hoping the warning is wrong.
    private static let lock = NSLock()
    private nonisolated(unsafe) static let _formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    public static func string(from date: Date) -> String {
        lock.lock(); defer { lock.unlock() }
        return _formatter.string(from: date)
    }

    public static func parse(_ string: String) -> Date? {
        lock.lock(); defer { lock.unlock() }
        return _formatter.date(from: string)
    }
}

/// The device-side half of the calendar mirror (`POST /v1/calendar/sync`).
///
/// The iPhone is the fact source; the server holds a mirror it can query and
/// summarise. Upload semantics, in the server's own vocabulary:
///
/// - the whole window is declared up front (`window_start`/`window_end`);
/// - every event intersecting the window goes in **one or more whole batches**,
///   each ≤ 200 events (the server's schema limit);
/// - the **last** batch of a window sets `window_complete = true`, which
///   authorises the server to mark window events absent from the upload as
///   deleted. That flag is what makes the device the fact source — and it is
///   why a window is never split lazily: an upload that stops without the
///   complete flag leaves the mirror honestly stale, never falsely current.
///
/// The window itself (回看 90 天 / 前瞻 180 天) covers both past and future
/// events: summaries about 明天/本周 read the mirror, so future events must
/// mirror too.
public struct CalendarSyncUploader: Sendable {
    /// How far back the mirror reaches.
    public let lookbackDays: Int
    /// How far ahead it reaches.
    public let lookaheadDays: Int
    /// The server's per-batch limit; the schema refuses more.
    public let batchSize: Int

    public init(lookbackDays: Int = 90, lookaheadDays: Int = 180, batchSize: Int = 200) {
        self.lookbackDays = lookbackDays
        self.lookaheadDays = lookaheadDays
        self.batchSize = batchSize
    }

    /// One window's worth of events, chunked for upload. `last_batch` marks
    /// the chunk that carries `window_complete`.
    public func chunk(
        _ events: [CalendarMirrorEvent], now: Date
    ) -> [(windowStart: Date, windowEnd: Date, events: [CalendarMirrorEvent], lastBatch: Bool)] {
        let windowStart = now.addingTimeInterval(Double(-lookbackDays) * 86_400)
        let windowEnd = now.addingTimeInterval(Double(lookaheadDays) * 86_400)
        guard !events.isEmpty else {
            // An empty window still gets one complete batch: the device has
            // seen the window and says there is nothing in it. That is a fact
            // the server may act on (mark everything in the window deleted),
            // not an absence of evidence.
            return [(windowStart, windowEnd, [], true)]
        }
        var chunks: [(Date, Date, [CalendarMirrorEvent], Bool)] = []
        var index = 0
        while index < events.count {
            let end = min(index + batchSize, events.count)
            chunks.append((windowStart, windowEnd, Array(events[index..<end]), end == events.count))
            index = end
        }
        return chunks
    }
}

/// The production driver the mirror never had (review R5, 2026-09-08):
/// snapshot the whole window, upload it in batches, and stamp the durable
/// marker **only** when the last batch — the one carrying
/// `window_complete` — was accepted. Composed into `AppModel` and triggered
/// from the foreground path and before chat sends that read the mirror.
///
/// The failure posture is "degrade, never lie":
///
/// - a snapshot that throws uploads **nothing**. Uploading an empty complete
///   batch after a failed snapshot would tell the server "the user's calendar
///   emptied out", which is a false deletion of the whole mirror window —
///   the one error this engine must be incapable of.
/// - a batch that throws leaves the marker at its previous value: the server's
///   `mirror_stale` is what then answers 「数据截至」 honestly, and the next
///   attempt re-runs the whole window (the window derives from `now`, not
///   from the marker, so a partially-uploaded window is restarted in full —
///   `window_complete` is what makes a window authoritative, and it exists
///   only at the end of a full pass).
/// - callers that cannot sync (no store access, offline) surface the failure
///   to their own degrade path; the engine never swallows into a fake success.
///
/// Concurrency: the engine is an actor, so overlapping triggers coalesce on
/// an in-flight flag rather than racing two full windows.
public actor CalendarMirrorSyncEngine {
    private let store: any CalendarStore
    private let backend: any ChatBackend
    private let storage: CredentialStore
    private let now: @Sendable () -> Date
    private let stalenessThreshold: TimeInterval
    private let uploader: CalendarSyncUploader
    private var inFlight = false

    public init(
        store: any CalendarStore,
        backend: any ChatBackend,
        storage: CredentialStore,
        uploader: CalendarSyncUploader = CalendarSyncUploader(),
        now: @escaping @Sendable () -> Date = { Date() },
        stalenessThreshold: TimeInterval = 25 * 3600
    ) {
        self.store = store
        self.backend = backend
        self.storage = storage
        self.uploader = uploader
        self.now = now
        // The server answers `mirror_stale` past 25h; syncing before that is
        // wasted upload, syncing after it is required for honest「数据截至」.
        self.stalenessThreshold = stalenessThreshold
    }

    /// Sync when the marker is older than the threshold (or absent). Returns
    /// whether a sync actually ran; throws what the snapshot or an upload
    /// threw, for the caller's degrade path.
    @discardableResult
    public func syncIfNeeded() async throws -> Bool {
        if inFlight { return false }
        guard try Self.isStale(storage: storage, now: now(), threshold: stalenessThreshold) else {
            return false
        }
        inFlight = true
        defer { inFlight = false }

        let instant = now()
        let events = try await store.snapshot(
            since: instant.addingTimeInterval(Double(-uploader.lookbackDays) * 86_400),
            until: instant.addingTimeInterval(Double(uploader.lookaheadDays) * 86_400),
            asOf: instant
        )
        // `asOf` is the device's stamp of vouching: the batch, not the row, is
        // what the upsert arbitrates on (CalendarStore.snapshot's contract).
        var lastError: Error?
        for chunk in uploader.chunk(events, now: instant) where lastError == nil {
            do {
                _ = try await backend.uploadCalendarSync(
                    windowStart: chunk.windowStart,
                    windowEnd: chunk.windowEnd,
                    events: chunk.events,
                    windowComplete: chunk.lastBatch
                )
            } catch {
                lastError = error
            }
        }
        if let lastError { throw lastError }
        // Only here: every batch — including window_complete — was accepted.
        let encoded = String(instant.timeIntervalSince1970).data(using: .utf8)!
        try storage.write(CredentialKey.calendarMirrorSyncedAt, value: encoded)
        return true
    }

    /// Read the durable marker and decide staleness. A missing or unreadable
    /// marker reads as stale — a first sync must run, and an unparseable one
    /// is exactly the shape that must not silently disable syncing forever.
    private static func isStale(
        storage: CredentialStore, now: Date, threshold: TimeInterval
    ) throws -> Bool {
        guard let data = try storage.read(CredentialKey.calendarMirrorSyncedAt),
              let text = String(data: data, encoding: .utf8),
              let interval = Double(text) else {
            return true
        }
        return now.timeIntervalSince1970 - interval > threshold
    }
}
