import Foundation

/// The isolated acceptance build's counterparties: a Timeline that serves the
/// seeded archive, and a device-action executor that answers with fixed
/// receipts.
///
/// ## What these are, and are not
///
/// They are **stand-ins for the server and for EventKit**, and they are worth
/// exactly what a stand-in is worth: the cards drawn from them are the shipping
/// rendering code over the shipping wire shapes, but nothing here validates
/// server policy, model behaviour, or a real calendar. The acceptance build's
/// claim is bounded to what a person can see on the phone, and the checklist is
/// written in those terms.
///
/// The synthetic server is deliberately *not* permissive: every path the seeded
/// archive does not need refuses with a typed error saying so, rather than
/// inventing a plausible answer. A send that silently produced a fake assistant
/// turn would make the acceptance build look like it had a model.
///
/// ## Isolation
///
/// No network, no EventKit, no TCC, no model. The archive is one JSON file
/// inside this app's own container, at a path the caller supplies, so the tests
/// write it to a temporary directory and a device writes it to Application
/// Support — never to a location the ordinary app reads.

// MARK: - errors

/// Every refusal this harness makes, in words a person reading the screen can
/// act on. Each one names what the acceptance build does not have, because a
/// generic "network error" here would read as a defect in the thing under
/// acceptance.
public enum AcceptanceHarnessError: Error, Equatable, LocalizedError {
    /// The harness has no model and no backend, so a message cannot be sent.
    case noModel
    /// The operation id names nothing in the seeded archive.
    case unknownOperation(String)
    /// A cursor this harness did not mint. Cannot happen from its own paging,
    /// which is why it refuses rather than guessing a page.
    case unknownCursor(String)
    /// The mirror uploader must never be reachable from this build.
    case noMirror
    /// The action id names no seeded operation.
    case unknownAction(String)
    /// An archive is on disk and could not be read or decoded.
    ///
    /// A typed refusal rather than a fallback, and the distinction is the whole
    /// of the restart item: a build that quietly re-seeded over a damaged file
    /// would show the same cards, in the same order, with the same wording —
    /// passing 「内容和顺序不变」 while proving nothing about recovery. The
    /// detail is carried so the screen can say which of the two happened.
    case archiveUnreadable(String)

    public var errorDescription: String? {
        switch self {
        case .noModel:
            return "验收构建没有模型与后端：这里只能看卡片，不能发送消息。"
        case .unknownOperation(let id):
            return "验收构建里没有这个操作：\(id)"
        case .unknownCursor(let cursor):
            return "验收构建不认识这个游标：\(cursor)"
        case .noMirror:
            return "验收构建不读日历、也不上传镜像。"
        case .unknownAction(let id):
            return "验收构建里没有这个设备动作：\(id)"
        case .archiveUnreadable(let detail):
            return "验收归档存在于磁盘上，但这次打不开：\(detail)。"
                + "这不是「第一次启动」——重新生成会掩盖重启恢复的问题，"
                + "所以这里直接停下来。请点「重置」重写归档，然后重新走一遍清单。"
        }
    }
}

// MARK: - the archive

/// Where the seeded archive lives on disk.
///
/// A value rather than a hard-coded path so the tests own a temporary file and
/// the device owns one inside its own container. Two different apps therefore
/// never share an archive even if they share a bundle family, and deleting the
/// acceptance app removes everything it wrote.
public struct AcceptanceArchiveLocation: Sendable {
    public let fileURL: URL

    public init(fileURL: URL) {
        self.fileURL = fileURL
    }

    /// `Application Support/personal-agent-acceptance/archive.json` in this
    /// app's own container.
    public static func applicationSupport(
        fileManager: FileManager = .default
    ) throws -> AcceptanceArchiveLocation {
        let base = try fileManager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base.appendingPathComponent(
            "personal-agent-acceptance", isDirectory: true
        )
        try fileManager.createDirectory(
            at: directory, withIntermediateDirectories: true
        )
        return AcceptanceArchiveLocation(
            fileURL: directory.appendingPathComponent("archive.json")
        )
    }
}

