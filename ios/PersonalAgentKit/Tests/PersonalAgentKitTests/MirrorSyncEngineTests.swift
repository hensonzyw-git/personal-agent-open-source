import Foundation
import Testing
@testable import PersonalAgentKit

// Review R5 (2026-09-08): the mirror-sync engine existed in pieces —
// `CalendarSyncUploader.chunk`, `EventKitCalendarStore.snapshot`,
// `DeviceSession.uploadCalendarSync` — but nothing in production ever called
// them together, so the mirror never synced and every calendar query answered
// 「数据截至」 with a mirror that was empty forever. These tests pin the engine
// that composes them, including the §5.1 failure shapes: a snapshot that
// fails must never be papered over with an empty complete batch (that would
// erase the mirror), and a failed upload must leave the marker where it was
// so the server's own `mirror_stale` keeps answering honestly.

/// A calendar store stub: hands back what the test set, or throws what it
/// was told to throw.
private struct StubMirrorCalendarStore: CalendarStore {
    var events: [CalendarMirrorEvent]
    var error: Error?
    var directory: [CalendarDirectoryEntry] = []
    var directoryError: Error?

    func save(_ draft: CalendarEventDraft) async -> CalendarSaveOutcome {
        .failed(detail: "not used in these tests")
    }

    func calendarDirectory() async throws -> [CalendarDirectoryEntry] {
        if let directoryError { throw directoryError }
        return directory
    }

    func recordAgentCreated(_ record: AgentCreatedEvent) async {}

    func snapshot(since: Date, until: Date, asOf: Date) async throws -> [CalendarMirrorEvent] {
        if let error { throw error }
        return events
    }
}

/// A backend stub recording the batches it was asked to upload, with an
/// optional "lose the network after N batches" failure shape.
private final class StubMirrorBackend: ChatBackend, @unchecked Sendable {
    private let lock = NSLock()
    private var _uploads: [(windowStart: Date, windowEnd: Date, events: [CalendarMirrorEvent], calendars: [CalendarDirectoryEntry], windowComplete: Bool)] = []
    var uploads: [(Date, Date, [CalendarMirrorEvent], Bool)] {
        lock.withLock { _uploads.map { ($0.windowStart, $0.windowEnd, $0.events, $0.windowComplete) } }
    }
    /// The directory each batch carried, in upload order.
    var uploadedDirectories: [[CalendarDirectoryEntry]] {
        lock.withLock { _uploads.map(\.calendars) }
    }
    /// When > 0, batch number `failAfterBatches` (1-based) throws.
    var failAfterBatches = 0

    func uploadCalendarSync(
        windowStart: Date, windowEnd: Date, events: [CalendarMirrorEvent],
        calendars: [CalendarDirectoryEntry],
        windowComplete: Bool, snapshotAsOf: Date
    ) async throws -> CalendarSyncResponse {
        let shouldFail = lock.withLock {
            _uploads.append((windowStart, windowEnd, events, calendars, windowComplete))
            return failAfterBatches > 0 && _uploads.count == failAfterBatches
        }
        if shouldFail {
            throw AgentClientError.transport("network lost mid-window")
        }
        return CalendarSyncResponse(status: "ok", upserted: events.count, skipped: 0, markedDeleted: 0)
    }

