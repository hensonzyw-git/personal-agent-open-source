import Foundation
import Testing

@testable import PersonalAgentKit

/// The acceptance harness, tested as a harness.
///
/// The acceptance build is the one artefact of this round that a person looks at
/// and pronounces on by eye, which makes two failure modes reachable that no
/// rendering test would catch:
///
/// - **the checklist drifts from the script.** An item that names a card the
///   harness no longer draws is a tick that means nothing, and the walk-through
///   gets shorter without anyone noticing. Every item here is anchored to a
///   seeded event, and the tests below fail if that anchor is missing or does not
///   project to the outcome the item claims.
/// - **the harness seeds a shape the server never writes.** Then the cards look
///   right for a reason that has nothing to do with production. The Swift side
///   pins the outcomes; the Python pin in
///   `tests/unit/test_acceptance_harness_vectors.py` holds the seeded field names
///   against the server's own emitter.
///
/// What is deliberately *not* tested here: that the App target composes this.
/// It cannot be, from this suite — `AcceptanceScene.swift` lives in the App
/// target, which has no test host. That gap is stated in the report rather than
/// papered over.
@Suite("Acceptance scenario")
struct AcceptanceScenarioTests {

    private let seed = AcceptanceScenario.seed()

    private func event(_ id: String) -> TimelineEvent? {
        seed.events.first { $0.eventID == id }
    }

    // --- the checklist and the script are one thing ---------------------------

