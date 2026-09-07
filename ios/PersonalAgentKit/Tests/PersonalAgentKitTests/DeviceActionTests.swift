import Foundation
import Testing
@testable import PersonalAgentKit

/// The device-action wire contract, fail-closed.
///
/// The failure shapes here are the §5.1 ones the device boundary opens: the
/// phone is the executor, so a payload it cannot understand is a *known*
/// failure to report (failed), never silence (which the server's timeout sweep
/// would read as needs_manual_review) and never a guess (an event with a
/// defaulted time is not the event the user asked for).
@Suite("Device action wire")
struct DeviceActionWireTests {

    private let validPayload: [String: Any] = [
        "action_id": "action-1",
        "tool": "calendar.create_event",
        "event": [
            "title": "网球",
            "start": "2026-09-12T15:00:00+08:00",
            "end": "2026-09-12T16:30:00+08:00",
            "all_day": false,
            "location": NSNull(),
            "notes": NSNull(),
        ],
    ]

    private func payload(_ mutate: (inout [String: Any]) -> Void) -> [String: Any] {
        var copy = validPayload
        mutate(&copy)
        return copy
    }

    @Test("a valid payload decodes with the authorised fields")
    func validDecode() throws {
        let action = try DeviceEventAction.decode(from: validPayload)
        #expect(action.actionID == "action-1")
        #expect(action.tool == "calendar.create_event")
        #expect(action.event.title == "网球")
        #expect(action.event.allDay == false)
        #expect(action.event.location == nil)
        let draft = try #require(action.event.draft())
        #expect(draft.title == "网球")
    }

    @Test("a missing required field is refused, not repaired")
    func missingFields() {
        #expect(throws: DeviceActionError.malformed("device_action is missing action_id")) {
            _ = try DeviceEventAction.decode(from: payload { $0.removeValue(forKey: "action_id") })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("title")
            })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("start")
            })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("end")
            })
        }
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("all_day")
            })
        }
    }

    private func eventWithout(_ key: String) -> [String: Any] {
        var event = validPayload["event"] as! [String: Any]
        event.removeValue(forKey: key)
        return event
    }

    @Test("an unknown tool is refused, not guessed at")
    func unknownTool() {
        #expect(throws: DeviceActionError.unsupportedTool("future.tool")) {
            _ = try DeviceEventAction.decode(from: payload { $0["tool"] = "future.tool" })
        }
    }

    @Test("unparseable times are malformed, never saved at a guessed instant")
    func unparseableTimes() {
        #expect(throws: DeviceActionError.self) {
            _ = try DeviceEventAction.decode(from: payload {
                $0["event"] = eventWithout("start").merging(
                    ["start": "下周三下午"], uniquingKeysWith: { _, new in new }
                )
            })
        }
    }

    @Test("the report body is closed and event_id travels only with evidence")
    func reportBodies() {
        // created/duplicate carry the event id the server records as evidence.
        let created = DeviceActionResultBody.created(eventID: "EK-1")
        #expect(created.result == "created")
        #expect(created.eventID == "EK-1")
        #expect(created.detail == nil)
        // denied/failed are the zero-write testimony: no event id exists.
        let denied = DeviceActionResultBody.denied(detail: "access denied")
        #expect(denied.result == "denied")
        #expect(denied.eventID == nil)
        #expect(denied.detail == "access denied")
        let failed = DeviceActionResultBody.failed(detail: "boom")
        #expect(failed.result == "failed")
        #expect(failed.eventID == nil)
    }

    @Test("the receipt's device_action envelope keeps the wire-spelled action id")
    func envelopeDecodesTheWireSpelledID() throws {
        // Regression: the envelope's keys must be declared, not synthesised —
        // a synthesised `actionID` key decodes the wire's `action_id` to nil
        // and turns every well-formed action into an unnameable refusal.
        let body = chatJSON([
            "action_id": "018f0000-0000-7000-8000-00000000cafe",
            "tool": "calendar.create_event",
            "event": [
                "title": "网球",
                "start": "2026-09-12T15:00:00Z",
                "end": "2026-09-12T16:30:00Z",
                "all_day": false,
            ],
        ])
        let envelope = try JSONDecoder().decode(DeviceActionEnvelope.self, from: body)
        guard case .execute(let action) = envelope.resolve() else {
            Issue.record("a well-formed action must resolve to execute")
            return
        }
        #expect(action.actionID == "018f0000-0000-7000-8000-00000000cafe")
    }
}

/// The sync uploader's window and chunking semantics: the device is the fact
/// source, so the last batch of a window must carry `window_complete`, and an
/// empty window must still produce one complete batch (nothing in the window
/// is a fact, not an absence of evidence).
@Suite("Calendar sync uploader")
struct CalendarSyncUploaderTests {

    private func event(_ offset: TimeInterval) -> CalendarMirrorEvent {
        CalendarMirrorEvent(
            eventIdentifier: "EK-\(Int(offset))", calendarIdentifier: "CAL-1",
            title: "e", start: Date(timeIntervalSince1970: offset),
            end: Date(timeIntervalSince1970: offset + 3600), allDay: false,
            location: nil, notes: nil,
            lastModified: Date(timeIntervalSince1970: 1_783_000_000)
        )
    }

