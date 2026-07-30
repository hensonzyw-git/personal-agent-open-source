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

    var body: some View {
        VStack(spacing: 0) {
            timeline
            Divider()
            composer
        }
        .navigationTitle("对话")
        .navigationBarTitleDisplayMode(.inline)
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

                    if let pending = model.unresolved {
                        unresolvedCard(pending)
                    }

                    ForEach(model.events) { event in
                        entry(event).id(event.eventID)
                    }

                    if let receipt = model.liveReceipt, !receipt.outcome.isSettled {
                        liveCard(receipt)
                    }

                    if let error = model.lastError {
                        Text(error)
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding()
            }
            .refreshable { await model.refresh() }
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
                    .padding(10)
                    .background(Color.accentColor.opacity(0.15))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
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
            switch outcome {
            case .running:
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("服务端仍在处理（\(state.wire)）").font(.callout)
                }

            case .recorded(let recordID, let tool):
                Label("已写入飞书账本", systemImage: "checkmark.seal")
                    .foregroundStyle(.green)
                field("记录 ID", recordID)
                if let tool { field("工具", tool) }

            case .answered(let text):
                Text(text)

            case .needsClarification(let question):
                Label("需要澄清", systemImage: "questionmark.circle")
                    .foregroundStyle(.orange)
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
                    .foregroundStyle(.orange)
                if let existing { Text(existing) }
                field("duplicate_check_id", checkID)
                if let pending = model.pendingDecisions[checkID] {
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
                    .foregroundStyle(.red)
                if let reason { field("原因", reason) }

            case .needsManualReview(let reason, let recordID):
                Label("需要人工核对：写入结果无法确认", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                if let reason { field("原因", reason) }
                if let recordID { field("记录 ID", recordID) }

            case .cancelledBeforeSubmit:
                Label("已取消，未写入", systemImage: "slash.circle")
                    .foregroundStyle(.secondary)

            case .indeterminate(let raw):
                // The honest answer: this build cannot say what happened.
                Label("本客户端无法判定结果", systemImage: "questionmark.diamond")
                    .foregroundStyle(.orange)
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
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 12))
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

    private func unresolvedCard(_ pending: ChatTimeline.PendingSend) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("上一条消息尚未确认", systemImage: "clock.arrow.circlepath")
                .foregroundStyle(.orange)
            Text(pending.text).font(.callout)
            if let operationID = pending.operationID {
                field("operation_id", operationID)
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
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: 12))
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
                Text("先处理上面那条未确认的消息，再发新的：否则同一笔可能被记两次。")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
    }
}
