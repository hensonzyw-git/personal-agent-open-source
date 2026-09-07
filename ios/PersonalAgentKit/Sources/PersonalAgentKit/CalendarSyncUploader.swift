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
