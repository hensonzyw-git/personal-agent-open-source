import PersonalAgentKit
import SwiftUI

struct DevelopmentWorkbench: View {
    @Bindable var model: AppModel
    @State private var tasks: [DevelopmentTask] = []
    @State private var filter = "ongoing"
    @State private var total: Int?
    @State private var complete = false
    @State private var error: String?
    @State private var loading = false
    @State private var loadID = UUID()
    @State private var roles: DevelopmentRoles?

    var body: some View {
        List {
            Section {
                Text("需求、审批和进度都可以直接在 Timeline 对话；这里集中查看开发信息。")
                    .font(.footnote).foregroundStyle(.secondary)
                Picker("范围", selection: $filter) {
                    Text("进行中").tag("ongoing")
                    Text("待处理").tag("waiting")
                    Text("全部").tag("all")
                }.pickerStyle(.segmented)
            }
            Section("当前角色配置") {
                ForEach(["planner", "coder", "reviewer"], id: \.self) { name in
                    VStack(alignment: .leading) {
                        Text(name == "planner" ? "规划" : name == "coder" ? "开发" : "审查")
                        if let role = roles?.roles[name] {
                            Text("\(role.model) · \(role.reasoning)").font(.subheadline)
                            Text("\(role.runtimeRef) / \(role.providerRef) / \(role.placementRef)").font(.caption).foregroundStyle(.secondary)
                        } else { Text("尚未配置或暂不可读取").font(.caption).foregroundStyle(.secondary) }
                    }
                }
                if roles?.available == false { Text("配置不代表已获运行准入").font(.caption).foregroundStyle(.secondary) }
            }
            if loading { ProgressView("正在读取开发信息…") }
            if let error { Text(error).foregroundStyle(.red) }
            if let total { Text(complete ? "共 \(total) 项" : "已加载 \(tasks.count) / \(total) 项，尚未完整") }
            ForEach(tasks) { task in
                NavigationLink { DevelopmentTaskView(model: model, taskID: task.taskID) } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text(task.summary)
                    Text(task.stateLabel).font(.caption).foregroundStyle(.secondary)
                    if task.summaryTruncated { Text("摘要已截短").font(.caption2).foregroundStyle(.secondary) }
                }
                }
            }
        }
        .navigationTitle("开发")
        .task(id: filter) { await load() }
        .task { roles = try? await model.developmentRoles() }
        .refreshable { await load() }
    }

    @MainActor private func load() async {
        let generation = UUID(); loadID = generation
        let selectedFilter = filter
        loading = true; error = nil; complete = false; tasks = []; total = nil
        defer { if loadID == generation { loading = false } }
        var cursor: String?
        var snapshot: String?
        var seen = Set<String>()
        do {
            repeat {
                try Task.checkCancellation()
                let page = try await model.developmentTasks(filter: selectedFilter, cursor: cursor)
                guard loadID == generation else { return }
                try Task.checkCancellation()
                guard snapshot == nil || snapshot == page.snapshot,
                      total == nil || total == page.total,
                      page.complete == (page.nextCursor == nil),
                      page.complete || (!page.items.isEmpty && page.nextCursor != cursor) else { throw AgentClientError.malformedResponse }
                for item in page.items {
                    guard seen.insert(item.id).inserted else { throw AgentClientError.malformedResponse }
                }
                snapshot = page.snapshot; total = page.total; tasks.append(contentsOf: page.items)
                guard tasks.count <= page.total, !page.complete || tasks.count == page.total else { throw AgentClientError.malformedResponse }
                cursor = page.nextCursor; complete = page.complete
            } while !complete
        } catch is CancellationError { }
        catch { if loadID == generation { self.error = "开发信息暂时无法完整读取，请下拉重试。" } }
    }
}

