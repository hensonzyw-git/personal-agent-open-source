import Foundation
import Testing

@testable import PersonalAgentKit

/// `DEV-040`'s failure set for the human resolution path, designed before the
/// implementation as §5.1 requires.
///
/// The affordance exists because `needs_manual_review` was a dead end: the state
/// is terminal, the write may or may not have landed, and the only local escape
/// was 丢弃 — which asks for no verification and leaves no trace. Recording what a
/// person actually saw in the ledger is therefore the *stronger* exit, and every
/// case below is a way this client could weaken it again:
///
/// - reporting a conclusion the server did not accept, or reading a body that
///   never said one was recorded;
/// - releasing the durable send slot on a failure, on a different operation, or
///   on an operation the server did not answer about — each of which frees the
///   next message to mint a second key for an intent that may already have
///   written;
/// - turning a human's report into evidence: the receipt above the card must stay
///   需要人工核对, and a resolution this build cannot name must stay visible rather
///   than reading as unanswered, because an unanswered card invites a second,
///   contradicting tap the server will refuse.
///
/// The counterparty is the **real** `AgentClient` and `DeviceSession` over the
/// shared stubbed `URLProtocol` harness from `ChatTimelineTests.swift`. Only HTTP
/// is fake.
@Suite("The DEV-040 manual-review resolution", .serialized)
struct ManualReviewResolutionTests {
    private func newService() -> Service { Service() }

    private let resolutionPath = "/v1/operations/op-1/resolution"

    /// The server's own reply shape for `POST /v1/operations/{id}/resolution`.
    /// Deliberately built here rather than reused from `chatReceipt`: the server
    /// refused to widen `chat_receipt_projection_v4` for this, and a fixture that
    /// merged the two would hide it if the client ever started decoding one as the
    /// other.
    private func resolutionBody(
        operation: String = "op-1",
        state: String = "needs_manual_review",
        resolution: Any = "confirmed_written",
        resolvedAt: Any = "2026-08-04T09:00:00+00:00",
        recorded: Any = true
    ) -> [String: Any] {
        [
            "operation_id": operation,
            "state": state,
            "manual_resolution": resolution,
            "manual_resolved_at": resolvedAt,
            "recorded": recorded,
        ]
    }