/// The on-disk shape. Explicit rather than derived from the in-memory types:
/// `TimelineEvent` is deliberately not `Codable` (it is a wire value, and the
/// wire is JSON the server wrote), so what is persisted here is a decision about
/// this harness, and it should be readable as one.
struct StoredAcceptanceArchive: Codable {
    struct Event: Codable {
        var eventID: String
        var eventType: String
        var operationID: String?
        var createdAt: String
        var content: [String: JSONValue]
    }

    var conversationID: String
    var events: [Event]
    var projections: [String: JSONValue]

    init(_ seed: AcceptanceScenario.Seed) {
        conversationID = seed.conversationID
        events = seed.events.map {
            Event(
                eventID: $0.eventID,
                eventType: $0.eventType,
                operationID: $0.operationID,
                createdAt: $0.createdAt,
                content: $0.content
            )
        }
        projections = seed.projections
    }

    var seed: AcceptanceScenario.Seed {
        AcceptanceScenario.Seed(
            conversationID: conversationID,
            events: events.map {
                TimelineEvent(
                    eventID: $0.eventID,
                    eventType: $0.eventType,
                    operationID: $0.operationID,
                    createdAt: $0.createdAt,
                    content: $0.content
                )
            },
            projections: projections
        )
    }
}

// MARK: - the synthetic Timeline