    @Test("a window fits one batch and the batch is complete")
    func singleBatch() {
        let uploader = CalendarSyncUploader()
        let now = Date(timeIntervalSince1970: 1_783_000_000)
        let chunks = uploader.chunk([event(1), event(2)], now: now)
        #expect(chunks.count == 1)
        #expect(chunks[0].lastBatch)
        #expect(chunks[0].events.count == 2)
        // 回看 90 天 / 前瞻 180 天.
        #expect(chunks[0].windowStart.timeIntervalSince(now) == -90 * 86_400)
        #expect(chunks[0].windowEnd.timeIntervalSince(now) == 180 * 86_400)
    }

    @Test("more than one batch chunks, and only the last is complete")
    func multiBatch() {
        let uploader = CalendarSyncUploader(batchSize: 2)
        let now = Date(timeIntervalSince1970: 1_783_000_000)
        let events = (0..<5).map { event(Double($0)) }
        let chunks = uploader.chunk(events, now: now)
        #expect(chunks.count == 3)
        #expect(chunks.map { $0.lastBatch } == [false, false, true])
        #expect(chunks.flatMap { $0.events }.count == 5)
    }

    @Test("an empty window still produces one complete batch")
    func emptyWindow() {
        let uploader = CalendarSyncUploader()
        let now = Date(timeIntervalSince1970: 1_783_000_000)
        let chunks = uploader.chunk([], now: now)
        #expect(chunks.count == 1)
        #expect(chunks[0].events.isEmpty)
        #expect(chunks[0].lastBatch)
    }

    @Test("RFC 3339 round-trips in UTC")
    func rfc3339() throws {
        let date = try #require(RFC3339.parse("2026-09-12T07:00:00Z"))
        #expect(RFC3339.string(from: date) == "2026-09-12T07:00:00Z")
        // An offset form parses to the same instant.
        let offset = try #require(RFC3339.parse("2026-09-12T15:00:00+08:00"))
        #expect(offset == date)
    }
}

// --- the device-action flow (`calendar.create_event`) -------------------------
//
// The reply to a chat send is the ONLY time the action exists on the wire, so
// the tests drive the real send path over the same URLProtocol stub the DEV-030
// suites use, with a stubbed executor seam. The failure shapes are §5.1's: an
// action nobody runs, an action the device refuses, and a refusal that cannot
// name its action — each either reported or honestly silent, never repaired.

/// A stub executor that records what it was handed and what it answered.
final class StubDeviceActionExecutor: DeviceActionExecuting, @unchecked Sendable {
    private let lock = NSLock()
    private var _actions: [DeviceEventAction] = []
    var actions: [DeviceEventAction] { lock.withLock { _actions } }
    /// The save outcome to hand back, or nil to use the default `created`.
    var outcome: CalendarSaveOutcome?
    /// The backend the report goes through — set after `makeChat` builds the
    /// session, which the executor cannot capture at construction time.
    var backend: (any ChatBackend)?

    func executeAndReport(_ action: DeviceEventAction) async -> OperationReceipt {
        lock.withLock { _actions.append(action) }
        let body: DeviceActionResultBody
        switch outcome ?? .created(eventID: "EK-NEW-1") {
        case .created(let id): body = .created(eventID: id)
        case .duplicate(let id): body = .duplicate(eventID: id)
        case .denied: body = .denied(detail: "refused by the stub")
        case .failed(let detail): body = .failed(detail: detail)
        }
        // The report goes through the real backend (the stub service), so the
        // endpoint interaction is the real one.
        do {
            return try await #require(backend)
                .reportDeviceActionResult(actionID: action.actionID, body: body)
        } catch {
            return OperationReceipt(
                operationID: "op-1", state: .sourceInProgress, cancelRequested: false,
                clientDetached: false, tool: nil, recordID: nil, failureReason: nil,
                duplicateCheckID: nil, clarification: nil, duplicateExisting: nil,
                answer: nil
            )
        }
    }

    func failedReport(detail: String) -> DeviceActionResultBody {
        .failed(detail: detail)
    }
}

@Suite("The device-action flow", .serialized)
struct DeviceActionFlowTests {

    /// The action id is a canonical UUIDv4 because the server locates the
    /// operation by it (the idempotency key), and the stub service enforces
    /// that shape on every request it relays. A function, not a stored
    /// constant: a fresh dictionary per call, so nothing non-Sendable is
    /// ever shared.
    private static func actionPayload() -> [String: Any] {
        [
            "action_id": "018f0000-0000-7000-8000-00000000cafe",
            "tool": "calendar.create_event",
            "event": [
                "title": "网球",
                "start": "2026-09-12T15:00:00Z",
                "end": "2026-09-12T16:30:00Z",
                "all_day": false,
            ],
        ]
    }

