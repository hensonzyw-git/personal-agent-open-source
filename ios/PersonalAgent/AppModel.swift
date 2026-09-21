import EventKit
import Foundation
import PersonalAgentKit
import Observation

/// The view state. All the interesting logic lives in `PersonalAgentKit`, which is
/// tested headlessly; this type only turns results and errors into something a
/// screen can render.
@MainActor
@Observable
final class AppModel {
    enum Phase: Equatable {
        case loading
        case needsEnrollment
        case ready
        /// The server will not issue tokens for this device any more.
        case revoked
    }

    var phase: Phase = .loading
    var pendingDevelopmentEventID: String?
    var pendingDevelopmentNotificationID: String?
    var developmentNotificationItems: [DevelopmentNotificationItem] = []
    var developmentNotificationError = false
    var developmentNavigationID = UUID()
    /// Where the backend is. The default is the production entry behind TLS
    /// (`DEV-033`); the Simulator's `http://127.0.0.1:8810` is typed by hand
    /// for local runs. A fresh install must default to the real server: a
    /// default of loopback made every real-device enrollment hit the phone's
    /// own lo0 with -1004 and no server-side trace (2026-07-31).
    var baseURLText: String = "https://agent.example.invalid"
    var enrollmentCode: String = ""
    var deviceName: String = defaultDeviceName()

    var deviceID: String?
    var keyKind: String?
    var capabilities: Capabilities?
    var selfDevice: DeviceSummary?
    var lastError: String?
    var busy = false
    /// `DEV-030`. Built once a session exists, and opened on the Timeline the
    /// server names in `/v1/capabilities` — never on an id chosen here.
    var chat: ChatModel?
    /// `DEV-031`. The daily-review surface. Like the chat it is built once per
    /// session, and its ledger jump URL is re-validated from every fresh
    /// `/v1/capabilities` read rather than remembered.
    var review: ReviewModel?

    private let store: CredentialStore = KeychainCredentialStore()
    private var session: DeviceSession?
    var developmentAuthorizationSession: DeviceSession? { session }
    private var chatTimeline: ChatTimeline?
    private var reviewCenter: ReviewCenter?
    /// The calendar mirror's production driver (review R5). Built with the
    /// session it uploads through; `nil` until a session exists.
    private var mirrorSyncEngine: CalendarMirrorSyncEngine?
    /// Design §9.1's `EKEventStoreChanged` observer. Held so it can be removed
    /// when the device is revoked — a revoked device must stop reading the
    /// calendar, not merely stop uploading it.
    private var calendarChangeObserver: NSObjectProtocol?
    /// `DEV-040`. Built once a session exists; the app delegate forwards iOS's
    /// remote-notification callbacks into it. Nil until enrollment, because a
    /// token cannot be uploaded without a device id and access token.
    private var pushCoordinator: PushCoordinator?

    // --- lifecycle -----------------------------------------------------------

    func start() async {
        if let url = try? DeviceSession.storedBaseURL(in: store) {
            baseURLText = url.absoluteString
        }
        guard let session = makeSession() else {
            phase = .needsEnrollment
            return
        }
        do {
            let state = try await session.restore()
            switch state {
            case .notEnrolled:
                phase = .needsEnrollment
            case .enrolled(let id), .rejected(let id):
                deviceID = id
                keyKind = await session.deviceKeyKind?.rawValue
                phase = .ready
                await refresh()
            }
        } catch {
            // A key blob that cannot be reopened is not something to paper over:
            // the device has to enroll again, and it should say so.
            lastError = describe(error)
            phase = .needsEnrollment
        }
    }

    func enroll() async {
        lastError = nil
        guard let session = makeSession() else {
            lastError = "服务地址无效，请使用 http:// 或 https://"
            return
        }
        let code = enrollmentCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty else {
            lastError = "请输入服务器生成的一次性注册码"
            return
        }
        busy = true
        defer { busy = false }
        do {
            let enrolled = try await session.enroll(code: code, displayName: deviceName)
            deviceID = enrolled.deviceID
            keyKind = await session.deviceKeyKind?.rawValue
            enrollmentCode = ""
            phase = .ready
            await refresh()
        } catch {
            lastError = describe(error)
        }
    }

