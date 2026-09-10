import Foundation
import Testing

@testable import PersonalAgentKit

/// The 人工核对 card's domain fork (design §10, gap 4).
///
/// The harm this fork exists to prevent is one-directional: sending a person to
/// the ledger to check a calendar write, or the reverse. Either way they report a
/// conclusion about something that is not there, and the server records it as a
/// human fact it will then refuse to let them contradict. So the tests below are
/// mostly about the *fallback* direction -- which copy an unknown or absent
/// domain draws -- because that is the case a new domain or an old event lands
/// in, and it is the case no happy path exercises.
@Suite("The 人工核对 card's domain fork")
struct ManualReviewCopyTests {
    @Test("the calendar domain selects the calendar card")
    func calendarDomainPicksCalendar() {
        #expect(ManualReviewCopy.forDomain("calendar") == .calendar)
    }

    @Test("the ledger domain selects the ledger card")
    func financeDomainPicksLedger() {
        #expect(ManualReviewCopy.forDomain("finance") == .ledger)
    }

    @Test("no domain draws the card these cards have always drawn")
    func absentDomainFallsBackToLedger() {
        // An operation recorded before `domain` existed. It is not evidence of
        // either domain, and the card it has always shown is the ledger one.
        #expect(ManualReviewCopy.forDomain(nil) == .ledger)
    }

    @Test("a domain this build has never heard of does not borrow the calendar wording")
    func unknownDomainDoesNotBorrowCalendar() {
        // The fail-safe direction. A third domain that borrowed the calendar
        // copy would send its user to the calendar for something else entirely;
        // borrowing the ledger copy is at least the behaviour that predates the
        // fork, and the fork is only ever meant to fire on the one value it
        // knows.
        for domain in ["meta", "", "Calendar", "calendar2", "CALENDAR"] {
            #expect(
                ManualReviewCopy.forDomain(domain) == .ledger,
                "\(domain) selected the calendar card"
            )
        }
    }

    @Test("the two cards name different destinations, labels and conclusions")
    func theTwoCardsActuallyDiffer() {
        // A fork that returned the same copy twice would pass every test above
        // and change nothing on screen.
        #expect(ManualReviewCopy.calendar != ManualReviewCopy.ledger)
        #expect(ManualReviewCopy.calendar.instruction != ManualReviewCopy.ledger.instruction)
        #expect(ManualReviewCopy.calendar.recordLabel != ManualReviewCopy.ledger.recordLabel)
        #expect(ManualReviewCopy.calendar.writtenButton != ManualReviewCopy.ledger.writtenButton)
        #expect(
            ManualReviewCopy.calendar.notWrittenButton
                != ManualReviewCopy.ledger.notWrittenButton
        )
        // The ledger is where the person must *not* be sent for a calendar write.
        #expect(!ManualReviewCopy.calendar.instruction.contains("账本"))
        #expect(ManualReviewCopy.ledger.instruction.contains("账本"))
        #expect(ManualReviewCopy.calendar.instruction.contains("日历"))
    }

    @Test("both conclusion paths are worded, in both cards")
    func everyConclusionIsNamed() {
        // The two paths are the whole point of the card: the record exists, or it
        // does not. `conclusion(forWire:)` must name both in both copies, and its
        // default -- "a conclusion this build cannot name" -- must stay
        // unreachable for either real wire value.
        for copy in [ManualReviewCopy.ledger, ManualReviewCopy.calendar] {
            let written = copy.conclusion(forWire: ManualResolution.confirmedWritten.rawValue)
            let notWritten = copy.conclusion(forWire: ManualResolution.confirmedNotWritten.rawValue)
            #expect(!written.contains("服务端结论"))
            #expect(!notWritten.contains("服务端结论"))
            #expect(written != notWritten)
            #expect(!written.isEmpty)
            #expect(!notWritten.isEmpty)
        }
        #expect(
            ManualReviewCopy.calendar.conclusion(
                forWire: ManualResolution.confirmedWritten.rawValue
            ) == "日历里有这条日程"
        )
        #expect(
            ManualReviewCopy.calendar.conclusion(
                forWire: ManualResolution.confirmedNotWritten.rawValue
            ) == "日历里没有这条日程"
        )
    }

    @Test("a resolution value this build cannot name is still reported as recorded")
    func unknownResolutionIsStillShown() {
        // The same rule the receipt decoder follows: dropping an unnameable
        // conclusion renders an answered card as unanswered, which invites a
        // second, contradicting tap that the server would then refuse.
        let text = ManualReviewCopy.calendar.conclusion(forWire: "confirmed_something_new")
        #expect(text.contains("confirmed_something_new"))
    }
}