/// A `ChatBackend` that serves the seeded archive and nothing else.
///
/// Persisted on first use and read back afterwards, which is what makes the
/// 「杀进程重启」 checklist item a real test rather than a tautology: the second
/// launch loads the same file through the same `loadLatest` path the ordinary
/// app uses, so a broken history load shows up as missing cards.
public actor AcceptanceTimeline: ChatBackend, MediaUploadBackend {
    private let location: AcceptanceArchiveLocation
    /// The page size for one read. Larger than the seeded archive on purpose:
    /// an acceptance run should not have to scroll to find the cards.
    private let pageSize: Int
    private var archive: AcceptanceScenario.Seed?

    public init(
        location: AcceptanceArchiveLocation,
        pageSize: Int = 50
    ) {
        self.location = location
        self.pageSize = pageSize
    }

    /// Delete whatever is on disk and write the seed again. The acceptance
    /// build's reset, and the only way the seeded cards ever change.
    public func resetToSeed() throws {
        let fresh = AcceptanceScenario.seed()
        try Self.write(fresh, to: location)
        archive = fresh
    }

    /// The archive, from memory, from disk, or seeded — and **never** re-seeded
    /// over something already there.
    ///
    /// The 2026-09-10 review found the two failure modes folded into one: a read
    /// error and a decode error both fell through to `AcceptanceScenario.seed()`
    /// and a rewrite. A damaged archive therefore came back as the same cards in
    /// the same order, which is exactly what the 「杀进程重启」 item asks a person
    /// to confirm — the item would have been ticked on a build that had not
    /// recovered anything.
    ///
    /// So the branches are now three, not two: absent → seed it; present and
    /// readable → use it; present and *not* readable → refuse, loudly, with the
    /// reason. The last one is the only one this build has no answer for, and
    /// saying so is the point of it.
    private func loaded() throws -> AcceptanceScenario.Seed {
        if let archive { return archive }
        let seed: AcceptanceScenario.Seed
        if FileManager.default.fileExists(atPath: location.fileURL.path) {
            seed = try Self.read(from: location)
        } else {
            seed = AcceptanceScenario.seed()
            try Self.write(seed, to: location)
        }
        archive = seed
        return seed
    }

    /// Read an archive that is known to exist. Both failures refuse: a file the
    /// process cannot open and a file it cannot parse are the same fact from
    /// here — this build does not have the archive it was supposed to recover.
    private static func read(
        from location: AcceptanceArchiveLocation
    ) throws -> AcceptanceScenario.Seed {
        let data: Data
        do {
            data = try Data(contentsOf: location.fileURL)
        } catch {
            throw AcceptanceHarnessError.archiveUnreadable(error.localizedDescription)
        }
        do {
            return try JSONDecoder()
                .decode(StoredAcceptanceArchive.self, from: data).seed
        } catch {
            throw AcceptanceHarnessError.archiveUnreadable(error.localizedDescription)
        }
    }

    private static func write(
        _ seed: AcceptanceScenario.Seed, to location: AcceptanceArchiveLocation
    ) throws {
        let data = try JSONEncoder().encode(StoredAcceptanceArchive(seed))
        try data.write(to: location.fileURL, options: .atomic)
    }

    private func persist(_ seed: AcceptanceScenario.Seed) throws {
        try Self.write(seed, to: location)
        archive = seed
    }

    // --- cursors -------------------------------------------------------------

    /// `accept:older:<index>` / `accept:newer:<index>`. The real server mints
    /// signed opaque cursors; this harness only has to be un-guessable by the
    /// client, and it is — `ChatTimeline` never parses one.
    private enum Cursor {
        static func encode(_ direction: TimelineDirection, _ index: Int) -> String {
            "accept:\(direction.rawValue):\(index)"
        }

        static func decode(
            _ raw: String, expecting direction: TimelineDirection
        ) -> Int? {
            let prefix = "accept:\(direction.rawValue):"
            guard raw.hasPrefix(prefix) else { return nil }
            return Int(raw.dropFirst(prefix.count))
        }
    }

    // This isolated harness must never gain an upload or media-read path.
    public func createMediaUpload(
        declaration: MediaUploadDeclaration, idempotencyKey: String
    ) async throws -> CreatedMediaUpload {
        throw AcceptanceHarnessError.noModel
    }

    public func putMediaContent(mediaID: String, body: Data) async throws -> MediaUploadReceipt {
        throw AcceptanceHarnessError.noModel
    }

    public func completeMediaUpload(mediaID: String) async throws -> CompletedMediaUpload {
        throw AcceptanceHarnessError.noModel
    }

    public func readMedia(mediaID: String) async throws -> Data {
        throw AcceptanceHarnessError.noModel
    }

    // --- ChatBackend ---------------------------------------------------------

    public func timelinePage(
        conversationID: String,
        cursor: String?,
        direction: TimelineDirection,
        limit: Int?
    ) async throws -> TimelinePageResponse {
        let seed = try loaded()
        let all = seed.events
        let size = max(1, limit ?? pageSize)

        // Which slice, if any, this request asks for. Both directions and both
        // cursor cases reduce to a half-open index range over the archive.
        let start: Int
        let end: Int
        switch direction {
        case .older:
            let upper: Int
            if let cursor {
                guard let anchor = Cursor.decode(cursor, expecting: .older) else {
                    throw AcceptanceHarnessError.unknownCursor(cursor)
                }
                upper = min(max(0, anchor), all.count)
            } else {
                upper = all.count
            }
            end = upper
            start = max(0, upper - size)
        case .newer:
            guard let cursor else {
                // The real server refuses `direction=newer` without one;
                // `ChatTimeline` never sends that, and answering with the whole
                // archive would be a silent widening of what it asked for.
                throw AcceptanceHarnessError.unknownCursor("")
            }
            guard let anchor = Cursor.decode(cursor, expecting: .newer) else {
                throw AcceptanceHarnessError.unknownCursor(cursor)
            }
            start = min(max(0, anchor), all.count)
            end = min(all.count, start + size)
        }

        let slice = all[start..<end]
        // An empty page carries no cursor and claims nothing about the
        // direction it was not asked about — the server's own rule, and the one
        // that keeps a client from following a cursor that anchors on nothing.
        guard !slice.isEmpty else {
            return TimelinePageResponse(
                conversationID: seed.conversationID,
                events: [],
                olderCursor: nil,
                newerCursor: nil,
                hasOlder: false,
                hasNewer: false
            )
        }
        return TimelinePageResponse(
            conversationID: seed.conversationID,
            events: Array(slice),
            // Minted only when there really is more that way, so the client
            // never pages into an empty page it was told to expect rows from.
            olderCursor: start > 0 ? Cursor.encode(.older, start) : nil,
            // Minted on **every** non-empty page, like the server's: the live
            // edge is where incremental sync starts, and a first load that
            // withheld it would leave the client unable to follow the Timeline
            // at all.
            newerCursor: Cursor.encode(.newer, end),
            hasOlder: start > 0,
            hasNewer: end < all.count
        )
    }

    public func operation(operationID: String) async throws -> OperationReceipt {
        let seed = try loaded()
        guard let receipt = AcceptanceScenario.receipt(operationID, in: seed) else {
            throw AcceptanceHarnessError.unknownOperation(operationID)
        }
        return receipt
    }

    public func operation(idempotencyKey: String) async throws -> OperationReceipt {
        throw AcceptanceHarnessError.unknownOperation(idempotencyKey)
    }

    public func cancelOperation(operationID: String) async throws -> OperationReceipt {
        throw AcceptanceHarnessError.noModel
    }

    public func sendChatMessage(
        conversationID: String,
        text: String,
        clarificationOf: String?,
        startNewSession: Bool,
        idempotencyKey: String
    ) async throws -> OperationReceipt {
        throw AcceptanceHarnessError.noModel
    }

    public func sendChatMessage(
        conversationID: String,
        parts: [ChatInputPart],
        clarificationOf: String?,
        startNewSession: Bool,
        idempotencyKey: String
    ) async throws -> OperationReceipt {
        // The isolated calendar harness has no upload or model path.
        throw AcceptanceHarnessError.noModel
    }

    public func decideDuplicate(
        checkID: String, decision: DuplicateDecision, idempotencyKey: String
    ) async throws -> OperationReceipt {
        throw AcceptanceHarnessError.noModel
    }

    public func updateExpenseCategory(
        recordID: String,
        category: String,
        expectedCurrentCategory: String?,
        idempotencyKey: String
    ) async throws -> OperationReceipt {
        throw AcceptanceHarnessError.noModel
    }

    public func uploadCalendarSync(
        windowStart: Date,
        windowEnd: Date,
        events: [CalendarMirrorEvent],
        calendars: [CalendarDirectoryEntry],
        windowComplete: Bool,
        snapshotAsOf: Date,
        syncEpoch: Int
    ) async throws -> CalendarSyncResponse {
        throw AcceptanceHarnessError.noMirror
    }

    /// Record what the person found, and append the marker the Timeline would
    /// have carried.
    ///
    /// The one write the harness performs, because 「人工核对两种结论」 is not
    /// accepted by looking at the buttons — the conclusion has to land and the
    /// history line has to appear. It appends through the same event shape the
    /// server writes, and it does **not** implement the server's rule that a
    /// contradicting re-resolution is refused: that rule is server policy, this
    /// build has no server, and pretending otherwise would make the checklist
    /// claim more than the harness can show.
    public func resolveManualReview(
        operationID: String, resolution: ManualResolution
    ) async throws -> ManualResolutionReceipt {
        var seed = try loaded()
        guard let existing = AcceptanceScenario.receipt(operationID, in: seed) else {
            throw AcceptanceHarnessError.unknownOperation(operationID)
        }
        // The domain the marker carries is the operation's own, read from the
        // projection — the same derivation the server makes. An operation with
        // no domain writes no `domain` key, which is the shape step 5's
        // predecessor produced and the one the neutral marker copy is for.
        var content: [String: JSONValue] = ["resolution": .string(resolution.rawValue)]
        if let domain = existing.domain, !domain.isEmpty {
            content["domain"] = .string(domain)
        }
        seed = AcceptanceScenario.Seed(
            conversationID: seed.conversationID,
            events: seed.events + [
                TimelineEvent(
                    eventID: "evt_accept_resolution_\(seed.events.count + 1)",
                    eventType: "manual_review_resolved",
                    operationID: operationID,
                    createdAt: AcceptanceScenario.seededAt,
                    content: content
                )
            ],
            projections: seed.projections
        )
        try persist(seed)

        let body: JSONValue = .object([
            "operation_id": .string(operationID),
            "state": .string(existing.state.wire),
            "manual_resolution": .string(resolution.rawValue),
            "recorded": .bool(true),
            "manual_resolved_at": .string(AcceptanceScenario.seededAt),
        ])
        guard case .object(let object) = body,
              let data = try? JSONEncoder().encode(object),
              let receipt = try? JSONDecoder().decode(
                  ManualResolutionReceipt.self, from: data
              )
        else { throw AcceptanceHarnessError.unknownOperation(operationID) }
        return receipt
    }

    /// Settle a device action this build's executor ran.
    ///
    /// The seeded archive hands out no actions, so this is reachable only if a
    /// future fixture delivers one; it answers with the operation's own current
    /// projection rather than inventing a settled one, which keeps the harness
    /// from claiming a write it did not make.
    public func reportDeviceActionResult(
        actionID: String, body: DeviceActionResultBody
    ) async throws -> OperationReceipt {
        let seed = try loaded()
        guard let operationID = seed.projections.first(where: { _, value in
            guard case .object(let object) = value,
                  case .string(let recorded)? = object["device_action_id"]
            else { return false }
            return recorded == actionID
        })?.key else {
            throw AcceptanceHarnessError.unknownAction(actionID)
        }
        return try await operation(operationID: operationID)
    }

    /// 「仍要创建」: the round's own button, answered end to end.
    ///
    /// A *harness* answer, and it is written down as one. The real server derives
    /// a second operation under a key published for exactly this purpose and has
    /// the device execute it; here a settled `succeeded` projection is appended
    /// directly, so the button's whole visible path — tap, marker, new card —
    /// can be checked without a server, a model, or a second real event. What
    /// this does **not** show is the server's idempotency of the override, which
    /// is `test_calendar_override.py`'s subject and not this build's.
    public func overrideDeviceAction(actionID: String) async throws -> OperationReceipt {
        var seed = try loaded()
        guard let source = seed.projections.first(where: { _, value in
            guard case .object(let object) = value,
                  case .string(let recorded)? = object["device_action_id"]
            else { return false }
            return recorded == actionID
        })?.key else {
            throw AcceptanceHarnessError.unknownAction(actionID)
        }
        let derived = "op-accept-override-\(source)"
        if seed.projections[derived] == nil {
            let operation = AcceptanceScenario.Operation(
                operationID: derived,
                tool: AcceptanceScenario.deviceTool,
                state: "succeeded",
                domain: OperationReceipt.calendarDomain,
                recordID: "EKA-ACCEPT-0002-OVERRIDE",
                deviceResult: "created",
                deviceActionID: "act-accept-override-\(actionID)"
            )
            var updated = seed.projections
            updated[derived] = AcceptanceScenario.projection(operation)
            seed = AcceptanceScenario.Seed(
                conversationID: seed.conversationID,
                events: seed.events + [
                    TimelineEvent(
                        eventID: "evt_accept_override_\(seed.events.count + 1)",
                        eventType: "operation_result",
                        operationID: derived,
                        createdAt: AcceptanceScenario.seededAt,
                        content: AcceptanceScenario.eventContent(operation)
                    )
                ],
                projections: updated
            )
            try persist(seed)
        }
        return try await operation(operationID: derived)
    }
}

// MARK: - the synthetic executor

/// The device-action executor the acceptance build composes.
///
/// It returns fixed receipts and **never touches EventKit**, which is the whole
/// point: the acceptance build must be unable to write to a real calendar even
/// by accident. It is belt-and-braces — the seeded archive hands out no actions
/// — but a future fixture that did would otherwise reach the production
/// executor through the same composition seam, and that is the accident this
/// type exists to make impossible.
///
/// `failedReport` is the one shape the protocol asks an executor to own: an
/// action this build decoded but cannot run. Answering `failed` is honest here
/// and is the same answer the production executor gives for a build with no
/// executor at all.
public struct AcceptanceDeviceExecutor: DeviceActionExecuting {
    private let receipts: [String: OperationReceipt]

    public init(receipts: [String: OperationReceipt] = [:]) {
        self.receipts = receipts
    }

    public func executeAndReport(_ action: DeviceEventAction) async -> OperationReceipt? {
        // No EventKit, no report. `nil` is the protocol's lost-report case; for
        // this build the truer description is that there is nothing to report
        // to, and the caller already holds the parked receipt it will degrade
        // to.
        receipts[action.actionID]
    }

    public func failedReport(detail: String) -> DeviceActionResultBody {
        .failed(detail: detail)
    }
}