    func openDevelopmentNotification() async {
        guard let id = pendingDevelopmentNotificationID, let session else { return }
        do {
            let context = try await session.developmentNotification(id: id)
            guard pendingDevelopmentNotificationID == id else { return }
            pendingDevelopmentNotificationID = nil
            if context.items.count == 1 { pendingDevelopmentEventID = context.items[0].eventID }
            else { developmentNotificationItems = context.items }
        } catch { developmentNotificationError = true }
    }

    func developmentRoles(workflowID: String? = nil) async throws -> DevelopmentRoles {
        guard let session else { throw AgentClientError.unauthenticated }
        return try await session.developmentRoles(workflowID: workflowID)
    }
    func developmentTask(id: String) async throws -> DevelopmentTaskDetail {
        guard let session else { throw AgentClientError.unauthenticated }
        return try await session.developmentTask(id: id)
    }

    func developmentTasks(filter: String, cursor: String?) async throws -> DevelopmentTaskPage {
        guard let session else { throw AgentClientError.unauthenticated }
        return try await session.developmentTasks(filter: filter, cursor: cursor)
    }

    func developmentDocument(id: String) async throws -> DevelopmentDocument {
        guard let session else { throw AgentClientError.unauthenticated }
        return try await session.developmentDocument(id: id)
    }

    func refresh() async {
        guard let session else { return }
        busy = true
        defer { busy = false }
        do {
            let read = try await session.capabilities()
            capabilities = read
            selfDevice = try await session.devices().devices.first { $0.isSelf }
            phase = .ready
            lastError = nil
            await openChat(session: session, conversationID: read.conversationID)
            // The foreground trigger for the mirror (review R5): every path
            // that brings the app to usable state runs `refresh()`, so this
            // is the one seam that covers launch, pull-to-refresh, and
            // return-to-foreground alike. Fire-and-degrade: a mirror that
            // cannot sync must not block the surfaces this method opens —
            // the server answers calendar queries with `mirror_stale` until
            // the next attempt, which is honest, versus a launch error card
            // that is not.
            await syncCalendarMirror(session: session)
            // §1c: the empty state's 能力清单 is the server's answer, not a list
            // compiled here. Pushed on every refresh rather than at construction so
            // a tool granted or withdrawn server-side appears without a reinstall.
            chat?.tools = read.tools
            chat?.ledgerURL = read.validatedLedgerURL
            chat?.imageCapability = read.images
            await openReview(session: session, capabilities: read)
        } catch AgentClientError.deviceRejected {
            phase = .revoked
            // The last successful read is now unverifiable, and leaving it on
            // screen would show `active` directly under "已被撤销". A stale value
            // presented as current is worse than no value.
            capabilities = nil
            selfDevice = nil
            // The chat surface goes with it: a Timeline this device can no longer
            // read must not stay on screen looking current.
            chat = nil
            chatTimeline = nil
            review = nil
            reviewCenter = nil
            forgetMirrorEngine()
            lastError = "服务端已不再为本设备签发 token（设备被撤销或密钥不匹配）。"
        } catch {
            lastError = describe(error)
        }
    }

    func revokeSelf() async {
        guard let session else { return }
        busy = true
        defer { busy = false }
        do {
            selfDevice = try await session.revokeSelf()
            phase = .revoked
            chat = nil
            chatTimeline = nil
            review = nil
            reviewCenter = nil
            forgetMirrorEngine()
            // A revoked device's stored token is dead weight; forgetting it
            // here means a re-enrollment (new device id) re-registers rather
            // than assuming this token already belongs to the new row.
            PushCoordinator.forgetConfirmedToken()
            lastError = nil
        } catch {
            lastError = describe(error)
        }
    }

    /// Forget the local enrollment. The server-side revocation is separate on
    /// purpose: deleting the key here does not revoke anything, and pretending it
    /// did would leave an active device row nobody can see.
    func forgetLocally() async {
        guard let session else { return }
        do {
            try await session.forgetLocally()
            deviceID = nil
            keyKind = nil
            capabilities = nil
            selfDevice = nil
            chat = nil
            chatTimeline = nil
            review = nil
            reviewCenter = nil
            forgetMirrorEngine()
            PushCoordinator.forgetConfirmedToken()
            // The previous phase's error described a device that no longer exists
            // here; carrying it onto the enrollment screen would report a failure
            // for a device that was just forgotten.
            lastError = nil
            phase = .needsEnrollment
        } catch {
            lastError = describe(error)
        }
    }

