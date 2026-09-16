import Foundation
import Testing

@testable import PersonalAgentKit

/// `DEV-031`'s failure set for the duplicate decision path, designed against the
/// task's acceptance rule — 决策幂等 — before the implementation.
///
/// Every case is a way the client could turn one user choice into two server
/// decisions, or lose which choice was made:
///
/// - a lost reply, an app restart between tap and reply, and a change of mind
///   while the first decision is unconfirmed;
/// - a server refusal that proves the decision never landed (which must free the
///   check) versus a 5xx that proves nothing (which must keep it locked);
/// - a `write_anyway` whose override operation then needs polling, including one
///   that ends indeterminate — the decision is durable, the outcome is the
///   server's to show.
///
/// The counterparty is the **real** `AgentClient` and `DeviceSession` over the
/// shared stubbed `URLProtocol` harness from `ChatTimelineTests.swift`, so the
/// real request construction, decoding and one-retry token policy are in the
/// loop. Only HTTP is fake.
@Suite("The DEV-031 duplicate decision path", .serialized)
struct DuplicateDecisionTests {
    private func newService() -> Service { Service() }

    private let decisionPath = "/v1/duplicate-checks/chk-1/decision"

    private func storedDecisions(
        _ store: CredentialStore
    ) throws -> [ChatTimeline.PendingDuplicateDecision] {
        guard let data = try store.read(CredentialKey.pendingDuplicateDecisions)
        else { return [] }
        return try JSONDecoder().decode(
            [ChatTimeline.PendingDuplicateDecision].self, from: data
        )
    }

    @Test("dismiss is sent once and frees the check")
    func dismissResolves() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/duplicate-checks/chk-1/decision"):
                return .ok(
                    chatReceipt("cancelled_pre_submit", operation: "op-1", duplicateCheckID: "chk-1")
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        let receipt = try await chat.decide(checkID: "chk-1", decision: .dismiss)

        #expect(receipt.outcome == .cancelledBeforeSubmit)
        let posts = service.calls("POST", decisionPath)
        #expect(posts.count == 1)
        #expect(posts.first?.string("decision") == "dismiss")
        #expect(try storedDecisions(store).isEmpty)
    }

    @Test("write_anyway is confirmed, then the override operation is polled to its evidence")
    func writeAnywayPollsOverrideOperation() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/duplicate-checks/chk-1/decision"):
                return .accepted(chatReceipt("accepted", operation: "op-2", duplicateCheckID: "chk-1"))
            case ("GET", "/v1/operations/op-2"):
                return .ok(
                    chatReceipt("succeeded", operation: "op-2", tool: "finance.log_expense", recordID: "rec-9")
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        let receipt = try await chat.decide(checkID: "chk-1", decision: .writeAnyway)

        #expect(receipt.outcome == .recorded(recordID: "rec-9", tool: "finance.log_expense", record: nil))
        let posts = service.calls("POST", decisionPath)
        #expect(posts.count == 1)
        #expect(posts.first?.string("decision") == "write_anyway")
        #expect(service.count("GET", "/v1/operations/op-2") == 1)
        #expect(try storedDecisions(store).isEmpty)
    }

