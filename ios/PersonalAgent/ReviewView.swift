import PersonalAgentKit
import SwiftUI

/// `DEV-031`'s review screen: one card per ledger day that had Agent writes,
/// opened to show each record's *current* Feishu values.
///
/// The wording rules match the PRD's boundary for this surface. The card offers
/// exactly three actions — 确认都正确, 稍后处理, 打开飞书账本 — and none of them
/// guesses at duplicates, corrects amounts or writes anything back to Feishu.
/// ack/defer move a status the server owns; the jump opens the ledger for
/// Henson to edit there.
struct ReviewListView: View {
    @Bindable var model: ReviewModel

    var body: some View {
        List {
            if model.summaries.isEmpty && !model.busy {
                ContentUnavailableView(
                    "没有待复核的卡片",
                    systemImage: "checkmark.seal",
                    description: Text("只有 Agent 当天写入过账目才会生成复核卡。")
                )
            }
            ForEach(model.summaries) { summary in
                reviewRow(summary)
            }
            if let error = model.lastError {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.danger)
                    .listRowBackground(Color.cardSurface)
            }
        }
        .designSystemListSurface()
        .navigationTitle("复核")
        .refreshable { await model.load() }
        .task { await model.load() }
        .sheet(item: $model.openedDetail) { opened in
            NavigationStack {
                ReviewDetailView(model: model, detail: opened.detail)
            }
        }
    }

    /// §3f: one list row, and the state that row itself is in.
    ///
    /// 活性归属于被点的那一行: a row being opened shows its own liveness line
    /// (with 取消) while every other row stays tappable and scrollable. A failed
    /// open keeps the row visible and says plainly that what it shows is the
    /// written-at value, not the current one — 3f's rule that old values must not
    /// pass for checked ones. Only the row actually opening is disabled, so a
    /// second tap cannot start a second read of the *same* card.
    @ViewBuilder
    private func reviewRow(_ summary: ReviewSummary) -> some View {
        let isOpening = model.opening?.reviewID == summary.reviewID
        if let failed = model.openFailed, failed.reviewID == summary.reviewID {
            // §3f 读取失败: the row stays, marked as showing the stale value.
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    rowTitle(summary)
                    Spacer()
                    statusBadge(summary.status)
                }
                HStack(spacing: 6) {
                    Text("读不到当前值 · 显示的是写入时的值")
                        .font(.footnote)
                        .foregroundStyle(.pending)
                    Spacer()
                    Button("重试") { model.retryOpen() }
                        .font(.footnote)
                        .foregroundStyle(.accentText)
                }
                if !failed.message.isEmpty {
                    Text(failed.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .listRowBackground(Color.cardSurface)
        } else {
            Button {
                model.open(reviewID: summary.reviewID)
            } label: {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        rowTitle(summary)
                        Spacer()
                        statusBadge(summary.status)
                    }
                    if isOpening, let opening = model.opening {
                        // §3f: the liveness line *under* the row, not replacing the
                        // row's own content. Says what is actually happening — the
                        // values are being re-read from Feishu — next to a counter
                        // that only advances while the app is alive, and 取消 aborts
                        // the real request rather than hiding the line.
                        HStack(spacing: 6) {
                            Text("正在回读飞书当前值")
                            Text(
                                timerInterval: opening.since...Date.distantFuture,
                                countsDown: false
                            )
                            .tabularNumbers()
                            Spacer()
                            Button("取消") { model.cancelOpen() }
                                .foregroundStyle(.accentText)
                        }
                        .font(.footnote)
                        .foregroundStyle(.pending)
                    } else {
                        Text("\(summary.itemCount) 笔写入")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .buttonStyle(.plain)
            .foregroundStyle(.primary)
            .listRowBackground(
                // §3f: the row being read takes the 面 background so it reads as
                // active, and its chevron is gone; other rows are untouched.
                isOpening ? Color.surface : Color.cardSurface
            )
            .disabled(isOpening)
        }
    }

    private func rowTitle(_ summary: ReviewSummary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(summary.reviewDate).font(.headline)
        }
    }

    private func statusBadge(_ status: ReviewStatus) -> some View {
        let (text, color): (String, Color) = {
            switch status {
            case .pending: return ("待复核", .pending)
            case .reviewed: return ("已复核", .accentText)
            case .deferred:
                // §3h 第 4 条: 「稍后处理」是贪睡, shown as 已推迟 to tell it apart
                // from 已核 — the card is not done, it is paused until the next 0:00.
                return ("已推迟", .pending)
            case .unrecognised(let raw): return (raw, .secondary)
            }
        }()
        return Text(text)
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.15))
            .foregroundStyle(color)
            .clipShape(Capsule())
    }
}

private struct ReviewDetailView: View {
    @Bindable var model: ReviewModel
    let detail: ReviewDetail
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        List {
            Section {
                HStack(spacing: 12) {
                    if detail.summary.status.allowsReviewActions {
                        Button("确认都正确") {
                            Task {
                                await model.ack()
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        Button("稍后处理") {
                            Task {
                                await model.deferCard()
                                // The card stays open either way: the new status
                                // is the server's reply, rendered where it lands.
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                    if let url = model.ledgerURL {
                        Link("打开飞书账本", destination: url)
                            .buttonStyle(.bordered)
                    } else {
                        Text("服务端未提供账本链接")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
                .disabled(model.busy)
            } footer: {
                Text("确认和稍后都只改变复核状态，不会改动飞书记录；要改账，打开飞书账本直接改。")
            }
            .listRowBackground(Color.cardSurface)

            Section {
                ForEach(detail.items) { item in
                    itemCard(item)
                }
            } header: {
                groupHeader("当日写入（打开时的飞书当前值）")
            }
            .listRowBackground(Color.cardSurface)

            if let error = model.lastError {
                Section {
                    Text(error).foregroundStyle(.danger)
                }
                .listRowBackground(Color.cardSurface)
            }
        }
        .designSystemListSurface()
        .navigationTitle("\(detail.summary.reviewDate) 复核")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button("完成") {
                    model.closeDetail()
                    dismiss()
                }
            }
        }
    }

    @ViewBuilder
    private func itemCard(_ item: ReviewItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(item.tableKind ?? item.tool).font(.callout.weight(.medium))
                Spacer()
                Text(item.committedAt).font(.caption).foregroundStyle(.secondary)
            }
            if let unavailable = item.unavailable {
                // The row stays, with its reason: a count that quietly loses a
                // record is a review that lies about what was written.
                Label(unavailableText(unavailable), systemImage: "exclamationmark.triangle")
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
    }

    private func unavailableText(_ reason: String) -> String {
        switch reason {
        case "unknown_tool": return "服务端不认识该写入工具，无法读取当前值"
        case "source_unavailable": return "暂时无法从飞书读取当前值，请稍后重新打开"
        case "no_receipt": return "服务端没有找到这条记录的回执"
        default: return "无法读取当前值（\(reason)）"
        }
    }
}
