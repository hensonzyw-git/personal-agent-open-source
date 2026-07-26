import PersonalAgentKit
import SwiftUI

struct RootView: View {
    @Bindable var model: AppModel

    var body: some View {
        NavigationStack {
            switch model.phase {
            case .loading:
                ProgressView("正在读取本机设备凭证…")
            case .needsEnrollment:
                EnrollmentView(model: model)
            case .ready, .revoked:
                ServiceStatusView(model: model)
            }
        }
    }
}

struct EnrollmentView: View {
    @Bindable var model: AppModel

    var body: some View {
        Form {
            Section("服务地址") {
                TextField("http://127.0.0.1:8810", text: $model.baseURLText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                Text("模拟器可直接用 127.0.0.1；真机填 Mac 的局域网地址。上线后改为 https://agent.example.invalid。")
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
        .navigationTitle("注册设备")
    }
}

struct ServiceStatusView: View {
    @Bindable var model: AppModel

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
                        Task { await model.revokeSelf() }
                    }
                    .disabled(model.busy)
                }
                Button("清除本机凭证", role: .destructive) {
                    Task { await model.forgetLocally() }
                }
                .disabled(model.busy)
            } footer: {
                Text("清除本机凭证只删除本机密钥，不等于服务端撤销；两件事分开做。")
            }
        }
        .navigationTitle("Personal Agent")
        .refreshable { await model.refresh() }
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