    @Test("a lost reply replays the same key and the same decision, never a second one")
    func lostReplyReplaysSameKey() async throws {
        let service = newService()
        service.answer { call, seen in
            switch (call.method, call.path) {
            case ("POST", "/v1/duplicate-checks/chk-1/decision"):
                // The first request may have landed; the reply is lost either way.
                if seen == 0 { return .init(status: 599, body: Data()) }
                return .accepted(chatReceipt("accepted", operation: "op-2", duplicateCheckID: "chk-1"))
            case ("GET", "/v1/operations/op-2"):
                return .ok(
                    chatReceipt("succeeded", operation: "op-2", tool: "finance.log_expense", recordID: "rec-9")
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.serverError(status: 599)) {
            _ = try await chat.decide(checkID: "chk-1", decision: .writeAnyway)
        }
        #expect(try storedDecisions(store).count == 1)

        let receipt = try await chat.decide(checkID: "chk-1", decision: .writeAnyway)

        #expect(receipt.outcome.provesWrite)
        let posts = service.calls("POST", decisionPath)
        #expect(posts.count == 2)
        #expect(
            Set(posts.compactMap(\.idempotencyKey)).count == 1,
            "a retry must re-present the decision's original idempotency key"
        )
        #expect(Set(posts.compactMap { $0.string("decision") }) == ["write_anyway"])
        #expect(try storedDecisions(store).isEmpty)
    }

    @Test("the other choice is locked while a decision is unconfirmed")
    func changeOfMindIsLocked() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasPrefix("/v1/duplicate-checks/")
                ? .error(500, "INTERNAL_ERROR")
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, _) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.serverError(status: 500)) {
            _ = try await chat.decide(checkID: "chk-1", decision: .writeAnyway)
        }
        let pending = try #require(try await chat.pendingDecisions().first)
        await #expect(
            throws: ChatTimeline.ChatError.lockedDuplicateDecision(pending)
        ) {
            _ = try await chat.decide(checkID: "chk-1", decision: .dismiss)
        }
        // The lock is local and total: no second request left the device, because
        // the server would either refuse it or — worse — have taken the first one.
        #expect(service.calls("POST", decisionPath).count == 1)
    }

    @Test("an app restart re-presents the stored key instead of minting a new one")
    func restartReplaysStoredKey() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/duplicate-checks/chk-1/decision"):
                return .ok(
                    chatReceipt("cancelled_pre_submit", operation: "op-1", duplicateCheckID: "chk-1")
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let store = InMemoryCredentialStore()
        // The state a previous launch left behind: tapped, persisted, reply lost.
        let pending = ChatTimeline.PendingDuplicateDecision(
            checkID: "chk-1", decision: .dismiss, idempotencyKey: "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        )
        try store.write(
            CredentialKey.pendingDuplicateDecisions,
            value: try JSONEncoder().encode([pending])
        )
        let (chat, _, _) = try await makeChat(service: service, store: store)

        let receipts = try await chat.resumeDecisions()

        #expect(receipts.count == 1)
        #expect(receipts.first?.outcome == .cancelledBeforeSubmit)
        let posts = service.calls("POST", decisionPath)
        #expect(posts.count == 1)
        #expect(posts.first?.idempotencyKey == "3f2504e0-4f89-41d3-9a0c-0305e82c3301")
        #expect(posts.first?.string("decision") == "dismiss")
        #expect(try storedDecisions(store).isEmpty)
    }

    @Test("an already-resolved check frees the slot and says so")
    func alreadyResolvedFreesSlot() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasPrefix("/v1/duplicate-checks/")
                ? .error(400, "INVALID_ARGUMENT")
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.badRequest(code: "INVALID_ARGUMENT")) {
            _ = try await chat.decide(checkID: "chk-1", decision: .dismiss)
        }
        // A 400 here proves the check is not parked — resolved or never existed —
        // so nothing will land from a retry and the slot must not lock it.
        #expect(try storedDecisions(store).isEmpty)
    }

    @Test("an idempotency conflict frees the slot, because the retry can never succeed")
    func idempotencyConflictFreesSlot() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasPrefix("/v1/duplicate-checks/")
                ? .error(409, "IDEMPOTENCY_CONFLICT")
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.idempotencyConflict) {
            _ = try await chat.decide(checkID: "chk-1", decision: .dismiss)
        }
        #expect(try storedDecisions(store).isEmpty)
        #expect(service.calls("POST", decisionPath).count == 1)
    }

    @Test("a server error keeps the slot, because it proves nothing")
    func serverErrorKeepsSlot() async throws {
        let service = newService()
        service.answer { call, _ in
            call.path.hasPrefix("/v1/duplicate-checks/")
                ? .error(500, "INTERNAL_ERROR")
                : .error(404, "NOT_FOUND")
        }
        let (chat, _, store) = try await makeChat(service: service)

        await #expect(throws: AgentClientError.serverError(status: 500)) {
            _ = try await chat.decide(checkID: "chk-1", decision: .dismiss)
        }
        #expect(try storedDecisions(store).count == 1)
        #expect(service.calls("POST", decisionPath).count == 1)
    }

    @Test("every unconfirmed decision is resumed under its own key")
    func resumePresentsEachStoredKey() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/duplicate-checks/chk-1/decision"):
                return .ok(
                    chatReceipt("cancelled_pre_submit", operation: "op-1", duplicateCheckID: "chk-1")
                )
            case ("POST", "/v1/duplicate-checks/chk-2/decision"):
                return .ok(
                    chatReceipt("cancelled_pre_submit", operation: "op-2", duplicateCheckID: "chk-2")
                )
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let store = InMemoryCredentialStore()
        let pending = [
            ChatTimeline.PendingDuplicateDecision(
                checkID: "chk-1", decision: .dismiss, idempotencyKey: "3f2504e0-4f89-41d3-9a0c-0305e82c3302"
            ),
            ChatTimeline.PendingDuplicateDecision(
                checkID: "chk-2", decision: .dismiss, idempotencyKey: "3f2504e0-4f89-41d3-9a0c-0305e82c3303"
            ),
        ]
        try store.write(
            CredentialKey.pendingDuplicateDecisions,
            value: try JSONEncoder().encode(pending)
        )
        let (chat, _, _) = try await makeChat(service: service, store: store)

        let receipts = try await chat.resumeDecisions()

        #expect(receipts.count == 2)
        #expect(
            service.calls("POST", "/v1/duplicate-checks/chk-1/decision")
                .first?.idempotencyKey == "3f2504e0-4f89-41d3-9a0c-0305e82c3302"
        )
        #expect(
            service.calls("POST", "/v1/duplicate-checks/chk-2/decision")
                .first?.idempotencyKey == "3f2504e0-4f89-41d3-9a0c-0305e82c3303"
        )
        #expect(try storedDecisions(store).isEmpty)
    }

    @Test("write_anyway ending indeterminate still frees the decision slot")
    func indeterminateOverrideStillFreesSlot() async throws {
        let service = newService()
        service.answer { call, _ in
            switch (call.method, call.path) {
            case ("POST", "/v1/duplicate-checks/chk-1/decision"):
                return .accepted(chatReceipt("accepted", operation: "op-2", duplicateCheckID: "chk-1"))
            case ("GET", "/v1/operations/op-2"):
                // A state this build does not know. The decision is durable; the
                // write's outcome is the server's to show, never the client's to
                // re-request with a second key.
                return .ok(chatReceipt("teleported", operation: "op-2"))
            default:
                return .error(404, "NOT_FOUND")
            }
        }
        let (chat, _, store) = try await makeChat(service: service)

        let receipt = try await chat.decide(checkID: "chk-1", decision: .writeAnyway)

        #expect(receipt.outcome == .indeterminate(state: "teleported"))
        #expect(try storedDecisions(store).isEmpty)
        #expect(service.calls("POST", decisionPath).count == 1)
    }

    @Test("discarding is local, deliberate and silent on the wire")
    func discardIsLocalOnly() async throws {
        let service = newService()
        service.answer { _, _ in .error(500, "INTERNAL_ERROR") }
        let store = InMemoryCredentialStore()
        let pending = ChatTimeline.PendingDuplicateDecision(
            checkID: "chk-1", decision: .dismiss, idempotencyKey: "3f2504e0-4f89-41d3-9a0c-0305e82c3302"
        )
        try store.write(
            CredentialKey.pendingDuplicateDecisions,
            value: try JSONEncoder().encode([pending])
        )
        let (chat, _, _) = try await makeChat(service: service, store: store)

        try await chat.discardDecision(checkID: "chk-1")

        #expect(try storedDecisions(store).isEmpty)
        #expect(service.calls("POST", decisionPath).isEmpty)
    }

    @Test("an unusable local decision record is reported, not discarded")
    func malformedDecisionRecord() async throws {
        let service = newService()
        let store = InMemoryCredentialStore()
        try store.write(CredentialKey.pendingDuplicateDecisions, value: Data("{".utf8))
        let (chat, _, _) = try await makeChat(service: service, store: store)

        await #expect(throws: ChatTimeline.ChatError.pendingDecisionsMalformed) {
            _ = try await chat.pendingDecisions()
        }
        await #expect(throws: ChatTimeline.ChatError.pendingDecisionsMalformed) {
            _ = try await chat.resumeDecisions()
        }
    }

    @Test("the wire values are the server's own vocabulary")
    func decisionWireValues() throws {
        let encoded = try JSONEncoder().encode(DuplicateDecision.writeAnyway)
        #expect(String(data: encoded, encoding: .utf8) == "\"write_anyway\"")
        #expect(DuplicateDecision.dismiss.rawValue == "dismiss")
        #expect(DuplicateDecision(rawValue: "maybe") == nil)
    }
}
