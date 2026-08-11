import PersonalAgentKit
import SwiftUI
import UIKit

struct RootView: View {
    @Bindable var model: AppModel
    /// The review list, reached from the ambient bar rather than from a tab.
    @State private var showingReview = false

    var body: some View {
        switch model.phase {
        case .loading:
            ProgressView("正在读取本机设备凭证…")
        case .needsEnrollment:
            NavigationStack { EnrollmentView(model: model) }
        case .ready, .revoked:
            // §1a: one Timeline, no tab bar. The review is no longer a peer surface
            // -- §1j and §1q make every long-running item reachable the same way,
            // through the ambient bar rather than through its own tab.
            //
            // A revoked device has no Timeline to show, so the status screen becomes
            // the root. `DEV-030`'s rule is unchanged: chat appears only once the
            // service has named a Timeline.
            NavigationStack {
                if let chat = model.chat, model.phase == .ready {
                    timelineSurface(chat: chat)
                } else {
                    ServiceStatusView(model: model)
                }
            }
        }
    }

    /// The Timeline plus the ambient bar, with the status entry in the navigation
    /// bar (§1q's answer to 「状态页入口位置」: it is low-frequency, and with no
    /// sidebar the trailing slot is the only sensible place left).
    @ViewBuilder
    private func timelineSurface(chat: ChatModel) -> some View {
        VStack(spacing: 0) {
            if let review = model.review {
                AmbientOperationBar(
                    pendingCount: pendingReviewCount(review.summaries)
                ) {
                    showingReview = true
                }
            }
            ChatView(model: chat)
        }
        .background(Color.screenBackground)
        .navigationTitle("Personal Agent")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink {
                    ServiceStatusView(model: model)
                } label: {
                    // §1q#4: the dot doubles as a health indicator, so an unhealthy
                    // service is visible without opening the page. It reports only
                    // what this client actually knows -- a failed read or a revoked
                    // device -- and never guesses at the服务端's own health.
                    Circle()
                        .fill(healthColor)
                        .frame(width: 11, height: 11)
                        .accessibilityLabel(healthLabel)
                }
            }
        }
        .sheet(isPresented: $showingReview) {
            if let review = model.review {
                NavigationStack { ReviewListView(model: review) }
            }
        }
    }

    private var healthColor: Color {
        if model.phase == .revoked { return .danger }
        return model.lastError == nil ? .accentBrand : .pending
    }

    private var healthLabel: String {
        if model.phase == .revoked { return "状态：本设备已被撤销" }
        return model.lastError == nil ? "状态：正常" : "状态：最近一次读取失败"
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
            .listRowBackground(Color.cardSurface)
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
            .listRowBackground(Color.cardSurface)
            if let error = model.lastError {
                Section {
                    Text(error).foregroundStyle(.danger)
                }
                .listRowBackground(Color.cardSurface)
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
            .listRowBackground(Color.cardSurface)
        }
        // iOS 26 SDK: `scrollDismissesKeyboardMode` is an environment value now,
        // which also covers Form/List, not just ScrollView.
        .environment(\.scrollDismissesKeyboardMode, .immediately)
        .designSystemListSurface()
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
                        .foregroundStyle(.danger)
                }
                .listRowBackground(Color.cardSurface)
            }

            Section("设备") {
                row("device_id", model.deviceID ?? "—")
                row("设备密钥", model.keyKind ?? "—")
                if let device = model.selfDevice {
                    row("状态", device.status)
                    // The parenthetical states why, and the why changed on
                    // 2026-08-01: a Personal Team could never sign APNs, but a
                    // paid membership now exists, so the remaining reason is
                    // that the capability, profile and server-side `.p8` are
                    // not in place. Saying "Personal Team" here would be a
                    // stale excuse shown to the one person who knows better.
                    row("推送 token", device.hasPushToken ? "已登记" : "未登记（推送尚未启用）")
                    row("显示名", device.displayName)
                }
            }
            .listRowBackground(Color.cardSurface)

            Section("服务") {
                row("地址", model.baseURLText)
                row("工具集合版本", model.capabilities?.allowedToolsVersion ?? "—")
            }
            .listRowBackground(Color.cardSurface)

            // **Temporary bridge, and it must not outlive the gap that requires it.**
            //
            // §1a removes the review tab because §1j puts the card in the Timeline
            // and §1q's ambient bar indexes it. The bar is built; the Timeline card
            // is not, because `TimelineEvent` has no case for a review and the
            // service emits no such event. So once nothing is pending the bar
            // disappears -- correctly, by its own zero rule -- and reviewed history
            // would have no entry point at all.
            //
            // Losing reachability of a surface that passed real-device acceptance is
            // not an acceptable cost of a visual refactor. Delete this section when
            // the review card reaches the Timeline.
            if model.review != nil {
                Section("复核") {
                    NavigationLink("每日复核") { reviewDestination }
                }
                .listRowBackground(Color.cardSurface)
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
            .listRowBackground(Color.cardSurface)

            if let error = model.lastError {
                Section("最近一次错误") {
                    Text(error).foregroundStyle(.danger)
                }
                .listRowBackground(Color.cardSurface)
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
            .listRowBackground(Color.cardSurface)
        }
        .designSystemListSurface()
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

    /// A label/value row.
    ///
    /// Long opaque values -- `device_id`, the allowed-tools digest -- used to wrap
    /// to two or three ragged right-aligned lines, which is both ugly and unreadable:
    /// a hash broken across lines cannot be compared by eye anyway. They are
    /// truncated in the middle instead, where a hash's distinguishing characters sit
    /// at both ends, and the whole value stays reachable through long-press copy so
    /// nothing is actually lost.
    @ViewBuilder
    private var reviewDestination: some View {
        if let review = model.review {
            ReviewListView(model: review)
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.callout)
                .lineLimit(1)
                .truncationMode(.middle)
                .contextMenu {
                    Button {
                        UIPasteboard.general.string = value
                    } label: {
                        Label("复制\(label)", systemImage: "doc.on.doc")
                    }
                }
        }
    }
}