struct DevelopmentDocumentView: View {
    let artifactID: String
    let load: ((String) async throws -> DevelopmentDocument)?
    var reply: (() async throws -> Void)? = nil
    private func safeMarkdown(_ text: String) -> AttributedString {
        var rendered = (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
        // Text never fetches images or executes HTML. Links remain readable but
        // cannot open arbitrary provider-supplied URLs from a report.
        for run in rendered.runs { if run.link != nil { rendered[run.range].link = nil } }
        return rendered
    }
    @Environment(\.dismiss) private var dismiss
    @State private var replyError = false
    @State private var document: DevelopmentDocument?
    @State private var failed = false
    var body: some View {
        ScrollView {
            if let document, document.complete {
                // Plain text preserves all content without executing HTML,
                // remote images, scripts or automatically opening links.
                Text(safeMarkdown(document.text)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding()
            } else if failed {
                Text("文档未能完整加载，请返回后重试。").padding()
            } else { ProgressView("正在完整加载并校验文档…").padding() }
        }
        .navigationTitle("开发文档")
        .toolbar {
            if document?.complete == true, let reply {
                Button("回复此文档") {
                    Task {
                        do { try await reply(); dismiss() }
                        catch { replyError = true }
                    }
                }
            }
        }
        .alert("当前文档已不待审或回复对象过期，请返回最新待审消息。", isPresented: $replyError) {
            Button("知道了", role: .cancel) { }
        }
        .task {
            do {
                guard let load else { throw AgentClientError.unauthenticated }
                document = try await load(artifactID)
            } catch { failed = true }
        }
    }
}


struct DevelopmentTaskView: View {
    @Bindable var model: AppModel
    let taskID: String
    @State private var detail: DevelopmentTaskDetail?
    @State private var taskRoles: DevelopmentRoles?
    @State private var failed = false
    var body: some View {
        List {
            if let detail {
                Text(DevelopmentState.label(phase: detail.phase, status: detail.status))
                Text(detail.text).textSelection(.enabled)
                Section("任务与恢复") {
                    Text("任务编号：" + taskID).font(.caption).textSelection(.enabled)
                    Text("在对话中发送以下命令，可补充需求或恢复当前任务：").font(.caption)
                    Text("补充需求 \(taskID)：补充内容\n继续开发 \(taskID)\n刷新待审 \(taskID)")
                        .font(.caption.monospaced()).textSelection(.enabled)
                }
                Section("阶段") {
                    ForEach(detail.stages) { stage in
                        VStack(alignment: .leading, spacing: 4) {
                            Text("阶段 \(stage.stageID) · v\(stage.revision)")
                            Text(stage.state).font(.caption)
                            if let head = stage.headSHA { Text("代码版本：" + head).font(.caption).textSelection(.enabled) }
                            Text("验证：\(stage.verificationDigest == nil ? "尚无证据" : "已记录") · 审查：\(stage.reviewDigest == nil ? "尚无证据" : "已记录") · 提交：\(stage.commitDigest == nil ? "尚无证据" : "已记录")").font(.caption)
                        }
                    }
                }
                Section("待处理") {
                    ForEach(detail.pendingDecisions) { decision in
                        if let eventID = decision.eventID {
                            Button("返回 Timeline 处理 \(decision.kind)") {
                                model.pendingDevelopmentEventID = eventID
                                model.developmentNavigationID = UUID()
                            }
                        } else { Text("\(decision.kind)：消息同步中，请稍后刷新。") }
                    }
                }
                if let taskRoles {
                    Section("任务当前配置（来源：\(taskRoles.source ?? "未配置")）") {
                        ForEach(["planner", "coder", "reviewer"], id: \.self) { name in
                            if let role = taskRoles.roles[name] { Text("\(name)：\(role.model) · \(role.reasoning)") }
                        }
                    }
                    ForEach(taskRoles.runningSnapshots ?? []) { snapshot in
                        Section("执行中的配置") {
                            Text(snapshot.snapshotID).font(.caption)
                            ForEach(["planner", "coder", "reviewer"], id: \.self) { name in
                                if let role = snapshot.roles[name] { Text("\(name)：\(role.model) · \(role.reasoning)") }
                            }
                        }
                    }
                }
                Section("文档与证据") {
                    ForEach(detail.artifacts) { artifact in
                        NavigationLink("\(artifact.kind) · v\(artifact.revision)") {
                            DevelopmentDocumentView(artifactID: artifact.artifactID, load: { id in try await model.developmentDocument(id: id) }, reply: detail.pendingDecisions.first(where: { $0.artifactID == artifact.artifactID })?.eventID.map { eventID in
                                { model.pendingDevelopmentEventID = eventID; model.developmentNavigationID = UUID() }
                            })
                        }
                    }
                }
            } else if failed { Text("任务暂时无法读取。") }
            else { ProgressView("读取任务…") }
        }
        .navigationTitle("开发任务")
        .task {
            do { detail = try await model.developmentTask(id: taskID); taskRoles = try await model.developmentRoles(workflowID: taskID) }
            catch { failed = true }
        }
    }
}