// --- the domain on the wire ---------------------------------------------------

@Suite("The domain reaches the projection")
struct ManualReviewDomainTests {
    private func receipt(_ object: [String: Any]) throws -> OperationReceipt {
        try JSONDecoder().decode(OperationReceipt.self, from: chatJSON(object))
    }

    private func event(_ object: [String: Any]) throws -> TimelineEvent {
        try JSONDecoder().decode(TimelineEvent.self, from: chatJSON(object))
    }

    @Test("a calendar receipt carries its domain onto the outcome")
    func calendarReceiptKeepsItsDomain() throws {
        let parsed = try receipt(
            chatReceipt(
                "needs_manual_review",
                tool: "calendar.create_event",
                domain: OperationReceipt.calendarDomain,
                recordID: "evt-1",
                failureReason: "DEVICE_REPORT_TIMEOUT"
            )
        )
        #expect(parsed.domain == OperationReceipt.calendarDomain)
        #expect(
            parsed.outcome
                == .needsManualReview(
                    reason: "DEVICE_REPORT_TIMEOUT",
                    recordID: "evt-1",
                    domain: OperationReceipt.calendarDomain
                )
        )
        // And the card it selects is the calendar one -- the chain the whole
        // step exists for.
        if case .needsManualReview(_, _, let domain) = parsed.outcome {
            #expect(ManualReviewCopy.forDomain(domain) == .calendar)
        } else {
            Issue.record("a needs_manual_review receipt was not projected as one")
        }
    }

    @Test("a receipt with no domain key at all decodes as no domain, not as a default")
    func missingDomainKeyIsNil() throws {
        // The pre-step-5 history shape: the key is not there. Reading that as
        // `finance` would be inventing a fact the server never sent.
        var body = chatReceipt("needs_manual_review", recordID: "rec-1")
        body.removeValue(forKey: "domain")
        let parsed = try receipt(body)
        #expect(parsed.domain == nil)
        #expect(
            parsed.outcome
                == .needsManualReview(reason: nil, recordID: "rec-1", domain: nil)
        )
    }

    @Test("an explicitly null domain and an absent one are the same to the card")
    func nullDomainIsAlsoNoDomain() throws {
        // They are not the same *fact* -- null is the server saying "no tool was
        // recorded", absent is an event written before the field -- but the card
        // has nothing to choose between them with, and both draw the ledger card.
        let parsed = try receipt(
            chatReceipt("needs_manual_review", tool: NSNull(), recordID: "rec-1")
        )
        #expect(parsed.domain == nil)
    }

    @Test("the domain travels with the Timeline event too")
    func timelineEventCarriesTheDomain() throws {
        // Otherwise a restart would re-draw a calendar card as a ledger one.
        let parsed = try event(
            chatEvent(
                "ev-1",
                type: "operation_result",
                operation: "op-1",
                content: [
                    "state": "needs_manual_review",
                    "tool": "calendar.create_event",
                    "domain": OperationReceipt.calendarDomain,
                    "record_id": "evt-1",
                    "failure_reason": "DEVICE_REPORT_TIMEOUT",
                ]
            )
        )
        guard case .operationResult(let outcome, _, _) = parsed.kind else {
            Issue.record("the event was not projected as an operation result")
            return
        }
        #expect(
            outcome
                == .needsManualReview(
                    reason: "DEVICE_REPORT_TIMEOUT",
                    recordID: "evt-1",
                    domain: OperationReceipt.calendarDomain
                )
        )
    }

    @Test("an old Timeline event with no domain draws the ledger card")
    func timelineEventWithoutADomain() throws {
        let parsed = try event(
            chatEvent(
                "ev-1",
                type: "operation_result",
                operation: "op-1",
                content: [
                    "state": "needs_manual_review",
                    "tool": "finance.log_expense",
                    "record_id": "rec-1",
                    "failure_reason": "SOURCE_COMMIT_UNKNOWN",
                ]
            )
        )
        guard case .operationResult(let outcome, _, _) = parsed.kind else {
            Issue.record("the event was not projected as an operation result")
            return
        }
        #expect(
            outcome
                == .needsManualReview(
                    reason: "SOURCE_COMMIT_UNKNOWN", recordID: "rec-1", domain: nil
                )
        )
    }
}
