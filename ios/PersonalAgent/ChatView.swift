import PersonalAgentKit
import PhotosUI
import SwiftUI
import UIKit

/// `DEV-030`'s chat screen: one continuous Timeline, cursor-paged history, and a
/// structured receipt under each message.
///
/// The wording rules are the point of this file. A receipt says 已写入 only when it
/// carries an external `record_id`; a state this build does not recognise says so
/// in as many words; and a cancel that arrived after a possible submit says the
/// outcome is still the server's to decide. None of it is derived from what the
/// model wrote.
struct ChatView: View {
    @Bindable var model: ChatModel
    @Environment(\.scenePhase) private var scenePhase
    /// `1j`. The daily-review surface, now a card inside this Timeline. `nil`
    /// only before the first refresh has opened it; the review card draws from
    /// it for the live status and the ack/defer calls, while the frozen values
    /// come from the sealed event itself.
    var review: ReviewModel? = nil
    /// The check awaiting the user's 仍然记录 confirmation. `write_anyway`
    /// forces a write past the duplicate gate, so it is never one tap.
    @State private var confirmingWriteAnyway: String?
    /// `DEV-040`. The manual-review conclusion awaiting confirmation. Also never
    /// one tap: the server records the first answer and refuses a contradicting
    /// one, so a mis-tap cannot be corrected in the app.
    @State private var confirmingResolution: ResolutionIntent?
    @State private var confirmingNewTopic = false
    /// §3c: the 详情单 sheet showing every identifier this card carries, opened
    /// from the long-press menu's second item (or the explicit 查看标识符 affordance
    /// on non-success cards). `nil` hides the sheet.
    @State private var identifiers: IdentifierSet?
    /// §3c: the head of the identifier just copied, shown as a toast so a copy
    /// confirms itself without the identifiers ever going back on the card face.
    @State private var copiedPrefix: String?
    @State private var selectedPhoto: PhotosPickerItem?
    @State private var voiceInput = VoiceInput()

    struct ResolutionIntent: Equatable {
        let operationID: String
        let resolution: ManualResolution
    }

    /// Scroll target for the in-flight bubble, which has no `event_id` to use.
    private static let sendingAnchor = "pending-send"