    /// One chat send that ends parked at `needs_manual_review`, which is the only
    /// state this path applies to and the state that holds the durable slot.
    private func parkedChat(
        _ service: Service,
        resolution: @escaping @Sendable (Call, Int) -> Reply
    ) async throws -> (ChatTimeline, CredentialStore) {
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/chat/messages"):
                return .ok(
                    chatReceipt(
                        "needs_manual_review",
                        recordID: "rec-42",
                        failureReason: "RECEIPT_MISMATCH"
                    )
                )
            case ("POST", let path) where path.hasSuffix("/resolution"):
                return resolution(call, seen)
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)
        let receipt = try await chat.send(text: "咖啡 18 个人支出")
        // The premise of every case below: parked, and holding the slot.
        #expect(
            receipt.outcome
                == .needsManualReview(reason: "RECEIPT_MISMATCH", recordID: "rec-42")
        )
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
        return (chat, store)
    }

    // --- what is sent ---------------------------------------------------------

    @Test("the conclusion travels as the server's vocabulary and mints no key")
    func resolutionIsSentWithoutAnIdempotencyKey() async throws {
        let service = newService()
        let (chat, _) = try await parkedChat(service) { _, _ in
            .ok(self.resolutionBody())
        }

        let receipt = try await chat.resolveManualReview(
            operationID: "op-1", resolution: .confirmedWritten
        )

        #expect(receipt.resolution == "confirmed_written")
        #expect(receipt.recorded)
        // The state is carried, and it is still what the *system* proved. A
        // resolution never promotes it.
        #expect(receipt.state == .needsManualReview)
        let posts = service.calls("POST", resolutionPath)
        #expect(posts.count == 1)
        #expect(posts.first?.string("resolution") == "confirmed_written")
        // No durable local slot is created for this call, so there is no key to
        // send and nothing that can be left held. Asserted rather than assumed:
        // the two other write paths do mint one, and copying that pattern here
        // would add a second slot with nothing to protect.
        #expect(posts.first?.idempotencyKey == nil)
    }

    @Test("confirmed_not_written is sent as itself, not as the safer-looking one")
    func theOtherConclusionIsSentVerbatim() async throws {
        let service = newService()
        let (chat, _) = try await parkedChat(service) { _, _ in
            .ok(self.resolutionBody(resolution: "confirmed_not_written"))
        }

        let receipt = try await chat.resolveManualReview(
            operationID: "op-1", resolution: .confirmedNotWritten
        )

        #expect(receipt.resolution == "confirmed_not_written")
        #expect(service.calls("POST", resolutionPath).first?.string("resolution")
            == "confirmed_not_written")
    }

    @Test("a replay of the same conclusion is an answer, not a failure")
    func aReplayedResolutionIsNotAnError() async throws {
        let service = newService()
        let (chat, _) = try await parkedChat(service) { _, _ in
            // What the server returns for a tap it has already recorded.
            .ok(self.resolutionBody(recorded: false))
        }

        let receipt = try await chat.resolveManualReview(
            operationID: "op-1", resolution: .confirmedWritten
        )

        #expect(receipt.recorded == false)
        #expect(receipt.resolution == "confirmed_written")
    }

    @Test("the recorded conclusion is the server's, not the one that was tapped")
    func theServersConclusionWins() async throws {
        let service = newService()
        let (chat, _) = try await parkedChat(service) { _, _ in
            // A second device answered first; the server replays *its* answer.
            .ok(self.resolutionBody(resolution: "confirmed_not_written", recorded: false))
        }

        let receipt = try await chat.resolveManualReview(
            operationID: "op-1", resolution: .confirmedNotWritten
        )

        #expect(receipt.resolution == "confirmed_not_written")
    }

    // --- bodies that prove nothing --------------------------------------------

    @Test("a reply with no conclusion is refused, never read as recorded")
    func aBodyWithoutTheResolutionIsRefused() async throws {
        let service = newService()
        let (chat, store) = try await parkedChat(service) { _, _ in
            .ok(self.resolutionBody(resolution: NSNull()))
        }

        await #expect(throws: AgentClientError.malformedResponse) {
            _ = try await chat.resolveManualReview(
                operationID: "op-1", resolution: .confirmedWritten
            )
        }
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("a reply that never says whether it recorded is refused")
    func aBodyWithoutRecordedIsRefused() async throws {
        let service = newService()
        let (chat, store) = try await parkedChat(service) { call, _ in
            var body = self.resolutionBody()
            body.removeValue(forKey: "recorded")
            _ = call
            return .ok(body)
        }

        await #expect(throws: AgentClientError.malformedResponse) {
            _ = try await chat.resolveManualReview(
                operationID: "op-1", resolution: .confirmedWritten
            )
        }
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    // --- the durable slot -----------------------------------------------------

    @Test("an accepted conclusion releases the slot it verified")
    func acceptingReleasesTheSlot() async throws {
        let service = newService()
        let (chat, store) = try await parkedChat(service) { _, _ in
            .ok(self.resolutionBody())
        }

        _ = try await chat.resolveManualReview(
            operationID: "op-1", resolution: .confirmedWritten
        )

        #expect(try store.read(CredentialKey.pendingChatSend) == nil)
        #expect(try await chat.pendingSend() == nil)
        // And the composer is unblocked in the only way that matters: a new
        // message is no longer refused as `unresolvedSend`.
        let next = try await chat.send(text: "午饭 20 个人支出")
        #expect(next.operationID == "op-1")
    }

    @Test("a refused conclusion keeps the slot")
    func aConflictKeepsTheSlot() async throws {
        let service = newService()
        let (chat, store) = try await parkedChat(service) { _, _ in
            // The server's answer to a conclusion that contradicts a recorded one.
            .error(409, "IDEMPOTENCY_CONFLICT")
        }

        await #expect(throws: AgentClientError.idempotencyConflict) {
            _ = try await chat.resolveManualReview(
                operationID: "op-1", resolution: .confirmedNotWritten
            )
        }
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("a server failure keeps the slot: a failed report is not a report")
    func aServerErrorKeepsTheSlot() async throws {
        let service = newService()
        let (chat, store) = try await parkedChat(service) { _, _ in
            .error(500, "INTERNAL_ERROR")
        }

        await #expect(throws: AgentClientError.serverError(status: 500)) {
            _ = try await chat.resolveManualReview(
                operationID: "op-1", resolution: .confirmedWritten
            )
        }
        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("resolving another operation never releases this one's slot")
    func anotherOperationsResolutionLeavesTheSlotAlone() async throws {
        let service = newService()
        let (chat, store) = try await parkedChat(service) { _, _ in
            .ok(self.resolutionBody(operation: "op-99"))
        }

        _ = try await chat.resolveManualReview(
            operationID: "op-99", resolution: .confirmedWritten
        )

        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    @Test("the release follows the operation the server answered about")
    func theServersOperationIdBindsTheRelease() async throws {
        let service = newService()
        // The request names the parked operation; the reply names a different one.
        // Trusting the argument would free a slot the server never spoke about.
        let (chat, store) = try await parkedChat(service) { _, _ in
            .ok(self.resolutionBody(operation: "op-somewhere-else"))
        }

        _ = try await chat.resolveManualReview(
            operationID: "op-1", resolution: .confirmedWritten
        )

        #expect(try store.read(CredentialKey.pendingChatSend) != nil)
    }

    // --- the permanent marker -------------------------------------------------

    @Test("the Timeline marker projects to the conclusion it carries")
    func theMarkerProjects() {
        let event = TimelineEvent(
            eventID: "e1",
            eventType: "manual_review_resolved",
            operationID: "op-1",
            createdAt: "2026-08-04T09:00:00+00:00",
            content: ["resolution": .string("confirmed_not_written")]
        )

        #expect(event.kind == .manualReviewResolved(resolution: "confirmed_not_written"))
    }

    @Test("a conclusion this build cannot name stays visible, not dropped")
    func anUnknownConclusionStaysVisible() {
        let event = TimelineEvent(
            eventID: "e1",
            eventType: "manual_review_resolved",
            operationID: "op-1",
            createdAt: "2026-08-04T09:00:00+00:00",
            content: ["resolution": .string("confirmed_partially_written")]
        )

        // Not `unrecognised`: the card must show that *something* was recorded,
        // or it will offer the buttons again and invite a `409`.
        #expect(
            event.kind == .manualReviewResolved(resolution: "confirmed_partially_written")
        )
    }

    @Test("a marker with no readable conclusion is unreadable, never blank")
    func anEmptyMarkerIsUnrecognised() {
        for content: [String: JSONValue] in [
            [:],
            ["resolution": .string("")],
            ["resolution": .number(1)],
            ["resolution": .null],
        ] {
            let event = TimelineEvent(
                eventID: "e1",
                eventType: "manual_review_resolved",
                operationID: "op-1",
                createdAt: "2026-08-04T09:00:00+00:00",
                content: content
            )
            #expect(event.kind == .unrecognised(eventType: "manual_review_resolved"))
        }
    }

    @Test("a resolved operation's receipt is still 需要人工核对")
    func theReceiptIsNeverPromoted() {
        // The strongest rule in this file: `confirmed_written` is a person's
        // report, and the projection has no input for it at all. Even a
        // `needs_manual_review` carrying a `record_id` — the shape the 2026-08-04
        // drill produced — stays parked.
        let receipt = OperationReceipt(
            operationID: "op-1",
            state: .needsManualReview,
            cancelRequested: false,
            clientDetached: false,
            tool: "finance.log_expense",
            recordID: "rec-42",
            failureReason: "RECEIPT_MISMATCH",
            duplicateCheckID: nil,
            clarification: nil,
            duplicateExisting: nil,
            answer: "已经帮你记好了"
        )

        #expect(
            receipt.outcome
                == .needsManualReview(reason: "RECEIPT_MISMATCH", recordID: "rec-42")
        )
        #expect(receipt.outcome.provesWrite == false)
    }
}