    // The rest of the protocol is not this engine's business.
    func sendChatMessage(
        conversationID: String, text: String, clarificationOf: String?,
        startNewSession: Bool, idempotencyKey: String
    ) async throws -> OperationReceipt {
        throw AgentClientError.transport("unused")
    }
    func operation(operationID: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func operation(idempotencyKey: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func cancelOperation(operationID: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func timelinePage(conversationID: String, cursor: String?, direction: TimelineDirection, limit: Int?) async throws -> TimelinePageResponse { throw AgentClientError.transport("unused") }
    func decideDuplicate(checkID: String, decision: DuplicateDecision, idempotencyKey: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func resolveManualReview(operationID: String, resolution: ManualResolution) async throws -> ManualResolutionReceipt { throw AgentClientError.transport("unused") }
    func updateExpenseCategory(recordID: String, category: String, expectedCurrentCategory: String?, idempotencyKey: String) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
    func reportDeviceActionResult(actionID: String, body: DeviceActionResultBody) async throws -> OperationReceipt { throw AgentClientError.transport("unused") }
}

private let t0 = Date(timeIntervalSince1970: 1_783_000_000)

/// The marker's wire format is a UTF-8 `TimeInterval` string, so the tests
/// read it back through the same decode the engine writes — one helper so a
/// format change fails here first.
private func markerDate(_ data: Data) -> Date? {
    Double(String(data: data, encoding: .utf8) ?? "").map { Date(timeIntervalSince1970: $0) }
}

/// `try #require(storage.read(...))` trips the Testing macro's closure
/// expansion ("call can throw, not marked try"), so the read-and-unpack goes
/// through a plain throwing helper instead — a nil marker fails the test.
private func readMarker(_ storage: CredentialStore) throws -> Data {
    guard let data = try storage.read(CredentialKey.calendarMirrorSyncedAt) else {
        throw CalendarMarkerMissing()
    }
    return data
}

private struct CalendarMarkerMissing: Error {}

private func mirrorEvent(_ id: String, offset: TimeInterval) -> CalendarMirrorEvent {
    CalendarMirrorEvent(
        eventIdentifier: id, calendarIdentifier: "CAL-1", title: id,
        start: t0.addingTimeInterval(offset), end: t0.addingTimeInterval(offset + 3600),
        allDay: false, location: nil, notes: nil,
        lastModified: t0.addingTimeInterval(offset)
    )
}

@Suite("Calendar mirror sync engine", .serialized)
struct MirrorSyncEngineTests {

    private func makeEngine(
        store: CalendarStore,
        backend: ChatBackend,
        marker: Date? = nil,
        now: Date = t0,
        threshold: TimeInterval = 3600,
        batchSize: Int = 200
    ) throws -> (CalendarMirrorSyncEngine, InMemoryCredentialStore) {
        let storage = InMemoryCredentialStore()
        if let marker {
            let encoded = String(marker.timeIntervalSince1970).data(using: .utf8)!
            try storage.write(CredentialKey.calendarMirrorSyncedAt, value: encoded)
        }
        let engine = CalendarMirrorSyncEngine(
            store: store, backend: backend, storage: storage,
            uploader: CalendarSyncUploader(batchSize: batchSize),
            now: { now }, stalenessThreshold: threshold
        )
        return (engine, storage)
    }

    @Test("a stale mirror syncs the whole window and stamps the marker after the last batch")
    func fullWindowSync() async throws {
        let store = StubMirrorCalendarStore(
            events: [mirrorEvent("EK-1", offset: 0), mirrorEvent("EK-2", offset: 100)]
        )
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(
            store: store, backend: backend, marker: t0.addingTimeInterval(-7200)
        )

        let synced = try await engine.syncIfNeeded()

        #expect(synced == true)
        let uploads = backend.uploads
        #expect(uploads.count == 1)
        #expect(uploads[0].3 == true)
        #expect(uploads[0].2.count == 2)
        // The marker is the last batch's *completion*, read back from storage.
        let stamped = try readMarker(storage)
        let marker = try #require(markerDate(stamped))
        #expect(marker == t0)
    }

    @Test("a fresh marker skips the sync entirely")
    func freshMarkerSkips() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: t0)

        let synced = try await engine.syncIfNeeded()

        #expect(synced == false)
        #expect(backend.uploads.isEmpty)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == t0)
    }

    @Test("no marker at all is stale: the first sync runs")
    func noMarkerRuns() async throws {
        let store = StubMirrorCalendarStore(events: [])
        let backend = StubMirrorBackend()
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: nil)

        let synced = try await engine.syncIfNeeded()

        #expect(synced == true)
        #expect(backend.uploads.count == 1)
        #expect(backend.uploads[0].2.isEmpty, "an empty window is a fact, still uploaded complete")
        #expect(backend.uploads[0].3 == true)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == t0)
    }

    @Test("a failing snapshot uploads nothing — never an empty complete batch that erases the mirror")
    func failingSnapshotUploadsNothing() async throws {
        let store = StubMirrorCalendarStore(events: [], error: CalendarSnapshotError.accessDenied)
        let backend = StubMirrorBackend()
        let before = t0.addingTimeInterval(-7200)
        let (engine, storage) = try makeEngine(store: store, backend: backend, marker: before)

        do {
            _ = try await engine.syncIfNeeded()
            Issue.record("a snapshot failure must surface, not vanish")
        } catch {
            // The caller degrades; the marker must not have moved and nothing
            // must have been uploaded.
        }
        #expect(backend.uploads.isEmpty)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == before)
    }

    @Test("a mid-window batch failure leaves the marker at its old value and the whole window restarts next time")
    func midWindowFailureKeepsOldMarker() async throws {
        let store = StubMirrorCalendarStore(
            events: (0..<5).map { mirrorEvent("EK-\($0)", offset: Double($0)) }
        )
        let backend = StubMirrorBackend()
        backend.failAfterBatches = 2
        let before = t0.addingTimeInterval(-7200)
        let (engine, storage) = try makeEngine(
            store: store, backend: backend, marker: before, threshold: 0, batchSize: 2
        )

        do {
            _ = try await engine.syncIfNeeded()
            Issue.record("an upload failure must surface, not vanish")
        } catch {}
        // Batch 1 landed; the attempt at batch 2 threw (failAfterBatches=2
        // means the second attempt throws), so `window_complete` never
        // travelled. The marker must NOT advance: the mirror is honestly
        // stale, and both attempts are recorded on the stub.
        #expect(backend.uploads.count == 2)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == before)

        // Next attempt re-runs the *whole window* — the window is only
        // complete once its last batch landed, and the window derives from
        // `now`, not from the marker.
        let (engine2, _) = try makeEngine(
            store: store, backend: backend, marker: before, threshold: 0, batchSize: 2
        )
        _ = try await engine2.syncIfNeeded()
        #expect(backend.uploads.count == 2 + 3, "the retry re-uploads the whole window, not the tail")
    }

    @Test("concurrent calls coalesce: one sync runs, the marker is written once")
    func concurrentCallsCoalesce() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        let (engine, _) = try makeEngine(
            store: store, backend: backend, marker: t0.addingTimeInterval(-7200)
        )

        // All three fire at once; the actor's in-flight flag coalesces them.
        // Exactly one sync runs; the other two observe "in flight" and
        // return without uploading. None of them throw in this shape.
        async let a: Bool = engine.syncIfNeeded()
        async let b: Bool = engine.syncIfNeeded()
        async let c: Bool = engine.syncIfNeeded()
        _ = try await [a, b, c]

        #expect(backend.uploads.count == 1)
    }

