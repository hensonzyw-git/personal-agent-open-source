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
    private var chatTimeline: ChatTimeline?
    private var reviewCenter: ReviewCenter?
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
            // §1c: the empty state's 能力清单 is the server's answer, not a list
            // compiled here. Pushed on every refresh rather than at construction so
            // a tool granted or withdrawn server-side appears without a reinstall.
            chat?.tools = read.tools
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

    // --- helpers -------------------------------------------------------------

    /// Open the chat surface on the server's canonical Timeline.
    ///
    /// The `ChatTimeline` is created once per session and reused, because it owns
    /// the unresolved-message slot; rebuilding it on every refresh would drop the
    /// loaded history and re-run the resume path.
    private func openChat(session: DeviceSession, conversationID: String) async {
        if chatTimeline == nil {
            chatTimeline = ChatTimeline(backend: session, store: store)
        }
        guard let chatTimeline else { return }
        if chat == nil {
            chat = ChatModel(
                timeline: chatTimeline,
                describe: { [weak self] error in
                    self?.describe(error) ?? String(describing: error)
                }
            )
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
