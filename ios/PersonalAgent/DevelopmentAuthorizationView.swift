import SwiftUI
import PersonalAgentKit

/// A separate device operation: never writes into the chat draft or reply context.
struct DevelopmentAuthorizationView: View {
    let session: DeviceSession
    let requestID: String
    @Environment(\.dismiss) private var dismiss
    @State private var snapshot: DevelopmentAuthorizationSnapshot?
    @State private var projects: [DevelopmentAuthorizationProject] = []
    @State private var selected = ""
    @State private var actions: Set<String> = ["read"]
    @State private var seconds = 600
    @State private var expiry = Date().addingTimeInterval(3600)
    @State private var edited = true
    @State private var busy = false
    @State private var pending = false
    @State private var canAuthorize = false
    @State private var message: String?
    @State private var failed: String?
    private var project: DevelopmentAuthorizationProject? { projects.first { $0.id == selected } }
    private var previouslyGrantedActions: Set<String> {
        guard case .array(let values) = snapshot?.grants.first?["scope"]?.objectValue?["actions"] else { return [] }
        return Set(values.compactMap(\.stringValue))
    }
    private let names = ["read":"读取项目和准备文档", "write":"修改、验证和提交隔离副本", "remote_issue":"创建 GitHub Issue", "push":"推送开发分支并创建草稿 PR", "create":"创建本地项目", "local_init":"初始化本地仓库"]

    var body: some View {
        Form {
            Section("项目授权") {
                Text("项目权限不替代 PRD、技术设计或交付审批。")
                if !canAuthorize { Text("当前设备仅可查看，项目授权权限尚未开通。").foregroundStyle(.secondary) }
                if projects.isEmpty { Text("项目尚未接入或当前设备无可用项目权限。").foregroundStyle(.secondary) }
                else {
                    Picker("项目", selection: $selected) {
                        ForEach(projects) { Text($0.name).tag($0.id) }
                    }.disabled(snapshot?.grants.isEmpty == false || busy || pending)
                    if let project {
                        ForEach(project.actions.filter { $0 != "pr" }, id: \.self) { action in
                            Toggle(names[action] ?? action, isOn: Binding(get: { actions.contains(action) }, set: { on in
                                if on { actions.insert(action); if action == "push" { actions.insert("pr") } }
                                else { actions.remove(action); if action == "push" { actions.remove("pr") } }
                                edited = true
                            })).disabled(action == "read" || ["create","local_init"].contains(action) || previouslyGrantedActions.contains(action) || busy || pending)
                        }
                        Stepper("执行时长总上限：\(seconds) 秒", value: $seconds, in: 1...project.maxSeconds, step: 60).disabled(busy || pending)
                        DatePicker("有效至", selection: $expiry, in: Date()...Date().addingTimeInterval(Double(project.maxValidity))).disabled(busy || pending)
                        Text("费用 / reviewer：沿用现有额度，剩余待核实").font(.footnote)
                        Text("禁止合并 main、直接修改部署中的代码、自动生产部署。").font(.footnote)
                    }
                }
            }
            if let proposal = snapshot?.proposal {
                Section(proposal.status == "granted" ? "已记录的授权范围" : "冻结范围 · 请核对") {
                    ForEach(["display_name","root","remote_repository","branch","base_branch","base_sha","registration_policy","budget_seconds","expires_at"], id: \.self) { key in
                        LabeledContent(label(key), value: text(proposal.scope[key])).font(.footnote).textSelection(.enabled)
                    }
                    Text("允许动作：" + text(proposal.scope["actions"])).font(.footnote)
                    if proposal.status != "pending" { Text(proposal.status == "granted" ? "授权已记录，任务是否继续仍以最新状态为准。" : "此版本已失效，请重新核对。").foregroundStyle(.secondary) }
                }
            }
            Section {
                if let message { Text(message) }
                if let failed { Text(failed).foregroundStyle(.red) }
                if busy { ProgressView() }
                if pending {
                    Button("核对已提交操作") { Task { await recover() } }.disabled(busy)
                    Text("尚未取得最终结果时不能发出第二次授权；普通聊天不受影响。").font(.footnote)
                } else {
                    Button("核对并生成授权预览") { Task { await preview() } }.disabled(project == nil || busy || !canAuthorize)
                    Button("授权并继续") { Task { await approve() } }
                        .disabled(busy || !canAuthorize || edited || snapshot?.proposal?.status != "pending")
                }
                Button("暂不授权") { dismiss() }.disabled(busy)
            }
        }
        .navigationTitle("授权开发项目")
        .task { await reload(); await recover() }
        .refreshable { await recover(); await reload() }
        .onChange(of: selected) { _, _ in
            edited = true
            if snapshot?.grants.isEmpty == true, let project {
                actions = Set(project.actions.filter { ["read","write","create","local_init"].contains($0) })
                seconds = min(seconds, project.maxSeconds)
                expiry = min(expiry, Date().addingTimeInterval(Double(project.maxValidity)))
            }
        }
        .onChange(of: seconds) { _, _ in edited = true }
        .onChange(of: expiry) { _, _ in edited = true }
    }

