import PersonalAgentKit
import SwiftUI

struct RootView: View {
    @Bindable var model: AppModel

    var body: some View {
        switch model.phase {
        case .loading:
            ProgressView("正在读取本机设备凭证…")
        case .needsEnrollment:
            NavigationStack { EnrollmentView(model: model) }
        case .ready, .revoked:
            TabView {
                // `DEV-030`: chat is the primary surface. It appears only once the
                // service has named a Timeline; a revoked device gets the status
                // screen and an honest explanation instead.
                if let chat = model.chat, model.phase == .ready {
                    NavigationStack { ChatView(model: chat) }
                        .tabItem { Label("对话", systemImage: "bubble.left.and.text.bubble.right") }
                }
                // `DEV-031`: the daily review. It reads only what the service
                // proves, so it follows the same visibility rule as the chat.
                if let review = model.review, model.phase == .ready {
                    NavigationStack { ReviewListView(model: review) }
                        .tabItem { Label("复核", systemImage: "checkmark.seal") }
                }
                NavigationStack { ServiceStatusView(model: model) }
                    .tabItem { Label("状态", systemImage: "shield.lefthalf.filled") }
            }
        }
    }
}

struct EnrollmentView: View {
    @Bindable var model: AppModel

    var body: some View {
        Form {
            Section("服务地址") {
                TextField("https://agent.example.invalid", text: $model.baseURLText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                Text("默认是线上服务；本地联调（模拟器连本机服务）时改为 http://127.0.0.1:8810。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section("一次性注册码") {
                TextField("在服务器执行 personal-agent-device issue-code", text: $model.enrollmentCode)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    // The code is base64url. Without an ASCII keyboard a Chinese
                    // IME turns it into candidate text -- `_` came out as `——` on
                    // the first live attempt -- and the enrollment fails with a
                    // refusal that looks like a server problem.
                    .keyboardType(.asciiCapable)
                TextField("设备名称", text: $model.deviceName)
                Text("注册码 10 分钟内有效、只能用一次。设备私钥在 Secure Enclave 内生成，不会离开本机。")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            if let error = model.lastError {
                Section {
                    Text(error).foregroundStyle(.red)
                }
            }
            Section {
                Button {
                    Task { await model.enroll() }
                } label: {
                    if model.busy {
                        ProgressView()
                    } else {
                        Text("注册本设备")
                    }
                }
                .disabled(model.busy)
            }
        }
        // iOS 26 SDK: `scrollDismissesKeyboardMode` is an environment value now,
        // which also covers Form/List, not just ScrollView.
        .environment(\.scrollDismissesKeyboardMode, .immediately)
        .navigationTitle("注册设备")
    }
}

struct ServiceStatusView: View {
    @Bindable var model: AppModel
    /// `DEV-031`: both destructive actions are two-tap on purpose. A stray tap
    /// must not be able to strand the device server-side or orphan it locally.
    @State private var confirmingRevoke = false
    @State private var confirmingForget = false

    var body: some View {
        List {
            if model.phase == .revoked {
                Section {
                    Label("本设备已被撤销，服务端不会再签发 token。", systemImage: "xmark.shield")
                        .foregroundStyle(.red)
                }
            }

            Section("设备") {
                row("device_id", model.deviceID ?? "—")
                row("设备密钥", model.keyKind ?? "—")
                if let device = model.selfDevice {
                    row("状态", device.status)
                    row("推送 token", device.hasPushToken ? "已登记" : "未登记（Personal Team 无 APNs）")
                    row("显示名", device.displayName)
                }
            }

            Section("服务") {
                row("地址", model.baseURLText)
                row("工具集合版本", model.capabilities?.allowedToolsVersion ?? "—")
            }

            Section("本设备可用的工具") {
                if model.phase == .revoked {
                    // Not "no tools": unreadable. The distinction is the same one
                    // the服务端 makes -- an unreadable answer is never an empty one.
                    Text("无法读取：服务端已不再为本设备签发 token。")
                        .foregroundStyle(.secondary)
                } else if let tools = model.capabilities?.tools, !tools.isEmpty {
                    ForEach(tools, id: \.alias) { tool in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(tool.alias).font(.body.monospaced())
                            if let summary = tool.summary {
                                Text(summary).font(.footnote).foregroundStyle(.secondary)
                            }
                        }
                    }
                } else {
                    Text("服务端没有向本设备开放任何工具。")
                        .foregroundStyle(.secondary)
                }
            }

            if let error = model.lastError {
                Section("最近一次错误") {
                    Text(error).foregroundStyle(.red)
                }
            }

            Section {
                Button("刷新状态") { Task { await model.refresh() } }
                    .disabled(model.busy)
                if model.phase != .revoked {
                    Button("撤销本设备（服务端）", role: .destructive) {
                        confirmingRevoke = true
                    }
                    .disabled(model.busy)
                }
                Button("清除本机凭证", role: .destructive) {
                    confirmingForget = true
                }
                .disabled(model.busy)
            } footer: {
                if model.phase == .revoked {
                    Text("撤销已完成。想重新使用这台设备：先清除本机凭证，再用服务器上新的一次性注册码重新注册。")
                } else {
                    Text("清除本机凭证只删除本机密钥，不等于服务端撤销；两件事分开做。")
                }
            }
        }
        .navigationTitle("Personal Agent")
        .refreshable { await model.refresh() }
        .confirmationDialog(
            "撤销后服务端将不再为本设备签发 token，对话和复核都会立即不可用。重新使用需要服务器上的一次性注册码。",
            isPresented: $confirmingRevoke,
            titleVisibility: .visible
        ) {
            Button("确认撤销本设备", role: .destructive) {
                Task { await model.revokeSelf() }
            }
            Button("取消", role: .cancel) {}
        }
        .confirmationDialog(
            "清除本机凭证会删除 Secure Enclave 密钥。服务端上的设备记录仍然存在（成为孤儿设备），需要时在服务器执行 revoke 清理。",
            isPresented: $confirmingForget,
            titleVisibility: .visible
        ) {
            Button("确认清除本机凭证", role: .destructive) {
                Task { await model.forgetLocally() }
            }
            Button("取消", role: .cancel) {}
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .multilineTextAlignment(.trailing)
                .font(.callout)
                .textSelection(.enabled)
        }
    }
}