    @Test("a failed sync does not poison the next attempt (no stuck in-flight flag)")
    func failedSyncResetsInFlight() async throws {
        let store = StubMirrorCalendarStore(events: [mirrorEvent("EK-1", offset: 0)])
        let backend = StubMirrorBackend()
        backend.failAfterBatches = 1
        let (engine, storage) = try makeEngine(
            store: store, backend: backend, marker: t0.addingTimeInterval(-7200)
        )

        _ = try? await engine.syncIfNeeded()
        backend.failAfterBatches = 0
        let synced = try await engine.syncIfNeeded()
        #expect(synced == true)
        #expect(backend.uploads.count == 2)
        let stamped = try readMarker(storage)
        #expect(markerDate(stamped) == t0)
    }

    // --- second review F1: the wire request must carry snapshot_as_of ------

    /// The request the *real* client sends must satisfy the real route's
    /// schema: `snapshot_as_of` is required, and one window's batches all
    /// carry the same instant. The unit stubs above cannot catch this — they
    /// take the field as a parameter and never read a wire body — so this
    /// test drives `AgentClient` itself through the URLProtocol stub and
    /// inspects the JSON the real encoder produced.
    @Test("the wire request carries snapshot_as_of, identical across batches")
    func wireRequestCarriesSnapshotAsOf() async throws {
        let service = Service()
        // JSONSerialization products are not Sendable, so the bodies collect
        // as `Data` inside a lock-protected box and are parsed on the test
        // side after the calls return.
        final class BodyBox: @unchecked Sendable {
            private let lock = NSLock()
            private var bodies: [Data] = []
            func append(_ data: Data) { lock.withLock { bodies.append(data) } }
            var all: [Data] { lock.withLock { bodies } }
        }
        let box = BodyBox()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/calendar/sync"):
                box.append(call.rawBody)
                return .ok([
                    "status": "ok", "upserted": 0, "skipped": 0, "marked_deleted": 0,
                ])
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ChatStub.self]
        let client = try AgentClient(
            baseURL: service.baseURL,
            session: URLSession(configuration: configuration)
        )
        let instant = Date(timeIntervalSince1970: 1_783_000_000)
        let window = DateInterval(
            start: instant.addingTimeInterval(-90 * 86_400),
            end: instant.addingTimeInterval(180 * 86_400)
        )
        let events = (0..<3).map { mirrorEvent("EK-\($0)", offset: Double($0)) }
        // Two batches of the same window: same snapshot instant.
        _ = try await client.uploadCalendarSync(
            windowStart: window.start, windowEnd: window.end,
            events: Array(events.prefix(2)), calendars: [],
            windowComplete: false,
            snapshotAsOf: instant, token: "token"
        )
        _ = try await client.uploadCalendarSync(
            windowStart: window.start, windowEnd: window.end,
            events: Array(events.suffix(1)), calendars: [],
            windowComplete: true,
            snapshotAsOf: instant, token: "token"
        )
        let bodies = box.all
            .compactMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        #expect(bodies.count == 2)
        for body in bodies {
            #expect(body["snapshot_as_of"] != nil)
            #expect(body["window_complete"] != nil)
        }
        #expect(
            bodies[0]["snapshot_as_of"] as? String == bodies[1]["snapshot_as_of"] as? String
        )
    }

    // --- third review G2: the wait budget must actually bound the wait ------

    /// G2, reproduced against the first implementation: a task group holding
    /// both the timer and `await task.value` cannot return early — the group
    /// waits for all children and `cancelAll()` does not interrupt an
    /// unstructured task's `value` — so a 50 ms budget waited out a 900 ms
    /// sync. The wait must return within the budget while the sync itself
    /// still completes in the background (it is not cancelled).
    @Test("wait() returns within the budget while a slow sync keeps running")
    func waitIsBoundedByTheBudget() async throws {
        final class FlagBox: @unchecked Sendable {
            private let lock = NSLock()
            private var value = false
            var isSet: Bool { lock.withLock { value } }
            func set() { lock.withLock { value = true } }
        }
        let finished = FlagBox()
        let handle = MirrorSyncHandle(
            run: {
                try? await Task.sleep(for: .milliseconds(300))
                finished.set()
            },
            budget: .milliseconds(50)
        )
        let started = ContinuousClock.now
        await handle.wait()
        let waited = ContinuousClock.now - started
        // The wait returned within the budget (plus scheduling slop far below
        // the sync's own duration — the repro waited ~300 ms).
        #expect(waited < .milliseconds(200), "waited \(waited) for a 50 ms budget")
        // The timeout did not cancel the sync: it finishes on its own.
        waitForGate("the slow sync completes after the timeout") { finished.isSet }
        #expect(finished.isSet)
    }

    /// The other side of the race: a sync that finishes inside the budget
    /// wakes the waiter immediately — the budget is a ceiling, not a delay.
    @Test("wait() returns as soon as a fast sync finishes")
    func waitReturnsWhenTheSyncFinishes() async throws {
        let handle = MirrorSyncHandle(
            run: { try? await Task.sleep(for: .milliseconds(20)) },
            budget: .seconds(5)
        )
        let started = ContinuousClock.now
        await handle.wait()
        let waited = ContinuousClock.now - started
        #expect(waited < .seconds(1), "waited \(waited) for a 20 ms sync")
    }
}
