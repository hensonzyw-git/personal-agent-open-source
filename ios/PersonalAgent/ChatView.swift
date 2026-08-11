import PersonalAgentKit
import SwiftUI

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
    /// The check awaiting the user's 仍然记录 confirmation. `write_anyway`
    /// forces a write past the duplicate gate, so it is never one tap.
    @State private var confirmingWriteAnyway: String?
    /// `DEV-040`. The manual-review conclusion awaiting confirmation. Also never
    /// one tap: the server records the first answer and refuses a contradicting
    /// one, so a mis-tap cannot be corrected in the app.
    @State private var confirmingResolution: ResolutionIntent?

    struct ResolutionIntent: Equatable {
        let operationID: String
        let resolution: ManualResolution
    }

    var body: some View {
        VStack(spacing: 0) {
            timeline
            Divider()
            composer
        }
        .background(Color.screenBackground)
        // No title of its own: §1a makes the Timeline the whole surface, so the
        // navigation bar belongs to the app rather than to this view. `RootView`
        // sets it, together with the status entry.
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
                    .clipShape(RoundedRectangle(cornerRadius: Metric.bubbleRadius))
                if clarificationOf != nil {
                    Text("补充澄清").font(.caption2).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)

        case .operationResult(let outcome, let state):
            receiptCard(
                outcome: outcome,
                state: state,
                operationID: event.operationID,
                cancellation: .none
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

        case .manualReviewResolved(let resolution):
            Label(
                "已人工核对：\(manualResolutionText(resolution))",
                systemImage: "person.crop.circle.badge.checkmark"
            )
            .font(.caption)
            .foregroundStyle(.secondary)

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

    // --- receipts -------------------------------------------------------------

    @ViewBuilder
    private func receiptCard(
        outcome: OperationOutcome,
        state: OperationState,
        operationID: String?,
        cancellation: CancellationNote
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let badge = terminalBadge(for: outcome) {
                HStack {
                    Spacer(minLength: 0)
                    Text(badge.text)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(badge.color)
                }
            }
            switch outcome {
            case .running:
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("服务端仍在处理（\(state.wire)）").font(.callout)
                }

            case .recorded(let recordID, let tool):
                Label("已写入飞书账本", systemImage: "checkmark.seal")
                    .foregroundStyle(.accentText)
                field("记录 ID", recordID)
                if let tool { field("工具", tool) }

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
                Text("请不要当作已记录；在服务端或每日复核中确认。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if cancellation == .requestedOutcomeStillAuthoritative {
                Text("已请求取消。如果写入可能已提交，取消只是记录请求，最终结果仍由服务端判定。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let operationID {
                field("operation_id", operationID)
            }
        }
        .padding(Metric.cardPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cardSurface)
        .clipShape(RoundedRectangle(cornerRadius: Metric.cardRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Metric.cardRadius)
                .strokeBorder(borderColor(for: outcome), lineWidth: borderWidth(for: outcome))
        )
    }

    /// §1o 组件二: the terminal-state label in the card's top corner.
    ///
    /// One label set covers every outcome, so the *absence* of 写后回读一致 is itself
    /// legible — §3.2's rule that a state may never be promoted only works if the
    /// reader can see which rung was actually reached.
    ///
    /// `.answered` carries 无工具调用 (§2b). That case is a model reply with no tool
    /// call behind it, and on 2026-08-11 one of those read as "好的，帮你记上！" with
    /// nothing written. The card was correct to withhold a receipt, but nothing on
    /// screen contradicted the sentence; this label is that contradiction.
    private struct TerminalBadge {
        let text: String
        let color: Color
    }

    private func terminalBadge(for outcome: OperationOutcome) -> TerminalBadge? {
        switch outcome {
        // Not terminal: a badge here would name an outcome that has not happened.
        case .running:
            return nil
        case .recorded:
            return TerminalBadge(text: "写后回读一致", color: .accentText)
        case .answered:
            return TerminalBadge(text: "无工具调用", color: .secondary)
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
            return .ink.opacity(0.12)
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

    private func liveCard(_ receipt: OperationReceipt) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            receiptCard(
                outcome: receipt.outcome,
                state: receipt.state,
                operationID: receipt.operationID,
                cancellation: receipt.cancellation
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
        .padding(Metric.cardPadding)
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
            if let pending = model.unresolved {
                // The escape from a held slot must sit next to the blocked input,
                // not at the top of the timeline where a long history hides it
                // (2026-08-04: a needs_manual_review receipt looked fully stuck
                // because the only actionable card was a screen away).
                unresolvedCard(pending)
            }
            HStack(spacing: 8) {
                TextField("记一笔，或问一句", text: $model.draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .disabled(model.unresolved != nil)
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
                        || model.unresolved != nil
                        || model.draft.trimmingCharacters(in: .whitespacesAndNewlines)
                            .isEmpty
                )
            }
            if model.unresolved != nil {
                Text("先处理上面那张卡片里的未确认消息，再发新的：否则同一笔可能被记两次。")
                    .font(.caption)
                    .foregroundStyle(.pending)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
    }
}