    private func text(_ value: JSONValue?) -> String {
        switch value { case .string(let s): return s; case .number(let n): return String(format: "%.0f", n); case .array(let a): return a.map { text($0) }.joined(separator: "、"); default: return "无" }
    }
    private func label(_ key: String) -> String {
        ["display_name":"项目","root":"源目录（只读）","remote_repository":"仓库","branch":"开发分支","base_branch":"PR 目标分支","base_sha":"固定代码基线","registration_policy":"任务登记方式","budget_seconds":"总执行秒数","expires_at":"到期时间（含时区）"][key] ?? key
    }
    @MainActor private func reload() async {
        do {
            canAuthorize = try await session.canAuthorizeDevelopmentProject()
            let first = try await session.developmentAuthorization(id: requestID)
            var all = first.projects; var cursor = first.nextCursor; var seen = Set<String>()
            while let current = cursor {
                guard seen.insert(current).inserted else { throw AgentClientError.malformedResponse }
                let page = try await session.developmentAuthorization(id: requestID, cursor: current)
                guard page.snapshot == first.snapshot, page.total == first.total else { throw AgentClientError.malformedResponse }
                all.append(contentsOf: page.projects); cursor = page.nextCursor
            }
            guard all.count == first.total, Set(all.map(\.id)).count == all.count else { throw AgentClientError.malformedResponse }
            snapshot = first; projects = all
            if selected.isEmpty {
                let previous = first.grants.first?["scope"]?.objectValue
                selected = previous?["project_id"]?.stringValue ?? all.first?.id ?? ""
                if case .array(let values) = previous?["actions"] { actions = Set(values.compactMap(\.stringValue)) }
                else if let p = project {
                    actions = Set(p.actions.filter { ["read","write","create","local_init"].contains($0) })
                }
                if case .number(let used) = previous?["budget_seconds"] { seconds = Int(used) }
                if let p = project {
                    let proposed = Date().addingTimeInterval(Double(min(3600, p.maxValidity)))
                    let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                    let oldExpiry = previous?["expires_at"]?.stringValue.flatMap { value -> Date? in
                        if let date = formatter.date(from: value) { return date }
                        formatter.formatOptions = [.withInternetDateTime]
                        return formatter.date(from: value)
                    }
                    expiry = max(oldExpiry ?? proposed, proposed)
                    if seconds > p.maxSeconds || !actions.isSubset(of: Set(p.actions)) || expiry > Date().addingTimeInterval(Double(p.maxValidity)) {
                        canAuthorize = false
                        failed = "当前项目模板不容纳此前授权范围，需要先核对项目配置；不会自动缩减已有权限。"
                    } else { seconds = min(seconds, p.maxSeconds) }
                }
            }
        } catch { failed = "无法核对授权信息。若项目未接入或设备权限未开通，请等待配置完成。" }
    }
    @MainActor private func preview() async {
        guard !busy, let project, let snapshot else { return }
        busy = true; failed = nil; edited = true
        defer { busy = false }
        do {
            let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let grant = snapshot.grants.first
            var expectedGrant: JSONValue = .null
            var operation = "create"
            if let grant {
                guard snapshot.grants.count == 1, grant["revoked"] == .bool(false) else { throw AgentClientError.malformedResponse }
                expectedGrant = .object(["id":grant["grant_id"]!, "version":grant["version"]!, "digest":grant["digest"]!])
                let old = grant["scope"]?.objectValue?["actions"]
                let oldActions: Set<String>
                if case .array(let a) = old { oldActions = Set(a.compactMap(\.stringValue)) } else { throw AgentClientError.malformedResponse }
                operation = actions == oldActions ? "renew" : "amend"
            }
            let body: [String: JSONValue] = ["operation":.string(operation), "project_id":.string(project.id),
                "template_revision":.number(Double(project.revision)), "template_digest":.string(project.digest),
                "requested_actions":.array(actions.sorted().map(JSONValue.string)), "budget_seconds":.number(Double(seconds)),
                "grant_expires_at":.string(formatter.string(from: expiry)), "expected":.object(snapshot.expected), "expected_grant":expectedGrant]
            _ = try await session.submitDevelopmentAuthorization(requestID: requestID, target: requestID, kind: "authorization_preview", body: body)
            pending = true; message = "预览已提交，尚未授权。"; await poll()
        } catch { pending = (try? await session.pendingDevelopmentAuthorization()) != nil; failed = "预览尚未确认，请核对已提交操作。" }
    }
    @MainActor private func approve() async {
        guard !busy, !edited, let p = snapshot?.proposal, p.status == "pending" else { return }
        busy = true; failed = nil; defer { busy = false }
        do {
            let context = try await session.developmentAuthorizationContext(proposalID: p.id, digest: p.digest)
            guard context.proposalID == p.id, let token = context.token, context.commandID == nil else { throw AgentClientError.malformedResponse }
            _ = try await session.submitDevelopmentAuthorization(requestID: requestID, target: p.id, kind: "authorization_approve", body: ["context_token":.string(token), "binding_digest":.string(p.digest)])
            pending = true; message = "提交中，等待真实授权回执。"; await poll()
        } catch { pending = (try? await session.pendingDevelopmentAuthorization()) != nil; failed = "授权结果尚未确认，请核对已提交操作。" }
    }
    @MainActor private func poll() async {
        for _ in 0..<8 {
            do {
                if let result = try await session.resumeDevelopmentAuthorization() {
                    message = result.message; pending = !result.terminal
                    if result.terminal { await reload(); edited = result.status != "accepted" || snapshot?.proposal?.status != "pending"; return }
                    if result.halted { return }
                }
                try await Task.sleep(for: .seconds(2))
            } catch {
                if (error as? AgentClientError)?.errorCode == "CONFIRMATION_EXPIRED_NOT_QUEUED" {
                    pending = false; edited = true; failed = "确认已过期，服务端确认未提交。请重新生成预览。"; await reload()
                } else { failed = "结果待核实，可稍后重新查询；请勿重复授权。" }
                return
            }
        }
    }
    @MainActor private func recover() async {
        guard !busy else { return }
        busy = true; defer { busy = false }
        do {
            guard let stored = try await session.pendingDevelopmentAuthorization() else { pending = false; return }
            pending = true
            if stored.requestID != requestID { message = "另一个任务有未确认的授权操作，请先核对该任务。"; return }
            await poll()
        } catch { failed = "无法读取持久化授权操作，请稍后重试。" }
    }
}