    var body: some View {
        VStack(spacing: 0) {
            timeline
            Divider()
            composer
        }
        .background(Color.screenBackground)
        .onChange(of: scenePhase) { _, phase in
            // Backgrounded audio is never kept as an implicit recording or
            // transformed into a partial draft after the user returns.
            if phase != .active, voiceInput.isActive {
                voiceInput.interrupt()
            }
        }
        // No title of its own: §1a makes the Timeline the whole surface, so the
        // navigation bar belongs to the app rather than to this view. `RootView`
        // sets it, together with the status entry.
        .confirmationDialog(
            "开始新话题？",
            isPresented: $confirmingNewTopic,
            titleVisibility: .visible
        ) {
            Button("放弃未提交待办并开始") {
                model.prepareNewTopic()
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("下一条消息会安全取消当前仍未提交的待办，并从新话题开始。若某项操作可能已经提交，服务端会拒绝切换，不会把它当作已放弃。")
        }
        .confirmationDialog(
            "服务端已提示这笔与现有记录疑似重复。仍然写入一条新记录？",
            isPresented: Binding(
                get: { confirmingWriteAnyway != nil },
                set: { if !$0 { confirmingWriteAnyway = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("仍然记录", role: .destructive) {
                if let checkID = confirmingWriteAnyway {
                    confirmingWriteAnyway = nil
                    Task { await model.decideDuplicate(checkID: checkID, decision: .writeAnyway) }
                }
            }
            Button("再想想", role: .cancel) { confirmingWriteAnyway = nil }
        }
        .confirmationDialog(
            confirmingResolution.map(resolutionPrompt) ?? "",
            isPresented: Binding(
                get: { confirmingResolution != nil },
                set: { if !$0 { confirmingResolution = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("确认，我已在账本里核对过") {
                if let intent = confirmingResolution {
                    confirmingResolution = nil
                    Task {
                        await model.resolveManualReview(
                            operationID: intent.operationID,
                            resolution: intent.resolution
                        )
                    }
                }
            }
            Button("再想想", role: .cancel) { confirmingResolution = nil }
        } message: {
            Text("这个结论记录后不能在应用里改判：服务端会拒绝相反的答复。它只写在这次操作旁边，不会改动账本。")
        }
        .confirmationDialog(
            "开始新话题？",
            isPresented: $confirmingNewTopic,
            titleVisibility: .visible
        ) {
            Button("放弃未提交待办并开始") {
                model.prepareNewTopic()
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("下一条消息会安全取消当前仍未提交的待办，并从新话题开始。若某项操作可能已经提交，服务端会拒绝切换，不会把它当作已放弃。")
        }
        // §3c: the 详情单 for every identifier a card carries. Off the card face
        // (they are unreadable noise there) but one menu item away, and for
        // non-success cards explicitly reachable. Full strings, no truncation,
        // each copyable — the format that exists for 报障 and 排查.
        .sheet(item: $identifiers) { set in
            NavigationStack {
                IdentifierDetailSheet(identifiers: set) { prefix in
                    withAnimation { copiedPrefix = prefix }
                }
            }
        }
        // §3c: a copy confirms itself by echoing the head of what was copied,
        // never the whole string. It appears above the composer and fades after
        // a beat; the identifiers themselves stay off the card face.
        .overlay(alignment: .bottom) {
            if let prefix = copiedPrefix {
                Text("已复制 \(prefix)…")
                    .font(.footnote)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.surface)
                    .clipShape(Capsule())
                    .padding(.bottom, 56)
                    .transition(.opacity)
                    .onAppear {
                        Task { @MainActor in
                            try? await Task.sleep(for: .seconds(1.6))
                            withAnimation { copiedPrefix = nil }
                        }
                    }
            }
        }
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button("新话题") { confirmingNewTopic = true }
                    .disabled(model.busy || model.unresolved != nil)
            }
        }
    }

    private func resolutionPrompt(_ intent: ResolutionIntent) -> String {
        switch intent.resolution {
        case .confirmedWritten:
            return "确认飞书账本里已经有这一笔？"
        case .confirmedNotWritten:
            return "确认飞书账本里没有这一笔？"
        }
    }

    private var timeline: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if model.hasOlder {
                        Button {
                            Task { await model.loadOlder() }
                        } label: {
                            if model.loadingOlder {
                                ProgressView()
                            } else {
                                Text("加载更早的记录").font(.footnote)
                            }
                        }
                        .frame(maxWidth: .infinity)
                        .disabled(model.loadingOlder)
                    }

                    // §1c. Only when the Timeline is genuinely empty -- not while
                    // history is still paging in, and not while a receipt is in
                    // flight, either of which would make "说一句话就行" a lie about
                    // what the screen knows.
                    if model.events.isEmpty, !model.hasOlder, model.liveReceipt == nil {
                        EmptyTimelineView(tools: model.tools) { model.draft = $0 }
                    }

                    ForEach(model.events) { event in
                        entry(event).id(event.eventID)
                    }

                    // §3.4: the message is on screen before the server has heard of
                    // it, and says so. It sits after the history because that is
                    // where it will land once the server's own copy arrives.
                    if let sending = model.sending {
                        sendingBubble(sending).id(Self.sendingAnchor)
                    }

                    if let receipt = model.liveReceipt,
                       !model.hasMirroredReceipt(receipt) {
                        liveCard(receipt)
                    }

                    if let error = model.lastError {
                        Text(error)
                            .font(.footnote)
                            .foregroundStyle(.danger)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding()
            }
            .refreshable { await model.refresh() }
            // iOS 26 SDK: no longer a View modifier, it is an environment value.
            .environment(\.scrollDismissesKeyboardMode, .immediately)
            .onChange(of: model.events.count) {
                if let last = model.events.last?.eventID {
                    withAnimation { proxy.scrollTo(last, anchor: .bottom) }
                }
            }
            // The optimistic bubble is the point at which the user expects to see
            // their message; arriving without scrolling to it would put it off
            // screen on a full Timeline and look exactly like nothing happened.
            .onChange(of: model.sending != nil) { _, isSending in
                guard isSending else { return }
                withAnimation { proxy.scrollTo(Self.sendingAnchor, anchor: .bottom) }
            }
        }
    }

    /// The in-flight message: the same bubble it will become, dimmed, with 发送中
    /// under it — and, once the server has proven it, the stage trail.
    ///
    /// Drawn from the bubble the Timeline uses rather than a separate style, so
    /// nothing moves or changes shape when the server's copy replaces it. What
    /// distinguishes the two is opacity and the label — §3.2 in miniature: 发送中 is
    /// a weaker claim than 已发送, and the screen must not let the first read as the
    /// second.
    ///
    /// The trail under 发送中 carries only what the server's own operation
    /// projection stated: a stage name, and the registered tool name once one
    /// was selected. There is no model reasoning on it, by construction — the
    /// server never puts thought text in the operation row.
    private func sendingBubble(_ text: String) -> some View {
        VStack(alignment: .trailing, spacing: 3) {
            Text(text)
                .foregroundStyle(.white)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color.userBubble)
                .clipShape(
                    UnevenRoundedRectangle(
                        topLeadingRadius: Metric.bubbleRadius,
                        bottomLeadingRadius: Metric.bubbleRadius,
                        bottomTrailingRadius: Metric.bubbleTailRadius,
                        topTrailingRadius: Metric.bubbleRadius
                    )
                )
                .opacity(0.55)
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text("发送中")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            if !model.liveStages.isEmpty {
                stageTrail(model.liveStages)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }

    /// The execution pipeline under a sending bubble: 已接收 → 正在理解 → 已选择工具
    /// → 正在写入 → 正在核对. Only the stages the server has actually reached are
    /// drawn, each with its own spinner state — the current one spins, the ones
    /// before it show checkmarks, and nothing is ever shown before the server
    /// said so.
    private func stageTrail(_ stages: [OperationStage]) -> some View {
        // Collapse duplicates: the poll may observe the same stage many times.
        // `stages` arrives in observation order, and the pipeline's own order is
        // the state machine's, so the first observation of each distinct stage
        // wins. Dispatching always carries the tool in production — both server
        // transitions write a non-optional tool (orchestrator's model path and
        // the override replay path) — so `dispatching(tool: nil)` never arrives
        // and one dispatching entry is all there is.
        var collapsed: [OperationStage] = []
        for stage in stages where !collapsed.contains(where: { $0 == stage }) {
            collapsed.append(stage)
        }
        let current = collapsed.last
        let currentIndex = collapsed.count - 1
        return HStack(spacing: 8) {
            ForEach(Array(collapsed.enumerated()), id: \.offset) { index, stage in
                HStack(spacing: 3) {
                    if index == currentIndex {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "checkmark")
                            .font(.caption2.weight(.semibold))
                    }
                    Text(stageLabel(stage, isCurrent: index == currentIndex))
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func stageLabel(_ stage: OperationStage, isCurrent: Bool) -> String {
        switch stage {
        case .accepted: return "已接收"
        case .interpreting: return "正在理解"
        case .dispatching(let tool):
            // The registered tool name is the server's own fact; showing it is
            // exactly the point of the trail. The generic label is a defensive
            // fallback only — the server's dispatching transitions always
            // carry a non-optional tool, so it should never render.
            if let tool { return isCurrent ? "已选择 \(tool)" : tool }
            return "已选择工具"
        case .sourceInProgress: return "正在写入"
        case .verifying: return "正在核对"
        }
    }

    // --- one Timeline entry ---------------------------------------------------

    @ViewBuilder
    private func entry(_ event: TimelineEvent) -> some View {
        switch event.kind {
        case .userMessage(let text, let clarificationOf):
            VStack(alignment: .trailing, spacing: 2) {
                Text(text)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Color.userBubble)
                    .clipShape(
                        UnevenRoundedRectangle(
                            topLeadingRadius: Metric.bubbleRadius,
                            bottomLeadingRadius: Metric.bubbleRadius,
                            bottomTrailingRadius: Metric.bubbleTailRadius,
                            topTrailingRadius: Metric.bubbleRadius
                        )
                    )
                if clarificationOf != nil {
                    Text("补充澄清").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)

        case .operationResult(let outcome, let state, let toolEvidence):
            receiptCard(
                outcome: outcome,
                state: state,
                operationID: event.operationID,
                cancellation: .none,
                toolEvidence: toolEvidence
            )

        case .sessionDivider(let reason, let corrected):
            HStack {
                Rectangle().frame(height: 1).foregroundStyle(.quaternary)
                Text(corrected ? "话题边界已修正\(reasonSuffix(reason))" : "新话题\(reasonSuffix(reason))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize()
                Rectangle().frame(height: 1).foregroundStyle(.quaternary)
            }

        case .duplicateDecision(let checkID, let decision):
            Label(
                "疑似重复已处理：\(duplicateDecisionText(decision))",
                systemImage: "checkmark.circle"
            )
            .font(.caption)
            .foregroundStyle(.secondary)
            .accessibilityHint("duplicate_check_id \(checkID)")

        case .expenseCategoryCorrected(_, let record):
            Label(
                "分类已修改为 \(record.category ?? "未分类")",
                systemImage: "tag.circle"
            )
            .font(.caption)
            .foregroundStyle(.secondary)

        case .manualReviewResolved(let resolution):
            Label(
                "已人工核对：\(manualResolutionText(resolution))",
                systemImage: "person.crop.circle.badge.checkmark"
            )
            .font(.caption)
            .foregroundStyle(.secondary)

        case .dailyReview(let snapshot):
            if model.isSupersededDailyReview(event) {
                // A newer snapshot reopened this card. The older one stays in the
                // sealed archive, and the screen says so rather than drawing the
                // same review twice with a different item count.
                Label("复核卡已更新", systemImage: "checkmark.seal")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                reviewCard(snapshot: snapshot)
            }

        case .riskReport(let snapshot):
            riskReportCard(snapshot: snapshot)

        case .unrecognised(let eventType):
            // Not dropped: a history that silently omits entries is a history that
            // lies about what happened.
            Text("本客户端无法识别的事件（\(eventType)）")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func reasonSuffix(_ reason: String?) -> String {
        guard let reason, !reason.isEmpty else { return "" }
        return "·\(reason)"
    }

    // --- the daily review card (`1j`) -----------------------------------------

    /// The review card, drawn from the frozen snapshot sealed in the Timeline.
    ///
    /// The values come straight from the event — never re-read from Feishu — and
    /// the status comes from `review`, the one field that keeps changing after
    /// the snapshot was sealed. ack/defer move the status; 打开飞书账本 is the way
    /// to a correction, which the card itself never makes.
    @ViewBuilder
    private func reviewCard(snapshot: ReviewCardSnapshot) -> some View {
        // The status is live; until the review list has loaded it is unknown,
        // and an unknown status is not "pending". The card then shows no badge
        // and no buttons rather than guessing 待复核 for a card that may already
        // be 已复核.
        let status = review?.summary(for: snapshot.reviewID)?.status
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("每日复核 · \(snapshot.reviewDate)")
                    .font(.callout.weight(.medium))
                Spacer(minLength: 8)
                if let status {
                    reviewStatusCapsule(status)
                }
            }
            .padding(.horizontal, Metric.cardInset)
            .padding(.top, Metric.cardHeaderTop)
            .padding(.bottom, Metric.cardHeaderBottom)

            Text("\(snapshot.itemCount) 笔写入")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Metric.cardInset)
                .padding(.vertical, Metric.fieldRowPadding)
                .overlay(alignment: .top) { hairline }

            VStack(spacing: 0) {
                ForEach(snapshot.items) { item in
                    reviewItemRow(item)
                }
            }
            .padding(.horizontal, Metric.cardInset)

            if let status {
                reviewActionRow(snapshot: snapshot, status: status)
                    .overlay(alignment: .top) { hairline }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cardSurface)
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(Color.cardBorder, lineWidth: Metric.hairline)
        )
    }

    // --- the systemic-risk card ------------------------------------------------

    /// The risk card, drawn from the frozen snapshot sealed in the Timeline. The
    /// values come straight from the event — never re-pulled from FRED/Tencent —
    /// so scrolling back always shows the score as it was sealed that day.
    @ViewBuilder
    private func riskReportCard(snapshot: RiskReportSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("系统性风险 · \(snapshot.asOf)")
                    .font(.callout.weight(.medium))
                Spacer(minLength: 8)
                if snapshot.anomalous == true {
                    riskBadge("异常", .danger)
                }
                if let stale = snapshot.staleDays, stale > 7 {
                    riskBadge("数据过期", .pending)
                }
                if snapshot.qualityStatus == "data_quality_warning" {
                    riskBadge("数据不完整", .pending)
                }
                Text(riskStateLabel(snapshot.state))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, Metric.cardInset)
            .padding(.top, Metric.cardHeaderTop)
            .padding(.bottom, Metric.cardHeaderBottom)

            Text(riskScoresLine(snapshot))
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Metric.cardInset)
                .padding(.vertical, Metric.fieldRowPadding)
                .overlay(alignment: .top) { hairline }

            if let components = snapshot.components {
                riskComponentGroup("MBS 指标", components.mbs)
                riskComponentGroup("CSS 指标", components.css)
                if let ratesCredit = components.ratesCredit, !ratesCredit.isEmpty {
                    riskComponentGroup("RCS 美债与金融条件", ratesCredit)
                }
            }

            if let action = snapshot.action, !action.isEmpty {
                Text(action)
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Metric.cardInset)
                    .padding(.vertical, Metric.fieldRowPadding)
                    .overlay(alignment: .top) { hairline }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cardSurface)
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(Color.cardBorder, lineWidth: Metric.hairline)
        )
    }

    private func riskStateLabel(_ state: String) -> String {
        switch state {
        case "NORMAL": return "正常"
        case "RISK_ACCUMULATION": return "风险累积"
        case "CREDIT_CONFIRMATION": return "信用确认"
        case "DELEVERAGING": return "去杠杆"
        default: return state
        }
    }

    private func riskScoresLine(_ snapshot: RiskReportSnapshot) -> String {
        func fmt(_ value: Double?) -> String {
            guard let value else { return "-" }
            return String(format: "%.1f", value)
        }
        return "MBS \(fmt(snapshot.mbs)) / CSS \(fmt(snapshot.css)) / AFRS \(fmt(snapshot.afrs)) / RCS \(fmt(snapshot.ratesCredit))"
    }

    /// A small status capsule for a quality signal (degraded / stale / anomalous)
    /// so a card with suspicious data can never masquerade as a clean one.
    private func riskBadge(_ text: String, _ color: Color) -> some View {
        Text(text)
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.15))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }

    /// The band's traffic-light colour for the status dot. Green/orange/red are
    /// the standard risk convention; the dot is a status label, not a second
    /// brand colour (DesignTokens §7).
    private func riskBandColor(_ band: String) -> Color {
        switch band {
        case "green": return .green
        case "yellow": return .yellow
        case "orange": return .orange
        case "red": return .red
        default: return .secondary
        }
    }

    /// One indicator group (MBS or CSS): a quiet header over its rows.
    @ViewBuilder
    private func riskComponentGroup(_ title: String, _ rows: [RiskComponent]) -> some View {
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Metric.cardInset)
                    .padding(.top, Metric.fieldRowPadding)
                    .padding(.bottom, 4)

                ForEach(rows, id: \.label) { component in
                    riskComponentRow(component)
                }
            }
            .overlay(alignment: .top) { hairline }
        }
    }

    /// A single indicator row: band dot + label on the left, the frozen value on
    /// the right. The value is a string already formatted by the backend, never a
    /// re-derived number.
    private func riskComponentRow(_ component: RiskComponent) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Circle()
                .fill(riskBandColor(component.band))
                .frame(width: 8, height: 8)
            Text(component.label)
                .font(.footnote)
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(component.value)
                .font(.footnote)
        }
        .padding(.horizontal, Metric.cardInset)
        .padding(.vertical, 6)
    }

    private func reviewStatusCapsule(_ status: ReviewStatus) -> some View {
        let (text, color): (String, Color) = switch status {
        case .pending: ("待复核", .pending)
        case .reviewed: ("已复核", .accentText)
        case .deferred:
            // 「稍后处理」是贪睡, shown as 已推迟 to tell it apart from 已复核 —
            // the card is not done, it is paused until the next 0:00.
            ("已推迟", .pending)
        case .unrecognised(let raw): (raw, .secondary)
        }
        return Text(text)
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.15))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }

    /// One record on the card, with the values frozen at build time.
    @ViewBuilder
    private func reviewItemRow(_ item: ReviewItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(item.tableKind ?? item.tool).font(.callout.weight(.medium))
                Spacer()
                Text(item.committedAt).font(.caption).foregroundStyle(.secondary)
            }
            if let unavailable = item.unavailable {
                // The row stays, with its reason: a count that quietly loses a
                // record is a review that lies about what was written.
                Label(
                    reviewUnavailableText(unavailable),
                    systemImage: "exclamationmark.triangle"
                )
                .font(.footnote)
                .foregroundStyle(.pending)
            }
            if let values = item.values {
                ForEach(values.keys.sorted(), id: \.self) { key in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(key).font(.caption).foregroundStyle(.secondary)
                        Spacer(minLength: 12)
                        Text(values[key]?.displayText ?? "—")
                            .font(.callout)
                            .tabularNumbers()
                            .multilineTextAlignment(.trailing)
                            .textSelection(.enabled)
                    }
                }
            }
            if !item.unreadableFields.isEmpty {
                Text("以下字段服务端未能解析：\(item.unreadableFields.joined(separator: "、"))")
                    .font(.caption)
                    .foregroundStyle(.pending)
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("record_id").font(.caption).foregroundStyle(.secondary)
                Text(item.recordID)
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
            }
        }
        .padding(.vertical, 4)
        .overlay(alignment: .top) { hairline }
    }

    @ViewBuilder
    private func reviewActionRow(
        snapshot: ReviewCardSnapshot, status: ReviewStatus
    ) -> some View {
        HStack(spacing: 12) {
            if let review, status.allowsReviewActions {
                Button("确认都正确") {
                    Task { await review.ack(reviewID: snapshot.reviewID) }
                }
                .buttonStyle(.borderedProminent)
                Button("稍后处理") {
                    Task { await review.deferCard(reviewID: snapshot.reviewID) }
                }
                .buttonStyle(.bordered)
            }
            if let url = review?.ledgerURL {
                Link("打开飞书账本", destination: url)
                    .buttonStyle(.bordered)
            } else if review != nil {
                Text("服务端未提供账本链接")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .disabled(review?.busy ?? false)
        .padding(Metric.cardInset)
    }

    private func reviewUnavailableText(_ reason: String) -> String {
        switch reason {
        case "unknown_tool": return "服务端不认识该写入工具，无法读取当前值"
        case "source_unavailable": return "暂时无法从飞书读取当前值，请稍后重新打开"
        case "no_receipt": return "服务端没有找到这条记录的回执"
        default: return "无法读取当前值（\(reason)）"
        }
    }

    // --- receipts -------------------------------------------------------------

    @ViewBuilder
    private func receiptCard(
        outcome: OperationOutcome,
        state: OperationState,
        operationID: String?,
        cancellation: CancellationNote,
        toolEvidence: ToolEvidence
    ) -> some View {
        // §3a: 回执三档, and the tier is decided by how many non-empty fields the
        // receipt actually carries, not by a switch the server toggles. Today the
        // projection carries zero business fields, so every recorded receipt lands
        // on tier one (a status row) -- the 2026-08-11 acceptance call "格式对了,
        // 就是很丑" is fixed here, by removing the chrome a field-less receipt
        // cannot justify. When G1 lands and the projection starts carrying fields,
        // this same code promotes the receipt to tier two (a row) and then tier
        // three (the composed card) without a client-side switch.
        //
        // §1i's non-recorded states are plain label-and-body cards and do not take
        // part in the tiers at all. A query result is its own card too: structured
        // data is never flattened into a prose answer (§1i).
        if case .recorded(let recordID, let tool, let record) = outcome {
            recordedReceipt(
                recordID: recordID,
                tool: tool,
                record: record,
                operationID: operationID
            )
        } else if case .answeredWithQuery(let result, let tool) = outcome {
            queryReceiptCard(result: result, tool: tool, operationID: operationID)
        } else {
            plainReceiptCard(
                outcome: outcome,
                state: state,
                operationID: operationID,
                cancellation: cancellation,
                toolEvidence: toolEvidence
            )
        }
    }

    // --- §3a/§3b: the receipt's three tiers --------------------------------

    /// A field a receipt carries. Kept as a labelled pair so tier three can draw
    /// a divided field area and tier two can draw the same values side by side;
    /// the tier is chosen from how many of these the receipt has.
    private struct ReceiptField: Identifiable, Equatable {
        let label: String
        let value: String
        var id: String { label }
    }

    /// The fields the server actually returned.
    ///
    /// `G1` landed in `chat_receipt_projection_v5`, and the tier rules were
    /// already written against this array, so a receipt that carries the row
    /// promotes itself to 档三 on the same code path that used to draw 档一.
    /// A receipt without one -- an `idempotent_replay`, an older event, a
    /// payload that failed projection -- returns `[]` and still gets the honest
    /// status row.
    ///
    /// Order is 日期 → 名称 → 分类 → 金额 → 是否家庭支出 → 个人支出: what the
    /// entry *is* before what it *cost*, which is the order Henson asked for and
    /// the order the ledger's own columns read in.
    private func receiptFields(for record: FinanceExpenseRecord?) -> [ReceiptField] {
        guard let record else { return [] }
        var fields: [ReceiptField] = [
            ReceiptField(label: "日期", value: record.occurredOn),
            ReceiptField(label: "名称", value: record.name),
            // Null category is a real state for a refund or AA receipt, so it
            // is shown as absent rather than omitted: a missing row would read
            // as "the server did not say", and this row is about to become
            // editable, so which one it is matters.
            ReceiptField(label: "分类", value: record.category ?? "未分类"),
            ReceiptField(label: "金额", value: yuan(record.amount)),
            ReceiptField(
                label: "是否家庭支出", value: record.isFamilyExpense ? "是" : "否"
            ),
        ]
        // 个人支出 is a Base formula. Absent means the ledger had not evaluated
        // it (or had just been asked to re-evaluate it after a category edit),
        // and an absent row is honest where a stale number would not be.
        if let personalSpend = record.personalSpend {
            fields.append(
                ReceiptField(label: "个人支出", value: yuan(personalSpend))
            )
        }
        return fields
    }

    /// Render a ledger amount without ever parsing it.
    ///
    /// The server sends decimal *text* precisely so no float ever touches a
    /// money value; turning it into a `Double` here to format it would undo
    /// that at the last step, on the one screen whose job is to be checkable.
    /// A negative amount keeps its sign ahead of the symbol -- `-¥880.00` --
    /// because a refund reads as a refund, not as a smaller expense.
    private func yuan(_ amount: String) -> String {
        amount.hasPrefix("-") ? "-¥" + amount.dropFirst() : "¥" + amount
    }

    @ViewBuilder
    private func recordedReceipt(
        recordID: String,
        tool: String?,
        record: FinanceExpenseRecord?,
        operationID: String?
    ) -> some View {
        // `G1` shipped, so the current ledger row is what the card draws. The
        // overlay is what makes Henson's 2026-08-15 decision true: after a
        // category correction the *same* ledger row is described by a newer
        // operation, and every card for that row -- including the original
        // receipt, scrolled back to -- follows the ledger rather than freezing
        // at what was first written.
        let current = model.currentRecord(forRecordID: recordID) ?? record
        let fields = receiptFields(for: current)
        switch fields.count {
        case 0, 1, 2:
            // §3a/§3b 档一/档二: a status row. No container, no border, no action
            // row; 打开飞书账本 becomes an end-of-row link. Tier two (1-2 fields)
            // is the same row with the fields side by side under the trace line;
            // tier one (0 fields) is the row alone. Both are deliberately the
            // lightest form -- a card would imply a structured record to check,
            // and today there is none to check.
            recordedStatusRow(
                recordID: recordID,
                tool: tool,
                record: current,
                operationID: operationID,
                fields: fields
            )
        default:
            // §3a/§3b 档三: the composed card, kept for when ≥3 fields exist.
            // Its 副标带 is already gone (§2.2): identifiers live behind the
            // long-press menu, so the band has nothing to carry.
            recordedCard(
                recordID: recordID,
                tool: tool,
                record: current,
                operationID: operationID,
                fields: fields
            )
        }
    }

    /// §3a/§3b 档一档二: 状态行. The lightest possible receipt.
    ///
    /// No container: a card around a single line of text spends its whole border
    /// budget announcing a structured record that does not exist yet. What the
    /// row carries is exactly what the server proved -- the terminal state, the
    /// tool, and the way to the evidence.
    private func recordedStatusRow(
        recordID: String,
        tool: String?,
        record: FinanceExpenseRecord?,
        operationID: String?,
        fields: [ReceiptField]
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            // §3d: no header row on tiers one/two, so the terminal-state label is
            // a leading chip rather than the header's 12pt capsule. Same colour
            // semantics, different chrome.
            terminalChip(
                for: .recorded(recordID: recordID, tool: tool, record: record),
                toolEvidence: .known(tool)
            )

            // §3a's 轨迹文字: 财务 · 记一笔支出, drawn from the granted tool set
            // rather than the raw alias, so the row reads as a tool did something.
            if let tool, !tool.isEmpty {
                Text(Capabilities.displayName(forAlias: tool, tools: model.tools))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            // Tier two only: the 1-2 fields side by side, without their names
            // (§3b: 字段并排不成表). Tier one has none.
            if !fields.isEmpty {
                HStack(spacing: 12) {
                    ForEach(fields) { field in
                        Text(field.value)
                            .font(.footnote.monospaced())
                            .tabularNumbers()
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
            }

            // §3b 约束三: 档一档二没有动作行, and 打开飞书账本 降级为行尾链接. 再记一笔
            // does not appear -- a receipt with nothing to verify must not invite
            // another write.
            if let ledgerURL = model.ledgerURL {
                Link("打开飞书账本", destination: ledgerURL)
                    .font(.footnote)
                    .foregroundStyle(.accentText)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .modifier(evidenceMenu(recordID: recordID, operationID: operationID, explicit: false))
    }

    // §3a/§3b 档三: the composed card, reached only once the receipt carries
    // ≥3 fields. Header, divided field rows, footer action row; no 副标带.
    private func recordedCard(
        recordID: String,
        tool: String?,
        record: FinanceExpenseRecord?,
        operationID: String?,
        fields: [ReceiptField]
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("记账回执").font(.callout.weight(.medium))
                Spacer(minLength: 8)
                terminalCapsule(
                    for: .recorded(recordID: recordID, tool: tool, record: record),
                    toolEvidence: .known(tool)
                )
            }
            .padding(.horizontal, Metric.cardInset)
            .padding(.top, Metric.cardHeaderTop)
            .padding(.bottom, Metric.cardHeaderBottom)

            // The identifiers are deliberately not rows. `operation_id` is an
            // internal request handle, and while `record_id` **is** the external
            // evidence that separates 已写入 from a model's claim, a 20-character
            // opaque string is not what makes it readable -- it is what makes the
            // card unreadable. Both stay reachable through long-press, so the
            // evidence is one gesture away rather than gone.
            VStack(spacing: 0) {
                if let tool { fieldRow("工具", Capabilities.displayName(forAlias: tool, tools: model.tools)) }
                ForEach(fields) { field in
                    if field.label == "分类", let record {
                        categoryRow(recordID: recordID, record: record)
                    } else {
                        fieldRow(field.label, field.value)
                    }
                }
                if let editedAt = record?.categoryUpdatedAt {
                    // The card follows the ledger's current value rather than
                    // freezing at what was written (Henson, 2026-08-15), which
                    // means it is no longer literally the write receipt. This
                    // line is that difference stated rather than hidden.
                    categoryEditNote(editedAt)
                }
            }
            .padding(.horizontal, Metric.cardInset)

            recordedActionRow
                .overlay(alignment: .top) { hairline }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cardSurface)
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(Color.cardBorder, lineWidth: Metric.hairline)
        )
        .modifier(evidenceMenu(recordID: recordID, operationID: operationID, explicit: false))
    }

    /// Wire a card's long-press menu to the ChatView's identifier sheet and copy
    /// toast. `explicit` turns on §3c's visible 查看标识符 affordance for the
    /// non-success cards that need it.
    private func evidenceMenu(
        recordID: String?, operationID: String?, explicit: Bool
    ) -> EvidenceMenu {
        EvidenceMenu(
            recordID: recordID,
            operationID: operationID,
            showExplicitEntry: explicit,
            onShowIdentifiers: {
                identifiers = IdentifierSet(recordID: recordID, operationID: operationID)
            },
            onCopied: { prefix in
                withAnimation { copiedPrefix = prefix }
            }
        )
    }

    /// Long-press exposes the identifiers the card no longer prints.
    ///
    /// They were removed from the face because they are unreadable noise, not
    /// because they stopped mattering: `record_id` is still the only thing that
    /// makes 已写入 checkable against Feishu, and a receipt whose evidence cannot be
    /// retrieved at all is a receipt that has to be taken on trust.
    ///
    /// §3c's three-tier retrieval lives here: 打开飞书账本 is the first tier (already
    /// on the row), this menu is the second (直接复制 each id), and the 详情单 is
    /// the third — the menu's second item, listing every identifier in full with
    /// its own per-item copy, for the cases that need the whole string (报障/排查).
    /// Copies confirm themselves through a toast echoing the head of the string,
    /// never the whole identifier.
    private struct EvidenceMenu: ViewModifier {
        let recordID: String?
        let operationID: String?
        /// True on non-success cards, which §3c says must offer 查看标识符 explicitly
        /// — long-press is an invisible gesture, and those are the cards where the
        /// user actually needs the id.
        let showExplicitEntry: Bool
        let onShowIdentifiers: () -> Void
        let onCopied: (String) -> Void

        func body(content: Content) -> some View {
            content
                .contextMenu {
                    if let recordID {
                        Button {
                            UIPasteboard.general.string = recordID
                            onCopied(String(recordID.prefix(8)))
                        } label: {
                            Label("复制记录 ID", systemImage: "doc.on.doc")
                        }
                    }
                    if recordID != nil || operationID != nil {
                        Button {
                            onShowIdentifiers()
                        } label: {
                            Label("标识符", systemImage: "list.bullet.rectangle")
                        }
                    }
                    if let operationID {
                        Button {
                            UIPasteboard.general.string = operationID
                            onCopied(String(operationID.prefix(8)))
                        } label: {
                            Label("复制 operation_id", systemImage: "number")
                        }
                    }
                }
                .overlay(alignment: .topTrailing) {
                    // §3c: the explicit affordance on non-success cards. Long-press
                    // is discoverable only to whoever already knows it exists; these
                    // cards are the ones where the id is the way to the evidence, so
                    // it gets a visible entry. Success cards keep it hidden.
                    if showExplicitEntry, recordID != nil || operationID != nil {
                        Button {
                            onShowIdentifiers()
                        } label: {
                            Image(systemName: "ellipsis.circle")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                        .padding(6)
                    }
                }
        }
    }

    private var hairline: some View {
        Rectangle()
            .fill(Color.hairlineDivider)
            .frame(height: Metric.hairline)
    }

    /// A field row carries its own top rule, so rows stack without a trailing one.
    /// 分类, as a picker over the ledger's own option set.
    ///
    /// The one editable row on the card. It is a `Menu` rather than a sheet
    /// because the whole point is that correcting a mis-categorised expense
    /// costs one tap and one choice -- re-describing the entry to the Agent was
    /// always possible and was always the wrong repair.
    ///
    /// The options come from `ExpenseCategory.all`, which is pinned to the
    /// ledger's single-select options by the cross-language vector file. The
    /// connector never creates a select option, so an option this client
    /// invented would be a refused write, not a new category.
    ///
    /// Nothing here is optimistic. The row shows the ledger's value until the
    /// server has verified the change against the ledger; while the write is in
    /// flight it shows the target with a progress indicator, and a failure
    /// leaves the *old* value on screen with the reason beneath. Showing the new
    /// category before it was proven would be the receipt card telling the same
    /// kind of lie the whole projection exists to prevent.
    @ViewBuilder
    private func categoryRow(
        recordID: String, record: FinanceExpenseRecord
    ) -> some View {
        let edit = model.categoryEdit(forRecordID: recordID)
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("分类").font(.footnote).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            if case .inFlight(let target) = edit {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text(target)
                        .font(.footnote.monospaced())
                        .foregroundStyle(.secondary)
                }
            } else {
                Menu {
                    ForEach(ExpenseCategory.all, id: \.self) { option in
                        Button {
                            Task {
                                await model.changeCategory(
                                    recordID: recordID,
                                    from: record.category,
                                    to: option
                                )
                            }
                        } label: {
                            if option == record.category {
                                Label(option, systemImage: "checkmark")
                            } else {
                                Text(option)
                            }
                        }
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(record.category ?? "未分类")
                            .font(.footnote.monospaced())
                            .lineLimit(1)
                        Image(systemName: "chevron.up.chevron.down")
                            .font(.caption2)
                    }
                    .foregroundStyle(.accentText)
                }
            }
        }
        .padding(.vertical, Metric.fieldRowPadding)
        .overlay(alignment: .top) { hairline }

        if case .failed(let message) = edit {
            // Deliberately below the row, with the old value still shown above
            // it: the ledger did not change, and the card must not imply it did.
            Text(message)
                .font(.caption)
                // `pending`, not `danger`: nothing was written and nothing is
                // broken. The ledger simply does not hold what this card
                // assumed, and the next move is Henson's.
                .foregroundStyle(.pending)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.bottom, Metric.fieldRowPadding)
        }
    }

    private func categoryEditNote(_ editedAt: String) -> some View {
        Text("分类已于 \(ChatView.editStamp(editedAt)) 修改")
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, Metric.fieldRowPadding)
            .overlay(alignment: .top) { hairline }
    }

    /// Render the server's RFC 3339 stamp in the ledger's own timezone.
    ///
    /// Falls back to the raw string rather than to "just now" or an empty label:
    /// a timestamp this build cannot parse is still evidence that an edit
    /// happened, and dropping it would erase the one thing this line exists for.
    static func editStamp(_ value: String) -> String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let moment = parser.date(from: value) ?? {
            let plain = ISO8601DateFormatter()
            plain.formatOptions = [.withInternetDateTime]
            return plain.date(from: value)
        }()
        guard let moment else { return value }
        let display = DateFormatter()
        display.locale = Locale(identifier: "zh_Hans_CN")
        display.timeZone = TimeZone(identifier: "Asia/Shanghai")
        display.dateFormat = "M月d日 HH:mm"
        return display.string(from: moment)
    }

    private func fieldRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label).font(.footnote).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.footnote.monospaced())
                .tabularNumbers()
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .padding(.vertical, Metric.fieldRowPadding)
        .overlay(alignment: .top) { hairline }
    }

    /// A structured Finance query result rendered as its own card, per view.
    ///
    /// This is not a prose answer and not a receipt: the server validated the
    /// projection, and the three views are deliberately different -- a total
    /// shows one figure with its 口径, by_category shows the breakdown, and
    /// records shows a bounded page with a 继续查看 entry instead of claiming a
    /// page is the whole answer. None of it comes from what the model wrote.
    @ViewBuilder
    private func queryReceiptCard(
        result: FinanceQueryResult, tool: String?, operationID: String?
    ) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("查询结果").font(.callout.weight(.medium))
                Spacer(minLength: 8)
                if let tool {
                    Text(tool).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, Metric.cardInset)
            .padding(.top, Metric.cardHeaderTop)
            .padding(.bottom, Metric.cardHeaderBottom)

            VStack(spacing: 0) {
                queryScope(result)
                switch result.view {
                case .total:
                    queryTotal(result)
                case .byCategory:
                    queryByCategory(result)
                case .records:
                    queryRecords(result)
                }
            }
            .padding(.horizontal, Metric.cardInset)
            .padding(.bottom, Metric.cardInset)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cardSurface)
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(Color.cardBorder, lineWidth: Metric.hairline)
        )
        // §3c: the same long-press menu and identifier sheet as the recorded
        // receipt. A query has no `record_id` of its own, but its `operation_id`
        // is what the evidence would be checked against, and it stays reachable.
        .modifier(evidenceMenu(recordID: nil, operationID: operationID, explicit: false))
    }

    /// The query 口径 (date range / category filters) the result was computed
    /// under, so the figure is not read as "everything" when it is not.
    @ViewBuilder
    private func queryScope(_ result: FinanceQueryResult) -> some View {
        let dates = result.filtersApplied["date_range"]?.objectValue
        let start = dates?["start"]?.stringValue
        let end = dates?["end"]?.stringValue
        let categories = result.filtersApplied["categories"]?.arrayValue?
            .compactMap { $0.stringValue }
        if start != nil || end != nil || !(categories?.isEmpty ?? true) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline) {
                    Text("筛选口径").font(.footnote).foregroundStyle(.secondary)
                    Spacer(minLength: 12)
                    Text(scopeText(start: start, end: end))
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                if let categories, !categories.isEmpty {
                    Text("分类：\(categories.joined(separator: "、"))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.vertical, Metric.fieldRowPadding)
            .overlay(alignment: .top) { hairline }
        }
    }

    private func scopeText(start: String?, end: String?) -> String {
        switch (start, end) {
        case (let s?, let e?): return "\(s) ~ \(e)"
        case (let s?, nil): return "从 \(s) 起"
        case (nil, let e?): return "至 \(e)"
        case (nil, nil): return "全部时间"
        }
    }

    private func queryTotal(_ result: FinanceQueryResult) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("个人支出合计").font(.callout).foregroundStyle(.secondary)
                Spacer(minLength: 12)
                Text("¥\(result.amount ?? "—")")
                    .font(.title3.weight(.semibold))
                    .tabularNumbers()
            }
            .padding(.vertical, Metric.fieldRowPadding)
            .overlay(alignment: .top) { hairline }
            fieldRow("记录数", "\(result.recordCount)")
            if !result.sourceSystem.isEmpty {
                fieldRow("数据源", result.sourceSystem)
            }
        }
    }

    private func queryByCategory(_ result: FinanceQueryResult) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("合计").font(.callout).foregroundStyle(.secondary)
                Spacer(minLength: 12)
                Text("¥\(result.amount ?? "—")")
                    .font(.title3.weight(.semibold))
                    .tabularNumbers()
            }
            .padding(.vertical, Metric.fieldRowPadding)
            .overlay(alignment: .top) { hairline }
            ForEach(Array(result.byCategory.enumerated()), id: \.offset) { _, bucket in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(bucket.category ?? "未分类").font(.callout)
                    Spacer(minLength: 12)
                    if let share = bucket.share {
                        Text("\(share)%").font(.caption).foregroundStyle(.secondary)
                    }
                    Text(bucket.amount)
                        .font(.callout.monospaced())
                        .tabularNumbers()
                }
                .padding(.vertical, Metric.fieldRowPadding)
                .overlay(alignment: .top) { hairline }
            }
            fieldRow("记录数", "\(result.recordCount)")
        }
    }

    private func queryRecords(_ result: FinanceQueryResult) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(result.records.enumerated()), id: \.offset) { _, row in
                VStack(alignment: .leading, spacing: 2) {
                    Text(row.name).font(.callout)
                    HStack(spacing: 8) {
                        Text("¥\(row.amount)")
                            .font(.caption.monospaced())
                            .tabularNumbers()
                            .foregroundStyle(.secondary)
                        if let occurredOn = row.occurredOn {
                            Text(occurredOn).font(.caption).foregroundStyle(.secondary)
                        }
                        if let category = row.category {
                            Text(category).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(.vertical, Metric.fieldRowPadding)
                .overlay(alignment: .top) { hairline }
            }
            if result.nextCursor != nil {
                Text("已显示部分明细，查询结果还有更多。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, Metric.fieldRowPadding)
                    .overlay(alignment: .top) { hairline }
                Button("继续查看更多明细") { model.draft = "继续查看上一条查询的支出明细" }
                    .font(.footnote)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, Metric.fieldRowPadding)
                    .overlay(alignment: .top) { hairline }
            } else {
                fieldRow("明细页", "已全部显示（共 \(result.recordCount) 条）")
            }
        }
    }

    @ViewBuilder
    private func plainReceiptCard(
        outcome: OperationOutcome,
        state: OperationState,
        operationID: String?,
        cancellation: CancellationNote,
        toolEvidence: ToolEvidence
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            // §2b's chip, not a full-width heading. A right-aligned label spent a
            // whole line announcing 无工具调用 above two lines of reply -- on the
            // most common card in the Timeline, that is a third of its height given
            // to a tag. Same information, read as a tag instead of a title.
            terminalChip(for: outcome, toolEvidence: toolEvidence)
            switch outcome {
            case .running:
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("服务端仍在处理（\(state.wire)）").font(.callout)
                }

            // Handled by `recordedReceipt` and `queryReceiptCard` above;
            // listed only to keep the switch exhaustive, so a new outcome still
            // fails to compile here.
            case .recorded, .answeredWithQuery:
                EmptyView()

            case .answered(let text):
                Text(text)

            case .needsClarification(let question):
                Label("需要澄清", systemImage: "questionmark.circle")
                    .foregroundStyle(.pending)
                Text(question ?? "服务端没有给出问题正文；请重新描述这笔记录。")
                if let operationID, question != nil {
                    Button("回答这个问题") {
                        model.beginAnswering(
                            operationID: operationID, question: question ?? ""
                        )
                    }
                    .font(.footnote)
                }

            case .needsDuplicateDecision(let checkID, let existing):
                Label("疑似重复，尚未写入", systemImage: "doc.on.doc")
                    .foregroundStyle(.pending)
                if let existing { Text(existing) }
                field("duplicate_check_id", checkID)
                if let resolved = model.resolvedDuplicateDecisions[checkID] {
                    Label(
                        "已处理：\(duplicateDecisionText(resolved))",
                        systemImage: "checkmark.circle"
                    )
                    .foregroundStyle(.secondary)
                    Text("此提示已经完成，不能再次提交。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if let pending = model.pendingDecisions[checkID] {
                    // The choice was made but the reply never arrived. Offering
                    // the buttons again would be refused by the server — the
                    // same key under the other decision is a `409` — so the only
                    // honest actions are retrying the *same* decision or
                    // deliberately forgetting it locally.
                    Text("已提交「\(pending.decision == .dismiss ? "忽略" : "仍然记录")」，等待服务端确认。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack {
                        Button("重试同一决策") {
                            Task { await model.retryDecision(pending) }
                        }
                        Spacer()
                        Button("丢弃（不询问服务端）", role: .destructive) {
                            Task { await model.discardDecision(checkID: checkID) }
                        }
                    }
                    .font(.footnote)
                    .disabled(model.busy)
                } else {
                    HStack {
                        Button("仍然记录") { confirmingWriteAnyway = checkID }
                        Spacer()
                        Button("忽略，不记录", role: .destructive) {
                            Task { await model.decideDuplicate(checkID: checkID, decision: .dismiss) }
                        }
                    }
                    .font(.footnote)
                    .disabled(model.busy)
                }

            case .failedSafe(let reason):
                Label("未写入", systemImage: "xmark.circle")
                    .foregroundStyle(.danger)
                if let reason { field("原因", reason) }

            case .needsManualReview(let reason, let recordID):
                Label("需要人工核对：写入结果无法确认", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.pending)
                if let reason { field("原因", reason) }
                if let recordID { field("记录 ID", recordID) }
                if let operationID {
                    manualReviewResolution(operationID: operationID)
                }

            case .cancelledBeforeSubmit:
                Label("已取消，未写入", systemImage: "slash.circle")
                    .foregroundStyle(.secondary)

            case .indeterminate(let raw):
                // The honest answer: this build cannot say what happened.
                // §1i puts D1 at `danger`, not at the amber the other unresolved
                // states use: amber means "waiting for you", and this state is not
                // waiting for anything -- it is the one outcome that looks like a
                // success and must never be read as one.
                Label("本客户端无法判定结果", systemImage: "questionmark.diamond")
                    .foregroundStyle(.danger)
                field("服务端状态", raw)
                if case .unknown = toolEvidence {
                    // An event that predates tool recording carries an `answer`
                    // this client cannot trust as a clean reply (it may be the
                    // old raw query JSON). A missing fact is not "no tool".
                    Text("工具事实不可用：该历史事件未记录工具。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text("请不要当作已记录；在服务端或每日复核中确认。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if cancellation == .requestedOutcomeStillAuthoritative {
                Text("已请求取消。如果写入可能已提交，取消只是记录请求，最终结果仍由服务端判定。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(Metric.cardInset)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cardSurface)
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(borderColor(for: outcome), lineWidth: borderWidth(for: outcome))
        )
        // Same trade as the recorded card: off the face, one long-press away. The
        // `needs_manual_review` card keeps printing its 记录 ID inside the body,
        // because there the whole task is to go and look that row up in Feishu.
        //
        // Non-success cards get the explicit 查看标识符 affordance (§3c): long-press
        // is invisible to anyone who does not already know it exists, and these are
        // the cards where the id is actually needed. The card's own record id — the
        // `needs_manual_review` one — reaches the 详情单 through the same menu.
        .modifier(
            evidenceMenu(
                recordID: self.recordID(from: outcome),
                operationID: operationID,
                explicit: true
            )
        )
    }

    /// The external record id an outcome itself carries, if any. Only
    /// `needs_manual_review` names one inside the plain card; the others leave it
    /// to the menu (or have none). Kept here so the menu and the 详情单 see the
    /// same value the body already shows.
    private func recordID(from outcome: OperationOutcome) -> String? {
        if case .needsManualReview(_, let recordID) = outcome { return recordID }
        return nil
    }

    /// §1o 组件二: the terminal-state label in the card's top corner.
    ///
    /// One label set covers every outcome, so the *absence* of 账本已存在此记录 is
    /// itself legible — §3.2's rule that a state may never be promoted only works
    /// if the reader can see which rung was actually reached.
    ///
    /// `.answered` carries 无工具调用 (§2b). That case is a model reply with no tool
    /// call behind it, and on 2026-08-11 one of those read as "好的，帮你记上！" with
    /// nothing written. The card was correct to withhold a receipt, but nothing on
    /// screen contradicted the sentence; this label is that contradiction.
    private struct TerminalBadge {
        let text: String
        let color: Color
    }

    private func terminalBadge(
        for outcome: OperationOutcome, toolEvidence: ToolEvidence
    ) -> TerminalBadge? {
        switch outcome {
        // Not terminal: a badge here would name an outcome that has not happened.
        case .running:
            return nil
        case .recorded:
            // §4.5 文案降级: the system can prove only that the row exists in the
            // ledger, not that it performed a write-then-read-back comparison.
            // 「写后回读一致」 claimed a step nothing in the pipeline performs;
            // this name states the verifiable fact instead, and can be promoted
            // back only if the comparison is ever actually implemented.
            return TerminalBadge(text: "账本已存在此记录", color: .accentText)
        case .answered:
            // 无工具调用 is only claimed when the server explicitly recorded
            // `tool == null` for a direct answer. A history event that predates
            // tool recording is a missing fact, not a negative one, and an event
            // that names a tool is neither.
            switch toolEvidence {
            case .known(nil):
                return TerminalBadge(text: "无工具调用", color: .secondary)
            case .known(let tool):
                return tool.map { TerminalBadge(text: "工具：\($0)", color: .secondary) }
            case .unknown:
                return TerminalBadge(text: "工具事实不可用", color: .secondary)
            }
        case .answeredWithQuery:
            // The query card carries its own header; a badge here would compete
            // with the structured rows it renders.
            return nil
        case .needsClarification, .needsDuplicateDecision, .needsManualReview:
            return TerminalBadge(text: "待你处理", color: .pending)
        case .failedSafe:
            return TerminalBadge(text: "零副作用", color: .secondary)
        case .cancelledBeforeSubmit:
            return TerminalBadge(text: "你中止了", color: .secondary)
        case .indeterminate:
            return TerminalBadge(text: "无法判定", color: .danger)
        }
    }

    /// §3d: the terminal-state label as a **leading chip** (tiers one/two, where
    /// there is no header row). 7pt 圆角, 11.5pt/600, 22pt 高, 9pt 内边距.
    ///
    /// `toolEvidence` decides the `.answered` wording (无工具调用 vs 工具：x vs
    /// 工具事实不可用), exactly as the tier-three capsule reads the same state.
    @ViewBuilder
    private func terminalChip(
        for outcome: OperationOutcome, toolEvidence: ToolEvidence
    ) -> some View {
        if let badge = terminalBadge(for: outcome, toolEvidence: toolEvidence) {
            Text(badge.text)
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(badge.color)
                .padding(.horizontal, 9)
                .frame(height: 22)
                .background(Color.surface)
                .clipShape(RoundedRectangle(cornerRadius: 7))
        }
    }

    /// §3d: the terminal-state label as a **12pt capsule** (tier three, where the
    /// header row exists). Same colour semantics as the leading chip, so the same
    /// state reads the same in both layouts.
    @ViewBuilder
    private func terminalCapsule(
        for outcome: OperationOutcome, toolEvidence: ToolEvidence
    ) -> some View {
        if let badge = terminalBadge(for: outcome, toolEvidence: toolEvidence) {
            Text(badge.text)
                .font(.caption.weight(.semibold))
                .foregroundStyle(badge.color)
                .padding(.horizontal, 10)
                .frame(height: 24)
                .background(Color.surface)
                .clipShape(Capsule())
        }
    }

    /// §1i: 五种终态的边框与底色刻意不同重，**只有 D1 用实心红框**，而安全失败与已取消
    /// 用最轻的样式。
    ///
    /// The weighting is the message, so it is derived from the outcome rather than
    /// passed in by each call site: every card that renders `indeterminate` gets the
    /// heavy border, and no card that does not render it can accidentally acquire one.
    private func borderColor(for outcome: OperationOutcome) -> Color {
        switch outcome {
        case .indeterminate:
            return .danger
        case .needsClarification, .needsDuplicateDecision, .needsManualReview:
            return .pending
        default:
            return .cardBorder
        }
    }

    private func borderWidth(for outcome: OperationOutcome) -> CGFloat {
        switch outcome {
        case .indeterminate:
            return 2
        case .needsClarification, .needsDuplicateDecision, .needsManualReview:
            return 1
        default:
            return Metric.hairline
        }
    }

    /// §1d's follow-on action row under a receipt: 打开飞书账本 · 再记一笔 · 查本月支出.
    ///
    /// Only on `recorded`. A row that ends 「再记一笔」 under a card that did not
    /// record anything invites exactly the mistake §3.2 exists to prevent, so the
    /// actions follow the evidence rather than the intent.
    ///
    /// The two prompts fill the composer instead of sending, for the same reason
    /// §1c's chips do: neither is a complete instruction, and a tap that writes to
    /// the ledger unreviewed is the wrong default here.
    @ViewBuilder
    private var recordedActionRow: some View {
        HStack(spacing: 0) {
            if let ledgerURL = model.ledgerURL {
                Link("打开飞书账本", destination: ledgerURL)
                    .foregroundStyle(.accentText)
                    .modifier(ActionCell())
                verticalHairline
            }
            Button("再记一笔") { model.draft = "记一笔" }
                .modifier(ActionCell())
            verticalHairline
            Button("查本月支出") { model.draft = "查本月支出" }
                .modifier(ActionCell())
        }
        .font(.system(size: 14.5, weight: .medium))
    }

    /// §1d divides the actions with a rule rather than spacing them apart, so each
    /// one owns an equal share of the row and its tap target reaches the card edge.
    private struct ActionCell: ViewModifier {
        func body(content: Content) -> some View {
            content
                .frame(maxWidth: .infinity)
                .padding(.vertical, Metric.actionPadding)
                .contentShape(Rectangle())
        }
    }

    private var verticalHairline: some View {
        Rectangle()
            .fill(Color.hairlineDivider)
            .frame(width: Metric.hairline)
            .frame(maxHeight: .infinity)
    }

    private func liveCard(_ receipt: OperationReceipt) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            receiptCard(
                outcome: receipt.outcome,
                state: receipt.state,
                operationID: receipt.operationID,
                cancellation: receipt.cancellation,
                // The live receipt always carries the tool fact, even when null.
                toolEvidence: .known(receipt.tool)
            )
            if !receipt.outcome.isSettled {
                Button("请求取消", role: .destructive) {
                    Task { await model.cancelLive() }
                }
                .font(.footnote)
                .disabled(model.busy)
            }
        }
    }

    /// `DEV-040`. The human resolution path for a `needs_manual_review` card.
    ///
    /// It answers the question the state itself cannot: the system could not
    /// establish whether the row reached the ledger, and only a person looking at
    /// the ledger can. What it deliberately does **not** do is change the receipt
    /// above it — 需要人工核对 stays exactly as rendered, because that is still what
    /// the *system* proved. The resolution is shown beside it as what a person
    /// reported.
    @ViewBuilder
    private func manualReviewResolution(operationID: String) -> some View {
        if let resolved = model.resolvedManualReviews[operationID] {
            Label(
                "已人工核对：\(manualResolutionText(resolved))",
                systemImage: "checkmark.circle"
            )
            .foregroundStyle(.secondary)
            Text("这是你核对账本后的结论，不是系统验证的结果。要改判需要重新人工核对，服务端会拒绝相反的答复。")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else {
            Text("请先在飞书账本里核对这一笔（复核页有「打开飞书账本」），再选择结论。选择只记录你看到的事实，不会改动账本。")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Button("账本里有这笔") {
                    confirmingResolution = .init(
                        operationID: operationID, resolution: .confirmedWritten
                    )
                }
                Spacer()
                Button("账本里没有") {
                    confirmingResolution = .init(
                        operationID: operationID, resolution: .confirmedNotWritten
                    )
                }
            }
            .font(.footnote)
            .disabled(model.busy)
        }
    }

    private func manualResolutionText(_ wire: String) -> String {
        switch wire {
        case ManualResolution.confirmedWritten.rawValue: return "账本里有这笔"
        case ManualResolution.confirmedNotWritten.rawValue: return "账本里没有这笔"
        // A conclusion this build cannot name is still one that was recorded.
        default: return "服务端结论 \(wire)"
        }
    }

    private func duplicateDecisionText(_ wire: String) -> String {
        switch wire {
        case DuplicateDecision.writeAnyway.rawValue: return "仍然记录"
        case DuplicateDecision.dismiss.rawValue: return "忽略，不记录"
        default: return "服务端决策 \(wire)"
        }
    }

    private func unresolvedCard(_ pending: ChatTimeline.PendingSend) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("上一条消息尚未确认", systemImage: "clock.arrow.circlepath")
                .foregroundStyle(.pending)
            Text(pending.text).font(.callout)
            if let operationID = pending.operationID {
                field("operation_id", operationID)
                // `DEV-040`. The resolution is repeated here, next to the blocked
                // composer, for the same reason the card itself was moved here on
                // 2026-08-04: a `needs_manual_review` slot is exactly the case
                // where the app looks stuck, and the way out must not be a scroll
                // away. Only shown when this slot's own operation is the parked
                // one, and the action is idempotent, so seeing it in both places
                // costs nothing.
                if let receipt = model.liveReceipt,
                   receipt.operationID == operationID,
                   case .needsManualReview = receipt.outcome {
                    manualReviewResolution(operationID: operationID)
                }
            } else {
                Text("尚未拿到 operation_id：服务端可能已收到，也可能没有。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("向服务端确认") { Task { await model.resumeUnresolved() } }
                Spacer()
                Button("丢弃（不询问服务端）", role: .destructive) {
                    Task { await model.discardUnresolved() }
                }
            }
            .font(.footnote)
            .disabled(model.busy)
        }
        .padding(Metric.cardInset)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.pending.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(Color.pending.opacity(0.35), lineWidth: Metric.hairline)
        )
    }

    private func field(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .multilineTextAlignment(.leading)
        }
    }

    // --- composer -------------------------------------------------------------

    /// §3i 阶段一: whether a write is in flight, which is when the composer stops
    /// sending (but keeps typing).
    ///
    /// Three conditions count, because each means a second message could collide
    /// with the first write:
    /// - an optimistic bubble has left but the server has not echoed it yet
    ///   (`sending`);
    /// - a durable slot holds an unresolved message (`unresolved`);
    /// - a live receipt is still running.
    ///
    /// The input field stays enabled throughout (§3i); what is withheld is the
    /// *send*. This is phase one's honest middle: 可以打字、可以想, 按不下去, 且告诉
    /// 你为什么. Phase two (a normal send button plus a queue window) waits on the
    /// server's write serialisation.
    private var writingInProgress: Bool {
        model.sending != nil
            || model.unresolved != nil
            || (model.liveReceipt.map { !$0.outcome.isSettled } ?? false)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let answering = model.answering {
                HStack {
                    Text("回答：\(answering.question)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("取消") { model.cancelAnswering() }.font(.caption)
                }
            }
            if model.startNewTopic {
                HStack {
                    Text("下一条消息将开始新话题")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("取消") { model.cancelNewTopic() }
                        .font(.caption)
                }
            }
            if let pending = model.unresolved {
                // The escape from a held slot must sit next to the blocked input,
                // not at the top of the timeline where a long history hides it
                // (2026-08-04: a needs_manual_review receipt looked fully stuck
                // because the only actionable card was a screen away).
                unresolvedCard(pending)
            }
            if let photo = model.preparedPhoto,
               let preview = UIImage(data: photo.data) {
                HStack(spacing: 8) {
                    Image(uiImage: preview)
                        .resizable()
                        .scaledToFill()
                        .frame(width: 56, height: 56)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .accessibilityLabel("待发送照片")
                    Button("移除照片", role: .destructive) { model.clearPreparedPhoto() }
                        .font(.caption)
                }
            }
            HStack(spacing: 8) {
                // §3i 阶段一: 输入框不禁用。可以打字、可以想 —— 防重复记账不再靠
                // 锁住打字承担, 而是把发送挡住（见下）。灰掉输入框连起草下一句
                // 都不让, 而挡住发送已经足够安全: 同一笔不会被记两次, 因为发不出去。
                TextField("记一笔，或问一句", text: $model.draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                PhotosPicker(selection: $selectedPhoto, matching: .images) {
                    Image(systemName: "paperclip")
                        .font(.title3)
                }
                .disabled(model.busy || writingInProgress || !model.imageCapability.enabled)
                .accessibilityLabel("选择照片")
                .onChange(of: selectedPhoto) { _, item in
                    guard let item else { return }
                    Task {
                        defer { selectedPhoto = nil }
                        do {
                            guard let data = try await item.loadTransferable(type: Data.self) else {
                                model.lastError = "无法读取这张照片，请重新选择。"
                                return
                            }
                            model.preparePhoto(data)
                        } catch {
                            model.lastError = "无法读取这张照片，请重新选择。"
                        }
                    }
                }
                Button {
                    if voiceInput.isActive {
                        voiceInput.cancel()
                    }
                } label: {
                    Image(systemName: voiceInput.isActive ? "mic.fill" : "mic")
                        .font(.title3)
                        .foregroundStyle(voiceInput.isActive ? .danger : .primary)
                }
                .disabled(model.busy || writingInProgress)
                .accessibilityLabel(voiceInput.isActive ? "取消语音输入" : "长按语音输入")
                .onLongPressGesture(minimumDuration: 0.2, pressing: { holding in
                    if holding {
                        Task { await voiceInput.start() }
                    } else if voiceInput.isRecording {
                        Task {
                            model.appendVoiceDraft(await voiceInput.finish())
                        }
                    } else if voiceInput.isActive {
                        voiceInput.cancel()
                    }
                }, perform: {})
                if writingInProgress {
                    // §3i 阶段一: 写入进行中时, 发送按钮让位给一句说明。告诉用户
                    // 为什么按不下去, 而不是让他对着一个看似可点却不响应的按钮。
                    // 阶段二落地后这行被正常发送按钮取代, 排队态由队列窗口承载。
                    Text("前一笔写完再发")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .fixedSize()
                } else {
                    Button {
                        Task { await model.send() }
                    } label: {
                        if model.busy {
                            ProgressView()
                        } else {
                            Image(systemName: "arrow.up.circle.fill").font(.title2)
                        }
                    }
                    .disabled(
                        model.busy
                            || model.draft.trimmingCharacters(in: .whitespacesAndNewlines)
                                .isEmpty && model.preparedPhoto == nil
                    )
                }
            }
            if model.unresolved != nil {
                Text("上一条消息的结果还没确认。可以先打字, 但发出去要等它处理完: 否则同一笔可能被记两次。")
                    .font(.caption)
                    .foregroundStyle(.pending)
            }
            if voiceInput.isRecording {
                Text("正在本机转写；松开后可编辑再发送。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if voiceInput.isPreparing {
                Text("正在准备本机语音识别；松开或点麦克风可取消。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if let message = voiceInput.errorMessage {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.pending)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
    }
}

/// §3c: the set of identifiers one card carries, presented by the 详情单 sheet.
///
/// `Identifiable` so the sheet is keyed by the operation id (the stable handle
/// for a card) rather than by value equality. Either side can be nil — a live
/// receipt has an operation id but may have no record id yet, and a parked
/// decision has both or neither.
struct IdentifierSet: Identifiable, Equatable {
    let recordID: String?
    let operationID: String?
    var id: String { operationID ?? recordID ?? "" }
}

/// §3c 第三层: the 详情单. Every identifier in full — monospaced, unwrapped,
/// copyable one by one or all at once — for the cases that need the whole string
/// (报障 / 排查). It is deliberately a second menu item, not a replacement for
/// the direct-copy items: those are the common case, this is the exhaustive one.
struct IdentifierDetailSheet: View {
    let identifiers: IdentifierSet
    /// Reports a copied head (first 8 characters) so the card can toast it.
    let onCopied: (String) -> Void

    var body: some View {
        List {
            if let recordID = identifiers.recordID {
                Section {
                    identifierRow("外部记录 ID", recordID)
                }
            }
            if let operationID = identifiers.operationID {
                Section {
                    identifierRow("操作 ID", operationID)
                }
            }
            Section {
                Button("全部复制") {
                    copyAll()
                }
                .frame(maxWidth: .infinity)
            } footer: {
                Text("复制后会在卡片上方回显开头几个字符，便于确认复制的是哪一串。")
            }
        }
        .designSystemListSurface()
        .navigationTitle("标识符")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func identifierRow(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.footnote).foregroundStyle(.secondary)
            Text(value)
                .font(.footnote.monospaced())
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 4)
        .contextMenu {
            Button {
                UIPasteboard.general.string = value
                onCopied(String(value.prefix(8)))
            } label: {
                Label("复制", systemImage: "doc.on.doc")
            }
        }
    }

    private func copyAll() {
        let joined = [identifiers.operationID, identifiers.recordID]
            .compactMap { $0 }
            .joined(separator: "\n")
        UIPasteboard.general.string = joined
        // Report the operation id's head, the stable handle for the card.
        onCopied(String((identifiers.operationID ?? identifiers.recordID ?? "").prefix(8)))
    }
}