    // --- push (`DEV-040`) ----------------------------------------------------

    /// Hand the coordinator to the app delegate so iOS's callbacks reach it.
    /// Called once at launch, before `start`. The coordinator is built lazily
    /// in `registerPushIfPermitted`, so this only stores the reference the
    /// delegate will read when iOS fires `didRegisterForRemoteNotifications`.
    func bindPushCoordinator(into delegate: AppDelegate) {
        if pushCoordinator == nil {
            pushCoordinator = PushCoordinator { [weak self] token in
                guard let self, let session = self.session else {
                    throw DeviceSessionError.notEnrolled
                }
                try await session.setPushToken(token)
            }
        }
        delegate.pushCoordinator = pushCoordinator
        delegate.developmentBatchNotification = { [weak self] id in
            self?.pendingDevelopmentNotificationID = id
            self?.developmentNavigationID = UUID()
        }
        if let id = delegate.pendingDevelopmentNotificationID {
            pendingDevelopmentNotificationID = id
            delegate.pendingDevelopmentNotificationID = nil
        }
        delegate.developmentNotification = { [weak self] id in
            self?.pendingDevelopmentEventID = id
            self?.developmentNavigationID = UUID()
        }
        if let id = delegate.pendingDevelopmentEventID {
            pendingDevelopmentEventID = id
            delegate.pendingDevelopmentEventID = nil
        }
        // The foreground mirror trigger (review R5): iOS's `didBecomeActive`
        // is the one signal SwiftUI does not observe, and a foreground return
        // is when the calendar may have changed under us. It degrades through
        // the same `syncCalendarMirror` the refresh path uses.
        delegate.foregroundMirrorSync = { [weak self] in
            guard let self, let session = self.session, self.phase == .ready else {
                return
            }
            Task { @MainActor in
                await self.syncCalendarMirror(session: session)
            }
        }
    }

    /// Ask the user, ask iOS, and upload — or return quietly. Failing to
    /// register a push token must never block a launch or hide chat behind an
    /// error; the review card is in the app either way. The simulator has no
    /// APNs token to give, so this is a no-op there rather than an error.
    func registerPushIfPermitted() async {
        #if targetEnvironment(simulator)
        // The simulator entitlement deliberately omits aps-environment, so
        // registerForRemoteNotifications would only fail. Treat it as a place
        // push does not run, not as a launch error.
        return
        #else
        guard let pushCoordinator else { return }
        do {
            try await pushCoordinator.registerIfPermitted()
        } catch {
            // Swallowed on purpose: a declined prompt, a malformed token, or an
            // unreachable server are all non-fatal. The next launch retries.
        }
        #endif
    }

    // --- calendar mirror (`review R5`) ---------------------------------------

    /// Run the mirror sync if its durable marker says it is stale. The
    /// production caller for `CalendarMirrorSyncEngine`: the engine composes
    /// `EventKitCalendarStore.snapshot` with `DeviceSession.uploadCalendarSync`
    /// through `CalendarSyncUploader`'s batching — before this call existed,
    /// those three were wired to nothing and the mirror stayed empty forever.
    ///
    /// Every failure degrades: the engine throws only what the snapshot or an
    /// upload threw, and this method turns it into a `lastError` only if the
    /// screen is not already showing something more important. The marker
    /// moves only when a whole window completed, so a failing sync retries on
    /// the next `refresh()` — there is no separate repair path to forget.
    private func syncCalendarMirror(
        session: DeviceSession, reason: SyncReason = .gated
    ) async {
        guard let engine = mirrorEngine(session: session) else { return }
        do {
            _ = try await engine.sync(reason: reason)
        } catch {
            degradeMirror(error)
        }
    }

