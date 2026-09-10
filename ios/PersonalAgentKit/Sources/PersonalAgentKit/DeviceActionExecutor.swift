import Foundation

/// The concrete device-action executor: EventKit writes the server authorised,
/// then the report that settles the parked operation.
///
/// It is the only component that knows both halves of the exchange — the
/// draft EventKit accepts and the vocabulary the server's report endpoint
/// refuses anything outside — so the mapping lives here once and cannot
/// diverge between the write and its testimony.
///
/// The report is *always* sent, in every branch, including transport errors
/// during the report itself: a caught send error degrades to nothing here
/// (the server's 15-minute sweep is the remaining witness), never a retry of
/// the *save* — retrying the save behind a lost report reply is how a
/// duplicate event gets born, and local dedup is a mitigation, not a licence.
public struct DeviceEventActionExecutor: DeviceActionExecuting {
    private let store: any CalendarStore
    private let backend: any ChatBackend

    public init(store: any CalendarStore, backend: any ChatBackend) {
        self.store = store
        self.backend = backend
    }

    public func executeAndReport(_ action: DeviceEventAction) async -> OperationReceipt? {
        // The decode layer already pinned start/end to parseable instants; a
        // nil here is unreachable in practice, but a guessed instant is worse
        // than a reported failure, so it fails closed as one.
        guard let draft = action.event.draft() else {
            return await report(
                actionID: action.actionID,
                body: .failed(detail: "the authorised event times are not readable instants")
            )
        }
        let body: DeviceActionResultBody
        switch await store.save(draft) {
        case .created(let eventID):
            // Written before the report, not after: the event exists the
            // moment `save` returns, and the record is what lets a later
            // snapshot recognise it as the agent's own (§3.2, §8). A crash
            // between the two leaves the event recorded and the operation
            // parked for the server's sweep — the recoverable direction.
            await store.recordAgentCreated(
                AgentCreatedEvent(createdBy: action, eventID: eventID)
            )
            body = .created(eventID: eventID)
        case .duplicate(let existingID):
            // Deliberately not recorded. A duplicate is a same-title event
            // within ±5 minutes that this action did not write — it may be
            // the user's own, and claiming it would tell the mirror to read
            // an all-day event's dates off this action instead of the device.
            // The schema's default is the honest answer either way.
            body = .duplicate(eventID: existingID)
        case .denied:
            // The device's own zero-write testimony: the user refused access.
            body = .denied(detail: "calendar access was refused on this device")
        case .failed(let detail):
            body = .failed(detail: detail)
        }
        return await report(actionID: action.actionID, body: body)
    }

    public func failedReport(detail: String) -> DeviceActionResultBody {
        .failed(detail: detail)
    }

    /// Send the report. The endpoint answers with the settled projection; a
    /// transport failure degrades to the server's own sweep, silently — the
    /// error is logged for the trail, never re-thrown into the chat flow,
    /// whose outcome is the operation the server still owns.
    ///
    /// The lost reply is `nil`, not a receipt this method builds. The caller
    /// already holds the parked projection of the operation it is polling, and
    /// that one carries the *operation's* id — the action id is the idempotency
    /// key the report endpoint is addressed by, never an operation id, and the
    /// caller's poll reads operations by id. (Review R9, 2026-09-08, and design
    /// §4.2: with several actions in one reply there is no single id left to
    /// rebuild it from.)
    private func report(
        actionID: String, body: DeviceActionResultBody
    ) async -> OperationReceipt? {
        try? await backend.reportDeviceActionResult(actionID: actionID, body: body)
    }
}
