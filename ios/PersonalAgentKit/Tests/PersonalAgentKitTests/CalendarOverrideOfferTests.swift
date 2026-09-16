import Foundation
import Testing

@testable import PersonalAgentKit

/// 「仍要创建」 — the button, and the cases where it must not be there.
///
/// The failure set is written first, as §5.1 requires, because this button is
/// the one control in the client whose wrong appearance writes to the user's
/// real calendar. The safe direction is not symmetric:
///
/// - a button that should not be there creates a **second copy** of an event the
///   user already has, silently, under a card that said the write was fine;
/// - a button that should be there and is missing costs one retry by hand.
///
/// So every case below that is not a confirmed duplicate ends at `.notOffered`,
/// including the ones where the client simply could not find out — a failed
/// lookup, an unreachable server, a projection that still does not answer. The
/// one thing that must never happen is an action id being *guessed*, and
/// `overrideDecision` cannot guess one: it carries the id or it carries nothing.
///
/// The counterparty for the lookup tests is the **real** `DeviceSession` over a
/// stubbed `URLProtocol` (the harness in `ChatTimelineTests.swift`), so the real
/// request construction, the real decoding and the real refresh policy are all
/// in the loop and only HTTP is fake.

// --- the rule, with no network in it ------------------------------------------

@Suite("The override decision")
struct OverrideDecisionTests {

    private func outcome(
        tool: String? = "calendar.create_event",
        evidence: CalendarDeviceResult = .duplicate,
        actionID: String? = "action-1",
        eventID: String? = "EK-1"
    ) -> OperationOutcome {
        .calendarEventWritten(
            eventID: eventID ?? "", tool: tool, evidence: evidence, actionID: actionID
        )
    }

    @Test("a duplicate the server located is the one case the button appears in")
    func theOneOfferedCase() {
        #expect(outcome().overrideDecision == .offered(actionID: "action-1"))
        #expect(outcome().overrideDecision.actionID == "action-1")
    }

    @Test("a created event offers nothing, however it is read")
    func createdIsNeverOffered() {
        // The phone wrote this event. Offering 「仍要创建」 here would write a
        // second copy of an event that exists *because the user already asked
        // for it* -- and the card would have said 已创建 as it did so.
        #expect(outcome(evidence: .created).overrideDecision == .notOffered)
        #expect(outcome(evidence: .created).overrideIsUndecided == false)
    }

    @Test("an unstated result offers nothing by itself and is the one case worth asking about")
    func unstatedIsUndecidedNotOffered() {
        // Two different questions. `overrideDecision` is "may I draw the button
        // now", and the answer on a receipt that never carried the field is no.
        // `overrideIsUndecided` is "is there anything left to ask", and here
        // there is: the operation row behind the event may still carry both.
        let unstated = outcome(evidence: .unstated, actionID: nil)
        #expect(unstated.overrideDecision == .notOffered)
        #expect(unstated.overrideIsUndecided)
    }

    @Test("a duplicate with no action id asks rather than drawing a button that cannot be sent")
    func duplicateWithoutAnIDIsUndecided() {
        let orphan = outcome(actionID: nil)
        #expect(orphan.overrideDecision == .notOffered)
        #expect(orphan.overrideIsUndecided)
    }

    @Test("an empty action id is no action id")
    func emptyActionIDIsNotAnIdentity() {
        // A `""` that reached `POST /v1/device/actions//override` would be a
        // request to override nothing. The server would refuse it, but the
        // button should never have been drawn for the user to press.
        #expect(outcome(actionID: "").overrideDecision == .notOffered)
        #expect(outcome(actionID: "").overrideIsUndecided)
    }