    @Test("every checklist item names a seeded event")
    func everyItemIsAnchored() {
        let ids = Set(seed.events.map(\.eventID))
        for item in AcceptanceChecklist.items {
            #expect(
                ids.contains(item.anchorEventID),
                "\(item.id) names \(item.anchorEventID), which the harness does not seed"
            )
        }
    }

    @Test("the required coverage is all still in the walk-through")
    func requiredItemsSurvive() {
        let present = Set(AcceptanceChecklist.items.map(\.id))
        for required in AcceptanceChecklist.requiredItemIDs {
            #expect(present.contains(required), "\(required) was dropped")
        }
    }

    @Test("item ids are unique")
    func idsAreUnique() {
        let ids = AcceptanceChecklist.items.map(\.id)
        #expect(Set(ids).count == ids.count)
    }

    @Test("every item says what passing looks like")
    func everyItemHasAWayToFail() {
        for item in AcceptanceChecklist.items {
            // A pass condition has to be checkable by looking. "The card works"
            // is not, and an item that read that way would be ticked by default.
            #expect(!item.pass.isEmpty)
            #expect(item.title.count > 1)
        }
    }

    // --- the seeded cards project to what the checklist claims ----------------

    @Test("the list card is the calendar query, with the all-day range closed")
    func theListCard() throws {
        let receipt = try #require(AcceptanceScenario.receipt("op-accept-list", in: seed))
        guard case .answeredWithCalendarQuery(let result, let tool) = receipt.outcome else {
            Issue.record("op-accept-list is not a calendar query card: \(receipt.outcome)")
            return
        }
        #expect(tool == "calendar.query_events")
        #expect(result.events.count == 2)
        #expect(result.recordCount == 2)
        let allDay = try #require(result.events.first)
        #expect(allDay.allDay)
        #expect(allDay.createdByAgent)
        // `end_date` is exclusive on the wire, so a three-day trip ending 10-03
        // is stored as `2026-10-04`. The row's own label is where the batch-0
        // off-by-one used to be, which is why the checklist quotes it verbatim
        // and this asserts it whole rather than by substring.
        #expect(allDay.endDate == "2026-10-04")
        #expect(allDay.when == "10-01 至 10-03 全天")
        #expect(allDay.line == "东京出差（10-01 至 10-03 全天）")
        #expect(!allDay.line.contains("10-04"))

        let timed = try #require(result.events.last)
        #expect(!timed.allDay)
        #expect(timed.timezone == "Asia/Tokyo")
        // The zone is named in Chinese ("日本时间"), not as the identifier: the
        // row is read on a phone, and the checklist quotes what is on the row.
        // A timed row that folded Tokyo into Shanghai time would still contain
        // `Asia/Tokyo` in its `timezone` field, which is exactly why this
        // asserts the rendered string and not the field.
        #expect(timed.when == "10-02 14:00 日本时间 开始")
        #expect(timed.line == "客户拜访（10-02 14:00 日本时间 开始）")
    }

    @Test("the created receipt offers nothing")
    func theCreatedCard() throws {
        let receipt = try #require(AcceptanceScenario.receipt("op-accept-created", in: seed))
        #expect(receipt.outcome == .calendarEventWritten(
            eventID: "EKA-ACCEPT-0001",
            tool: AcceptanceScenario.deviceTool,
            evidence: .created,
            actionID: "act-accept-created"
        ))
        #expect(receipt.outcome.overrideDecision == .notOffered)
    }

    @Test("the duplicate receipt offers the one button, with the seeded action id")
    func theDuplicateCard() throws {
        let receipt = try #require(
            AcceptanceScenario.receipt("op-accept-duplicate", in: seed)
        )
        #expect(receipt.outcome == .calendarEventWritten(
            eventID: "EKA-ACCEPT-0002",
            tool: AcceptanceScenario.deviceTool,
            evidence: .duplicate,
            actionID: "act-accept-duplicate"
        ))
        // The button the checklist asks the person to look for, decided by the
        // shipping predicate over the seeded fields.
        #expect(receipt.outcome.overrideDecision == .offered(
            actionID: "act-accept-duplicate"
        ))
    }

    @Test("the calendar review card forks to the calendar words")
    func theCalendarReviewCard() throws {
        let receipt = try #require(
            AcceptanceScenario.receipt("op-accept-review-calendar", in: seed)
        )
        guard case .needsManualReview(let reason, let recordID, let domain) = receipt.outcome
        else {
            Issue.record("not a manual-review card: \(receipt.outcome)")
            return
        }
        #expect(reason == "SOURCE_COMMIT_UNKNOWN")
        #expect(recordID == "EKA-ACCEPT-0003")
        let copy = ManualReviewCopy.forDomain(domain)
        #expect(copy == .calendar)
        // The card and the dialog it opens are the same words, which is the
        // whole of what the batch-2 fork bought.
        for word in [
            copy.instruction, copy.writtenButton, copy.notWrittenButton,
            copy.confirmButton, copy.confirmMessage,
            copy.confirmPrompt(forWire: ManualResolution.confirmedWritten.rawValue),
            copy.confirmPrompt(forWire: ManualResolution.confirmedNotWritten.rawValue),
        ] {
            #expect(!word.contains("账本"), "the calendar card names the ledger: \(word)")
        }
    }

    @Test("the pre-step-5 record still falls back to the ledger words")
    func theLegacyReviewCard() throws {
        let receipt = try #require(
            AcceptanceScenario.receipt("op-accept-review-legacy", in: seed)
        )
        guard case .needsManualReview(_, _, let domain) = receipt.outcome else {
            Issue.record("not a manual-review card: \(receipt.outcome)")
            return
        }
        // `nil`, not `"finance"`: the seeded shape is the one the server wrote
        // before the field existed, and filling it in here would make the
        // harness prove a fallback it never exercised.
        #expect(domain == nil)
        #expect(ManualReviewCopy.forDomain(domain) == .ledger)
    }

    @Test("the calendar resolution marker keeps its domain")
    func theCalendarMarker() throws {
        let marker = try #require(event("evt_accept_011"))
        #expect(marker.operationID == "op-accept-review-calendar-done")
        guard case .manualReviewResolved(let resolution, let domain) = marker.kind else {
            Issue.record("not a resolution marker: \(marker.kind)")
            return
        }
        #expect(resolution == ManualResolution.confirmedWritten.rawValue)
        let copy = ManualReviewCopy.forResolvedMarker(domain)
        #expect(copy == .calendar)
        #expect(copy.conclusion(forWire: resolution).contains("日历"))
        #expect(!copy.conclusion(forWire: resolution).contains("账本"))
    }

    @Test("a marker with no domain borrows neither destination's words")
    func theNeutralMarker() throws {
        let marker = try #require(event("evt_accept_014"))
        guard case .manualReviewResolved(let resolution, let domain) = marker.kind else {
            Issue.record("not a resolution marker: \(marker.kind)")
            return
        }
        #expect(domain == nil)
        let copy = ManualReviewCopy.forResolvedMarker(domain)
        #expect(copy == .neutral)
        let conclusion = copy.conclusion(forWire: resolution)
        #expect(!conclusion.contains("账本"))
        #expect(!conclusion.contains("日历"))
    }

    // --- the archive ---------------------------------------------------------

    @Test("the script is deterministic")
    func theSeedsAgree() {
        #expect(AcceptanceScenario.seed() == AcceptanceScenario.seed())
        #expect(seed.conversationID == AcceptanceScenario.conversationID)
        #expect(seed.events.count == AcceptanceScenario.steps.count)
    }

    @Test("every seeded operation is reachable by the id its event carries")
    func everyOperationIsProjected() {
        for event in seed.events {
            guard event.eventType == "operation_result" else { continue }
            let operationID = event.operationID
            #expect(operationID != nil)
            if let operationID {
                #expect(
                    AcceptanceScenario.receipt(operationID, in: seed) != nil,
                    "\(operationID) has an event but no projection"
                )
            }
        }
    }

    @Test("the projection and the event describe the same write")
    func theTwoShapesAgree() throws {
        for event in seed.events where event.eventType == "operation_result" {
            let operationID = try #require(event.operationID)
            let receipt = try #require(AcceptanceScenario.receipt(operationID, in: seed))
            #expect(receipt.operationID == operationID)
            #expect(event.content["state"]?.stringValue == receipt.state.wire)
            #expect(event.content["record_id"]?.stringValue == receipt.recordID)
            // Live and frozen must agree about what the phone reported. These
            // two fields are what 「仍要创建」 is decided from, so a projection
            // that disagreed with its own event would offer the button on the
            // live card and withhold it after a relaunch -- the same write,
            // two answers, decided by which one the person happened to be
            // looking at.
            #expect(
                receipt.deviceResult
                    == CalendarDeviceResult(wire: event.content["device_result"]?.stringValue)
            )
            #expect(
                receipt.deviceActionID == event.content["device_action_id"]?.stringValue
            )
        }
        // The loop above would also pass if nothing carried either field. These
        // two say the seeded archive really does exercise the pair.
        let created = try #require(AcceptanceScenario.receipt("op-accept-created", in: seed))
        #expect(created.deviceResult == .created)
        #expect(created.deviceActionID == "act-accept-created")
    }
}