    /// A chat send whose reply carries a `device_action` and parks the
    /// operation at `source_in_progress`. The report endpoint answers the way
    /// the real one does: a `created`/`duplicate` report settles succeeded
    /// with the reported event id as evidence; a `denied`/`failed` report
    /// settles `failed_safe`. A fake that answered success to every report
    /// would let a device claim a write it never made — exactly what this
    /// endpoint exists to prevent.
    private func answerWithAction(_ service: Service) {
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_action"] = Self.actionPayload()
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                if call.string("result") == "created" || call.string("result") == "duplicate" {
                    return .ok(chatReceipt(
                        "succeeded", tool: "calendar.create_event",
                        recordID: call.string("event_id") ?? "EK-NEW-1"
                    ))
                }
                return .ok(chatReceipt(
                    "failed_safe", tool: "calendar.create_event",
                    failureReason: "DEVICE_EXECUTION_FAILED"
                ))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
    }

    @Test("a handed action is executed and its report settles the operation")
    func actionExecutedAndReported() async throws {
        let service = Service()
        answerWithAction(service)
        let executor = StubDeviceActionExecutor()
        let (chat, session, store) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六下午三点网球")

        #expect(executor.actions.count == 1)
        #expect(executor.actions.first?.actionID == Self.actionPayload()["action_id"] as? String)
        #expect(executor.actions.first?.event.title == "网球")
        #expect(final.state == .succeeded)
        #expect(final.recordID == "EK-NEW-1")
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        // A settled operation releases the pending slot.
        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
    }

    @Test("an action with no executor composed is reported failed, not dropped")
    func actionWithoutExecutorIsReportedFailed() async throws {
        let service = Service()
        answerWithAction(service)
        let (chat, _, _) = try await makeChat(service: service)

        let final = try await chat.send(text: "周六下午三点网球")

        // The server was told the action did not run — the sweep is not the
        // only witness, and the person is not left with a phantom.
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        let report = service.log.first {
            $0.path.hasPrefix("/v1/device-actions/")
        }
        #expect(report?.string("result") == "failed")
        #expect(final.state == .failedSafe)
    }

    @Test("a refusal with an action id is reported failed immediately")
    func refusedActionWithIDIsReported() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var payload = Self.actionPayload()
                payload["tool"] = "future.tool"
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_action"] = payload
                return .ok(receipt)
            case ("POST", let path) where path.hasPrefix("/v1/device-actions/"):
                return .ok(chatReceipt("failed_safe", failureReason: "DEVICE_EXECUTION_FAILED"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "安排一件事")

        // The executor was never reached: the action was refused at decode.
        #expect(executor.actions.isEmpty)
        #expect(service.count("POST", "/v1/device-actions/018f0000-0000-7000-8000-00000000cafe/result") == 1)
        #expect(final.state == .failedSafe)
    }

    @Test("a refusal without an action id reports nothing and polls the state")
    func refusalWithoutIDStaysSilent() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                var payload = Self.actionPayload()
                payload.removeValue(forKey: "action_id")
                var receipt = chatReceipt("source_in_progress", tool: "calendar.create_event")
                receipt["device_action"] = payload
                return .ok(receipt)
            case ("GET", "/v1/operations/op-1"):
                // One poll answers terminal; nothing may report to the
                // device-actions endpoint, which does not know this action.
                return .ok(chatReceipt("succeeded", tool: "calendar.create_event", recordID: "EK-1"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, _) = try await makeChat(service: service)

        let final = try await chat.send(text: "安排一件事")

        // The poll actually happened (the state was read back) and no report
        // was sent for an action this device cannot name.
        #expect(service.count("GET", "/v1/operations/op-1") >= 1)
        #expect(service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }.isEmpty)
        #expect(final.state == .succeeded)
    }

    @Test("a reply without a device action never touches the report endpoint")
    func plainReplySkipsTheFlow() async throws {
        let service = Service()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .ok(chatReceipt("succeeded", tool: "finance.log_expense", recordID: "rec-1"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let executor = StubDeviceActionExecutor()
        let (chat, _, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )

        let final = try await chat.send(text: "咖啡 18 个人支出")

        #expect(executor.actions.isEmpty)
        #expect(service.log.filter { $0.path.hasPrefix("/v1/device-actions/") }.isEmpty)
        #expect(final.outcome == .recorded(recordID: "rec-1", tool: "finance.log_expense", record: nil))
    }

    @Test("a denied save is the device's zero-write testimony")
    func deniedSaveReportsDenied() async throws {
        let service = Service()
        answerWithAction(service)
        let executor = StubDeviceActionExecutor()
        executor.outcome = .denied
        let (chat, session, _) = try await makeChat(
            service: service, deviceActionExecutor: executor
        )
        executor.backend = session

        let final = try await chat.send(text: "周六下午三点网球")

        let report = try #require(service.log.first { $0.path.hasPrefix("/v1/device-actions/") })
        #expect(report.string("result") == "denied")
        #expect(final.state == .failedSafe)
    }
}