    @Test("a decided card never asks the server")
    func decidedCardsDoNotAsk() async throws {
        let service = Service()
        service.answer { call, _ in
            // Reached only if the client asked a question its own receipt had
            // already answered -- which is the difference between a fallback and
            // a request per calendar card in the history.
            .error(500, "SHOULD_NOT_HAVE_BEEN_ASKED: \(call.path)")
        }
        let (chat, _, _) = try await makeChat(service: service)

        for decided in [
            outcome(evidence: .created),
            outcome(evidence: .duplicate, actionID: "action-1"),
        ] {
            #expect(
                await chat.overrideDecision(frozen: decided, operationID: "op-1")
                    == decided.overrideDecision
            )
        }
        #expect(service.count("GET", "/v1/operations/op-1") == 0)
    }

    // --- the lookup, over the real client ------------------------------------

    private func duplicateProjection(operation: String = "op-1") -> Reply {
        .ok(
            chatReceipt(
                "succeeded",
                operation: operation,
                tool: "calendar.create_event",
                domain: "calendar",
                recordID: "EK-1",
                extra: ["device_result": "duplicate", "device_action_id": "action-1"]
            )
        )
    }

    @Test("a history event frozen before the fields asks the server and gets the button")
    func theLookupFillsInWhatTheEventFrozeOut() async throws {
        let service = Service()
        service.answer { _, _ in duplicateProjection() }
        let (chat, _, _) = try await makeChat(service: service)

        // The real shape of the gap: this is the event a build *before* the
        // projection is holding, and its own fields cannot decide the question.
        let frozen = outcome(evidence: .unstated, actionID: nil)
        #expect(frozen.overrideDecision == .notOffered)

        #expect(
            await chat.overrideDecision(frozen: frozen, operationID: "op-1")
                == .offered(actionID: "action-1")
        )
        #expect(service.count("GET", "/v1/operations/op-1") == 1)
    }

    @Test("a lookup the server refuses offers nothing")
    func aRefusedLookupFailsClosed() async throws {
        let service = Service()
        service.answer { _, _ in .error(500, "INTERNAL_ERROR") }
        let (chat, _, _) = try await makeChat(service: service)

        #expect(
            await chat.overrideDecision(
                frozen: outcome(evidence: .unstated, actionID: nil),
                operationID: "op-1"
            ) == .notOffered
        )
    }

    @Test("a projection that still does not answer offers nothing")
    func aSilentProjectionFailsClosed() async throws {
        // The operation row exists but names no device result: the honest answer
        // is that this client does not know whether an event is already there,
        // and not knowing is not permission.
        let service = Service()
        service.answer { _, _ in
            .ok(
                chatReceipt(
                    "succeeded", tool: "calendar.create_event", domain: "calendar",
                    recordID: "EK-1"
                )
            )
        }
        let (chat, _, _) = try await makeChat(service: service)

        #expect(
            await chat.overrideDecision(
                frozen: outcome(evidence: .unstated, actionID: nil),
                operationID: "op-1"
            ) == .notOffered
        )
    }

    @Test("a Finance projection carries no action id, so no button")
    func financeReceiptsHaveNoDeviceAction() async throws {
        let service = Service()
        service.answer { _, _ in
            .ok(
                chatReceipt(
                    "succeeded", tool: "finance.log_expense", domain: "finance",
                    recordID: "rec-1",
                    // The server emits the key for every receipt and null for
                    // every operation that is not device-executed.
                    extra: ["device_result": NSNull(), "device_action_id": NSNull()]
                )
            )
        }
        let (chat, _, _) = try await makeChat(service: service)

        let frozen = try JSONDecoder().decode(
            OperationReceipt.self,
            from: chatJSON(
                chatReceipt(
                    "succeeded", tool: "finance.log_expense", domain: "finance",
                    recordID: "rec-1"
                )
            )
        ).outcome
        #expect(frozen.overrideIsUndecided == false)
        #expect(
            await chat.overrideDecision(frozen: frozen, operationID: "op-1")
                == .notOffered
        )
        #expect(service.count("GET", "/v1/operations/op-1") == 0)
    }
}

// --- the wire ----------------------------------------------------------------

@Suite("The override's fields on the wire")
struct OverrideWireTests {

    private func receipt(_ extra: [String: Any]) throws -> OperationReceipt {
        try try JSONDecoder().decode(
            OperationReceipt.self, from: chatJSON(
                chatReceipt(
                    "succeeded", tool: "calendar.create_event", domain: "calendar",
                    recordID: "EK-1", extra: extra
                )
            )
        )
    }

    @Test("the receipt reads device_action_id as written")
    func theActionIDIsDecoded() throws {
        let decoded = try receipt(["device_result": "duplicate", "device_action_id": "action-1"])
        #expect(decoded.deviceActionID == "action-1")
        #expect(decoded.outcome.overrideDecision == .offered(actionID: "action-1"))
    }

    @Test("an absent device_action_id stays absent rather than being derived")
    func theActionIDIsNotDerivedFromTheOperation() throws {
        // The operation id and the action id are equal for a device action by
        // the *server's* construction. A client that filled this field in from
        // `operation_id` would be asserting a server invariant it cannot check,
        // and would do it for the operations that are not device actions too.
        let decoded = try receipt([:])
        #expect(decoded.operationID == "op-1")
        #expect(decoded.deviceActionID == nil)
        #expect(decoded.outcome.overrideDecision == .notOffered)
    }

    @Test("the action id is not the event id")
    func theActionIDIsNotTheEventID() throws {
        // Two facts in one receipt. Sending the EventKit identifier to the
        // override endpoint is refused by the server's pin on this field being
        // the action, so the two must not be interchangeable here either.
        let decoded = try receipt(["device_result": "duplicate", "device_action_id": "action-1"])
        #expect(decoded.recordID == "EK-1")
        #expect(decoded.deviceActionID != decoded.recordID)
        #expect(
            decoded.outcome.overrideDecision == .offered(actionID: "action-1")
        )
    }

    @Test("a history event that never carried the field asks instead of guessing")
    func theFrozenEventDecodesToUndecided() throws {
        var content: [String: Any] = [
            "state": "succeeded",
            "tool": "calendar.create_event",
            "domain": "calendar",
            "record_id": "EK-1",
        ]
        let event = try JSONDecoder().decode(
            TimelineEvent.self,
            from: chatJSON(chatEvent("ev-1", type: "operation_result", content: content))
        )
        guard case .operationResult(let outcome, _, _) = event.kind else {
            Issue.record("not an operation result")
            return
        }
        #expect(outcome.overrideDecision == .notOffered)
        #expect(outcome.overrideIsUndecided)

        // The same event, as the server appends it now: the question is
        // answered by the event itself and the card never asks.
        content["device_result"] = "duplicate"
        content["device_action_id"] = "action-1"
        let current = try JSONDecoder().decode(
            TimelineEvent.self,
            from: chatJSON(chatEvent("ev-2", type: "operation_result", content: content))
        )
        guard case .operationResult(let answered, _, _) = current.kind else {
            Issue.record("not an operation result")
            return
        }
        #expect(answered.overrideDecision == .offered(actionID: "action-1"))
        #expect(answered.overrideIsUndecided == false)
    }
}
