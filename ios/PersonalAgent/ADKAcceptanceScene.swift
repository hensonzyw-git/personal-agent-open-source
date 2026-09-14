#if ADK_ACCEPTANCE
import SwiftUI
import CryptoKit
import Security
import PersonalAgentKit

/// No production AppModel, delegate, Keychain store or device-action executor.
private final class AcceptanceTLS: NSObject, URLSessionDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    let origin: URL
    let fingerprint: String
    init(origin: URL, fingerprint: String) { self.origin = origin; self.fingerprint = fingerprint }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == origin.host,
              challenge.protectionSpace.port == origin.port,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let cert = chain.first,
              SHA256.hash(data: SecCertificateCopyData(cert) as Data).map({ String(format: "%02x", $0) }).joined() == fingerprint
        else { NSLog("ADK acceptance TLS endpoint or fingerprint rejected"); completionHandler(.cancelAuthenticationChallenge, nil); return }
        SecTrustSetAnchorCertificates(trust, [cert] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var trustError: CFError?
        guard SecTrustEvaluateWithError(trust, &trustError) else { NSLog("ADK acceptance TLS trust: %@", String(describing: trustError)); completionHandler(.cancelAuthenticationChallenge, nil); return }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

private struct NoAcceptanceMedia: MediaUploadBackend {
    func createMediaUpload(declaration: MediaUploadDeclaration, idempotencyKey: String) async throws -> CreatedMediaUpload { throw AgentClientError.malformedResponse }
    func putMediaContent(mediaID: String, body: Data) async throws -> MediaUploadReceipt { throw AgentClientError.malformedResponse }
    func completeMediaUpload(mediaID: String) async throws -> CompletedMediaUpload { throw AgentClientError.malformedResponse }
}

@MainActor @Observable
private final class AcceptanceModel {
    var chat: ChatModel?
    var code = ""
    var error: String?
    var deviceID: String?
    var busy = false
    private var store: AcceptanceFileStore?
    private var session: DeviceSession?
    private var network: URLSession?
    private var work: Task<Void, Never>?

    func start(enroll: Bool = false) {
        guard !busy else { return }
        busy = true
        work = Task { [weak self] in
            guard let self else { return }
            defer { busy = false }
            do {
                guard let raw = Bundle.main.object(forInfoDictionaryKey: "ADKAcceptanceURL") as? String,
                      let url = URL(string: raw), url.scheme == "https", url.host != nil, url.port != nil,
                      url.user == nil, url.password == nil, url.path.isEmpty,
                      let pin = Bundle.main.object(forInfoDictionaryKey: "ADKAcceptanceCertificateSHA256") as? String,
                      pin.count == 64, pin.allSatisfy({ $0.isHexDigit }) else {
                    throw AgentClientError.invalidBaseURL
                }
                if session == nil {
                    let fileStore = try AcceptanceFileStore()
                    if let stored = try DeviceSession.storedBaseURL(in: fileStore), stored != url { throw AgentClientError.invalidBaseURL }
                    let config = URLSessionConfiguration.ephemeral
                    config.urlCache = nil; config.httpShouldSetCookies = false; config.httpCookieAcceptPolicy = .never
                    config.timeoutIntervalForRequest = 35
                    let network = URLSession(configuration: config, delegate: AcceptanceTLS(origin: url, fingerprint: pin.lowercased()), delegateQueue: nil)
                    self.network = network; store = fileStore
                    session = DeviceSession(client: try AgentClient(baseURL: url, session: network), store: fileStore)
                }
                guard let session, let store else { return }
                if enroll {
                    _ = try await session.enroll(code: code.trimmingCharacters(in: .whitespacesAndNewlines), displayName: "ADK 合成验收")
                    code = ""
                }
                let state = try await session.restore()
                guard case .enrolled(let id) = state else { return }
                deviceID = id
                let capabilities = try await session.capabilities()
                let timeline = ChatTimeline(backend: session, store: store)
                let chat = ChatModel(timeline: timeline, mediaBackend: NoAcceptanceMedia(), store: store,
                                     describe: { String(describing: $0) })
                self.chat = chat
                await chat.open(conversationID: capabilities.conversationID)
                error = nil
            } catch { self.error = String(describing: error) }
        }
    }
    func clear() async {
        let previous = work
        busy = true
        network?.invalidateAndCancel()
        previous?.cancel()
        await previous?.value
        busy = true
        let oldChat = chat
        chat = nil // Remove UI before draining; no new work can begin.
        await oldChat?.stopForAcceptanceReset()
        session = nil; network = nil; work = nil
        do {
            // Also permits recovery from a malformed envelope at startup.
            let fileStore = try store ?? AcceptanceFileStore()
            try fileStore.closeAndClear()
            store = nil; deviceID = nil; code = ""; error = nil
        } catch { self.error = String(describing: error) }
        busy = false
    }
}

struct ADKAcceptanceScene: View {
    @State private var model = AcceptanceModel()
    @State private var confirmClear = false
    var body: some View {
        NavigationStack {
            VStack(spacing: 8) {
                Text("合成数据验收 · 固定模型响应").font(.caption).foregroundStyle(.orange)
                if let error = model.error { Text(error).font(.caption).textSelection(.enabled) }
                if let chat = model.chat {
                    Menu("选择验收输入") {
                        ForEach(["你好", "澄清", "查询", "比较", "只读失败", "空响应", "畸形参数", "夹带正文", "提供方错误", "越权写入"], id: \.self) { text in
                            Button(text) { chat.draft = text }
                        }
                    }.disabled(model.busy)
                    ChatView(model: chat).disabled(model.busy)
                } else {
                    TextField("本机一次性注册码", text: $model.code).textFieldStyle(.roundedBorder).autocorrectionDisabled()
                    Button("注册验收设备") { model.start(enroll: true) }.disabled(model.busy || model.code.isEmpty)
                    Button("恢复验收会话") { model.start() }.disabled(model.busy)
                    Spacer()
                }
            }.padding(.horizontal, 8)
            .navigationTitle("ADK 验收")
            .toolbar { Button("清空验收数据") { confirmClear = true }.disabled(model.busy) }
            .confirmationDialog("仅清空本 App 的本地身份和待恢复请求；服务器历史和操作保留。再次发送将是新请求。", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("清空本地验收数据", role: .destructive) { Task { await model.clear() } }
            }
            .task { model.start() }
        }
    }
}
#endif