/// The synthetic counterparties: the seeded backend and the executor that never
/// touches EventKit.
@Suite("Acceptance harness")
struct AcceptanceHarnessTests {

    private let seed = AcceptanceScenario.seed()

    /// A location inside a fresh temporary directory. Never a device path: the
    /// suite must be able to run anywhere, and it must not be able to read a
    /// real archive even if one exists.
    private func temporaryLocation() throws -> AcceptanceArchiveLocation {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("acceptance-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
        return AcceptanceArchiveLocation(
            fileURL: directory.appendingPathComponent("archive.json")
        )
    }

    @Test("the first read seeds the archive and serves the whole script")
    func theFirstReadSeeds() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        let page = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        #expect(page.conversationID == AcceptanceScenario.conversationID)
        #expect(page.events == AcceptanceScenario.seed().events)
        #expect(page.hasOlder == false)
        #expect(page.olderCursor == nil)
        #expect(page.newerCursor != nil)
    }

    @Test("a second launch reads the archive back rather than reseeding it")
    func theRestartRecovers() async throws {
        let location = try temporaryLocation()
        let first = AcceptanceTimeline(location: location)
        _ = try await first.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        // A conclusion recorded in the first launch is the proof that what the
        // second launch reads is the file and not a fresh seed: a reseed would
        // silently drop it.
        _ = try await first.resolveManualReview(
            operationID: "op-accept-review-calendar",
            resolution: .confirmedNotWritten
        )
        let markerCount = try await first.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        ).events.filter { $0.eventType == "manual_review_resolved" }.count