    /// The engine, built on first use.
    ///
    /// The same EventKit store the device-action executor uses: one permission
    /// prompt, one calendar access, two consumers. Every entry point goes
    /// through here so that the `EKEventStoreChanged` observer and the
    /// device-write trigger cannot outrun the engine's construction.
    @discardableResult
    private func mirrorEngine(session: DeviceSession) -> CalendarMirrorSyncEngine? {
        if mirrorSyncEngine == nil {
            mirrorSyncEngine = CalendarMirrorSyncEngine(
                store: EventKitCalendarStore(), backend: session, storage: store
            )
            observeCalendarChanges(session: session)
        }
        return mirrorSyncEngine
    }

    /// Drop the engine *and* its observer together. A revoked device must stop
    /// reading the calendar, not merely stop uploading it, and the two are one
    /// object's lifetime.
    private func forgetMirrorEngine() {
        if let calendarChangeObserver {
            NotificationCenter.default.removeObserver(calendarChangeObserver)
        }
        calendarChangeObserver = nil
        mirrorSyncEngine = nil
    }

    /// Design §9.1's first `.forced` trigger: the calendar changed under this
    /// app — the user wrote 【飞行计划】 elsewhere, or this app's own executor
    /// did. Observed without naming a store object on purpose:
    /// `EventKitCalendarStore` holds a private `EKEventStore`, and an observer
    /// pinned to one instance would miss the other one's changes.
    ///
    /// `.forced` and not `.gated`: the change has already happened, so the
    /// staleness gate's answer ("the marker is seconds old, nothing to do")
    /// would leave the mirror missing the very event that was just written.
    private func observeCalendarChanges(session: DeviceSession) {
        guard calendarChangeObserver == nil else { return }
        calendarChangeObserver = NotificationCenter.default.addObserver(
            forName: .EKEventStoreChanged, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                await self?.noteCalendarChanged(session: session)
            }
        }
    }

    /// The `@Sendable` calendar-write sink's entry point (`ChatTimeline` calls
    /// it from its own actor, so the hop back into this one is explicit).
    ///
    /// **Arms, never waits.** The sink runs between a write landing and its
    /// receipt being returned, so awaiting the upload here made the user's
    /// receipt wait on a whole-window mirror sync it had nothing to do with.
    /// What §9.1 needs at that moment is that the change is recorded and a pass
    /// is behind it — both of which the arm does before it returns.
    ///
    /// The trade this makes, stated rather than hidden: an upload that fails
    /// inside the armed pass no longer reaches `degradeMirror`, because there is
    /// no longer a caller waiting to be told. The failure is still not silent —
    /// the pass drops the mirror marker and keeps the change sequence dirty, so
    /// `knownUnsynced` turns the query card's 「本地日历有未同步的变更」 on and
    /// the next refresh retries through the `.gated` path, which does degrade.
    private func calendarDidChange() async {
        guard let session else { return }
        guard let engine = mirrorEngine(session: session) else { return }
        do {
            try await engine.armCalendarChanged()
        } catch {
            degradeMirror(error)
        }
    }

    /// Record the change, then run the pass that covers it (design §9.1). The
    /// engine bumps its sequence **before** it syncs, so a pass that fails
    /// leaves the query warning up rather than letting a lost upload read as a
    /// clean mirror.
    private func noteCalendarChanged(session: DeviceSession) async {
        guard let engine = mirrorEngine(session: session) else { return }
        do {
            _ = try await engine.noteCalendarChanged()
        } catch {
            degradeMirror(error)
        }
    }

    /// §9.1's budget-exhaustion note, fired by the handle's losing side of the
    /// race rather than asked for afterwards.
    ///
    /// This used to run *inside* the sync task, after `await syncCalendarMirror`
    /// returned — where the engine's `inFlight` flag was already down, so the
    /// engine's own guard made it a no-op on every path. It looked like a
    /// belt-and-braces check on both sides of a race and was in fact
    /// unreachable. The budget expiring is a fact only the waiter has, at the
    /// moment it gives up; it is passed in from there.
    ///
    /// A storage failure is swallowed on purpose: the user is mid-send, and the
    /// consequence of losing the note is a missing warning line, never a wrong
    /// one — the pass itself is unaffected and the next trigger re-records the
    /// state.
    private func noteMirrorBudgetIfStillRunning() async {
        guard let engine = mirrorSyncEngine else { return }
        try? await engine.noteSyncBudgetExhausted()
    }

    /// Degrade, never block: the chat and review surfaces must open whether or
    /// not the mirror synced. The failure is surfaced without displacing a
    /// primary error the user is mid-way through reading.
    private func degradeMirror(_ error: Error) {
        if lastError == nil {
            lastError = "日历镜像同步未完成，查询结果可能不是最新（\(describe(error))）"
        }
    }

    // --- helpers -------------------------------------------------------------

    /// Open the chat surface on the server's canonical Timeline.
    ///
    /// The `ChatTimeline` is created once per session and reused, because it owns
    /// the unresolved-message slot; rebuilding it on every refresh would drop the
    /// loaded history and re-run the resume path.
    private func openChat(session: DeviceSession, conversationID: String) async {
        if chatTimeline == nil {
            // The device-action executor is what turns a handed
            // `calendar.create_event` into a real EventKit write and the
            // report that settles its operation. Composed here once, next to
            // the Timeline it serves; the write scope stays v1 create-only.
            chatTimeline = ChatTimeline(
                backend: session,
                store: store,
                deviceActionExecutor: DeviceEventActionExecutor(
                    store: EventKitCalendarStore(), backend: session
                )
            )
        }
        guard let chatTimeline else { return }
        if chat == nil {
            chat = ChatModel(
                timeline: chatTimeline,
                mediaBackend: session,
                store: store,
                describe: { [weak self] error in
                    self?.describe(error) ?? String(describing: error)
                }
            )
            chat?.developmentAuthorizationSession = session
            chat?.loadDevelopmentContext = { id in try await session.developmentReplyContext(eventID: id) }
            chat?.loadDevelopmentDocument = { id in try await session.developmentDocument(id: id) }
            // The pre-send mirror top-up (review R5): the same engine the
            // refresh path uses, so the staleness gate lives in one place.
            // The handle bounds what the send path waits (F8): the sync runs
            // on its own task and the send waits at most its budget.
            chat?.onSyncMirror = { [weak self] in
                guard let self, let session = self.session else {
                    return MirrorSyncHandle.done()
                }
                return MirrorSyncHandle(
                    run: { await self.syncCalendarMirror(session: session) },
                    // Design §9.1's fourth trigger. The handle reports which
                    // side of the wait won; only the budget's side means the
                    // trigger happened. Asking the engine afterwards cannot
                    // answer it — by then the pass may have finished, and the
                    // engine's `inFlight` guard would call a real exhaustion a
                    // no-op.
                    onBudgetExpired: { [weak self] in
                        await self?.noteMirrorBudgetIfStillRunning()
                    }
                )
            }
            // Design §9.1's second `.forced` trigger, installed next to the
            // Timeline that raises it: a device-action report that landed means
            // the calendar was written, and the query the user makes next must
            // read the mirror that contains it.
            await chatTimeline.setCalendarWriteSink { [weak self] in
                // The sink is called from the Timeline's actor; this is where
                // it re-enters the app's.
                await self?.calendarDidChange()
            }
            // §9.1's query gate: the query card states 「本地日历有未同步的
            // 变更」 from the device's own sequence comparison, never from the
            // server's `mirror_stale` — the two are different facts about
            // different sources and the server is never told this one. A
            // build with no engine yet reports clean, which is what a device
            // that has never uploaded has to say.
            chat?.onReadCalendarUnsynced = { [weak self] in
                guard let engine = self?.mirrorSyncEngine else { return false }
                return (try? await engine.knownUnsynced) ?? false
            }
            await chat?.open(conversationID: conversationID)
        } else if await chatTimeline.boundConversationID != conversationID {
            // The server named a different Timeline. Adopting it is the client's
            // only correct move; arguing with it is not an option it has.
            await chat?.open(conversationID: conversationID)
        }
    }

    /// Open the review surface (`DEV-031`).
    ///
    /// The list is (re)loaded on every refresh: it is a cheap local read, and a
    /// card reopened server-side by a late-verified write must not keep showing
    /// 已复核 because nobody asked again.
    private func openReview(session: DeviceSession, capabilities: Capabilities) async {
        if reviewCenter == nil {
            reviewCenter = ReviewCenter(backend: session)
        }
        guard let reviewCenter else { return }
        if review == nil {
            review = ReviewModel(
                center: reviewCenter,
                ledgerURL: nil,
                describe: { [weak self] error in
                    self?.describe(error) ?? String(describing: error)
                }
            )
        }
        review?.updateLedgerURL(from: capabilities)
        await review?.load()
    }

    private func makeSession() -> DeviceSession? {
        let text = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: text), let client = try? AgentClient(baseURL: url) else {
            return nil
        }
        if let session, client.baseURL == url { return session }
        let built = DeviceSession(client: client, store: store)
        session = built
        return built
    }

    private func describe(_ error: Error) -> String {
        switch error {
        case AgentClientError.enrollmentRejected:
            return "注册码无效、已使用或已过期，请在服务器重新生成。"
        case AgentClientError.deviceRejected:
            return "服务端拒绝为本设备签发 token。"
        case AgentClientError.unauthenticated:
            return "token 未被接受。"
        case AgentClientError.badRequest(let code):
            return "请求被拒绝（\(code ?? "INVALID_ARGUMENT")）。"
        case AgentClientError.forbidden(let code):
            return "权限不足（\(code ?? "SCOPE_DENIED")）。"
        case AgentClientError.serverError(let status):
            return "服务端错误 HTTP \(status)。"
        case AgentClientError.transport(let detail):
            return "无法连接服务：\(detail)"
        case AgentClientError.malformedResponse:
            return "服务返回的内容不符合约定，已拒绝当作成功处理。"
        case AgentClientError.invalidBaseURL:
            return "服务地址无效。"
        case DeviceIdentityError.secureEnclaveUnavailable:
            return "本设备无法创建 Secure Enclave 密钥。"
        case DeviceSessionError.notEnrolled:
            return "本设备尚未注册。"
        case DeviceSessionError.challengeNotForThisDevice,
             DeviceSessionError.unexpectedAudience,
             DeviceSessionError.malformedNonce:
            return "challenge 与本设备不匹配，已拒绝签名。"
        case DeviceSessionError.storedEnrollmentMalformed:
            return "本机设备凭证损坏，无法安全恢复；请先在服务器撤销旧设备，再重新注册。"
        case ChatTimeline.ChatError.timelineUnknown:
            return "还没拿到服务端的 Timeline，请先刷新状态。"
        case ChatTimeline.ChatError.unresolvedSend:
            return "上一条消息还没有确认结果，先处理它再发新的，否则同一笔可能被记两次。"
        case ChatTimeline.ChatError.pendingSendMalformed:
            return "本机保存的未完成消息已损坏，无法安全恢复；请记下界面上的 operation_id，在服务端确认后再丢弃。"
        case ChatTimeline.ChatError.lockedDuplicateDecision(let pending):
            let wording = pending.decision == .dismiss ? "忽略" : "仍然记录"
            return "该疑似重复已提交「\(wording)」但结果未确认，请先重试同一决策或丢弃它；直接改选会被服务端拒绝。"
        case ChatTimeline.ChatError.pendingDecisionsMalformed:
            return "本机保存的未确认决策已损坏，无法安全恢复；请记下界面上的 duplicate_check_id，在服务端确认后再丢弃。"
        case ReviewCenterError.mutationInProgress:
            return "这张复核卡正在更新，请等待服务端回复。"
        case ReviewCenterError.unrecognisedStatus(let status):
            return "服务端返回了本客户端不认识的复核状态（\(status)）；已按只读处理，请升级后再操作。"
        case DeviceSessionError.localPersistenceFailed(let deviceID, let revoked):
            if revoked {
                return "本机凭证保存失败；刚创建的服务端设备已自动撤销，请重新生成注册码。"
            }
            return "本机凭证保存失败，且未能自动撤销服务端设备 \(deviceID)。请先在服务器执行 revoke。"
        default:
            return String(describing: error)
        }
    }

    private static func defaultDeviceName() -> String {
        #if canImport(UIKit)
        return "iPhone"
        #else
        return "device"
        #endif
    }
}