        let second = AcceptanceTimeline(location: location)
        let page = try await second.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        #expect(page.events.filter { $0.eventType == "manual_review_resolved" }.count
            == markerCount)
        #expect(page.events.count == AcceptanceScenario.seed().events.count + 1)
    }

    @Test("incremental sync from the live edge sees only what was appended")
    func theNewerPage() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        let opening = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        let cursor = try #require(opening.newerCursor)
        let quiet = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: cursor, direction: .newer, limit: nil
        )
        #expect(quiet.events.isEmpty)
        // An empty page carries no cursor -- the live edge has not moved, and
        // minting one would claim it had.
        #expect(quiet.newerCursor == nil)

        _ = try await timeline.resolveManualReview(
            operationID: "op-accept-review-calendar",
            resolution: .confirmedWritten
        )
        let after = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: cursor, direction: .newer, limit: nil
        )
        #expect(after.events.count == 1)
        #expect(after.events.first?.eventType == "manual_review_resolved")
    }

    @Test("a cursor this harness did not mint is refused, not guessed at")
    func unknownCursorsRefuse() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        await #expect(throws: AcceptanceHarnessError.unknownCursor("nope")) {
            _ = try await timeline.timelinePage(
                conversationID: AcceptanceScenario.conversationID,
                cursor: "nope", direction: .older, limit: nil
            )
        }
    }

    @Test("the harness refuses everything it has no counterparty for")
    func theRefusalsAreHonest() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        // A model turn and a ledger write are the two things this build most
        // must not appear to have.
        await #expect(throws: AcceptanceHarnessError.noModel) {
            _ = try await timeline.sendChatMessage(
                conversationID: AcceptanceScenario.conversationID,
                text: "你好", clarificationOf: nil,
                startNewSession: false, idempotencyKey: "key"
            )
        }
        await #expect(throws: AcceptanceHarnessError.noModel) {
            _ = try await timeline.updateExpenseCategory(
                recordID: "REC-ACCEPT-0001", category: "交通",
                expectedCurrentCategory: nil, idempotencyKey: "key"
            )
        }
        // The mirror upload is how a device's real calendar reaches the server.
        await #expect(throws: AcceptanceHarnessError.noMirror) {
            _ = try await timeline.uploadCalendarSync(
                windowStart: Date(timeIntervalSince1970: 0),
                windowEnd: Date(timeIntervalSince1970: 0),
                events: [], calendars: [],
                windowComplete: true,
                snapshotAsOf: Date(timeIntervalSince1970: 0)
            )
        }
        await #expect(throws: AcceptanceHarnessError.unknownOperation("op-nope")) {
            _ = try await timeline.operation(operationID: "op-nope")
        }
    }

    @Test("a conclusion is recorded with the operation's own domain")
    func theResolutionCarriesItsDomain() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        let receipt = try await timeline.resolveManualReview(
            operationID: "op-accept-review-calendar",
            resolution: .confirmedWritten
        )
        #expect(receipt.operationID == "op-accept-review-calendar")
        #expect(receipt.resolution == ManualResolution.confirmedWritten.rawValue)
        #expect(receipt.recorded)

        let page = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        let marker = try #require(page.events.last)
        #expect(marker.operationID == "op-accept-review-calendar")
        #expect(marker.content["domain"]?.stringValue == OperationReceipt.calendarDomain)
    }

    @Test("a conclusion on a record with no domain writes no domain")
    func theLegacyResolutionStaysSilent() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        _ = try await timeline.resolveManualReview(
            operationID: "op-accept-review-legacy",
            resolution: .confirmedWritten
        )
        let page = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        let marker = try #require(page.events.last)
        // Absent, not null and not "finance": the marker must not claim a
        // destination the operation it resolves never recorded.
        #expect(marker.content["domain"] == nil)
    }

    @Test("「仍要创建」 lands as a new created card")
    func theOverrideLands() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        let settled = try await timeline.overrideDeviceAction(
            actionID: "act-accept-duplicate"
        )
        guard case .calendarEventWritten(let eventID, _, let evidence, _) = settled.outcome
        else {
            Issue.record("the override did not settle as a write: \(settled.outcome)")
            return
        }
        #expect(evidence == .created)
        #expect(eventID == "EKA-ACCEPT-0002-OVERRIDE")

        // And it is on the Timeline, so a person sees the consequence rather
        // than a card that silently did nothing.
        let page = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        #expect(page.events.last?.operationID == settled.operationID)
    }

    @Test("an override for an action the harness does not know is refused")
    func theUnknownOverrideRefuses() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        await #expect(throws: AcceptanceHarnessError.unknownAction("act-nope")) {
            _ = try await timeline.overrideDeviceAction(actionID: "act-nope")
        }
    }

    @Test("the reset puts the script back")
    func theReset() async throws {
        let timeline = AcceptanceTimeline(location: try temporaryLocation())
        _ = try await timeline.resolveManualReview(
            operationID: "op-accept-review-calendar",
            resolution: .confirmedWritten
        )
        try await timeline.resetToSeed()
        let page = try await timeline.timelinePage(
            conversationID: AcceptanceScenario.conversationID,
            cursor: nil, direction: .older, limit: nil
        )
        #expect(page.events == AcceptanceScenario.seed().events)
    }

    /// The executor the acceptance build composes, over an action the server
    /// really could have issued.
    ///
    /// The point of this type is that it has no EventKit in it, and "has no
    /// EventKit" is not something a test can observe from the outside — the
    /// observable half is that it answers only from its fixed map and never
    /// invents a receipt. The absence of EventKit is held by the App-target
    /// composition and by `check_acceptance_isolation.sh`; this pins the half
    /// that can be pinned here.
    @Test("the executor answers only from what it was handed")
    func theExecutorAnswersOnlyFromItsMap() async throws {
        let action = try DeviceEventAction.decode(from: [
            "action_id": "act-accept-created",
            "tool": DeviceEventAction.supportedTool,
            "event": [
                "title": "东京出差",
                "start": "2026-10-01T00:00:00+09:00",
                "end": "2026-10-04T00:00:00+09:00",
                "all_day": true,
                "start_date": "2026-10-01",
                "end_date": "2026-10-04",
            ],
        ])
        #expect(action.actionID == "act-accept-created")

        // No receipts handed in: `nil`, the protocol's lost-report case, rather
        // than a receipt this build made up.
        let bare = AcceptanceDeviceExecutor()
        #expect(await bare.executeAndReport(action) == nil)

        let receipt = try #require(
            AcceptanceScenario.receipt("op-accept-created", in: seed)
        )
        let mapped = AcceptanceDeviceExecutor(
            receipts: [action.actionID: receipt]
        )
        #expect(await mapped.executeAndReport(action) == receipt)
        // An action id the map does not know is the same answer as no map at
        // all. Never the neighbouring receipt.
        let other = try #require(
            AcceptanceScenario.receipt("op-accept-duplicate", in: seed)
        )
        #expect(await mapped.executeAndReport(action) != other)

        #expect(
            bare.failedReport(detail: "本构建不执行设备动作")
                == .failed(detail: "本构建不执行设备动作")
        )
    }
}
