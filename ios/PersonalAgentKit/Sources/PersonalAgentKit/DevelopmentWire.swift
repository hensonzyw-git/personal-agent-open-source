import Foundation
import CryptoKit

public struct DevelopmentTask: Decodable, Sendable, Identifiable {
    public let taskID: String
    public let kind: String
    public let version: Int
    public let status: String
    public let phase: String
    public let summary: String
    public let summaryTruncated: Bool
    public var id: String { taskID }
    enum CodingKeys: String, CodingKey {
        case taskID = "task_id", kind, version, status, phase, summary
        case summaryTruncated = "summary_truncated"
    }
    public var stateLabel: String { DevelopmentState.label(phase: phase, status: status) }
}

public enum DevelopmentState {
    public static func label(phase: String, status: String) -> String {
        if status == "blocked" { return "遇到卡点" }
        if status == "paused" { return "已暂停" }
        if status == "cancelled" { return "已取消" }
        let names = ["clarify":"等待补充需求", "project_routing":"判断项目", "project_selection":"等待选择项目",
            "project_registration":"登记项目", "workspace_prepare":"准备工作区", "researching":"调研中",
            "prd_authoring":"撰写 PRD", "prd_waiting":"等待审核 PRD", "design_authoring":"技术设计中",
            "design_review":"审核技术设计", "coding":"开发中", "verify":"验证中", "code_review":"代码审查中",
            "stage_planning":"准备开发阶段", "delivery_publication":"发布交付成果",
            "delivery_revision_planning":"规划交付修改", "delivery_revision_review":"审核修改计划", "delivery_probe":"核验交付版本",
            "fix":"修复中", "stage_commit":"提交阶段成果", "delivery_prepare":"准备交付", "delivery_waiting":"等待验收", "accepted":"已验收"]
        guard ["active", "completed"].contains(status), let name = names[phase] else { return "未知状态（\(phase) / \(status)）" }
        return name
    }
}

public struct DevelopmentTaskPage: Decodable, Sendable {
    public let items: [DevelopmentTask]
    public let total: Int
    public let snapshot: String
    public let asOf: String
    public let complete: Bool
    public let nextCursor: String?
    enum CodingKeys: String, CodingKey { case items,total,snapshot,complete; case asOf = "as_of", nextCursor = "next_cursor" }
}

public struct DevelopmentUpdate: Sendable, Equatable {
    public let taskID: String?
    public let text: String
    public let phase: String?
    public let status: String?
    public let artifactID: String?
    public let authorizationRequired: Bool
    public init?(content: [String: JSONValue]) {
        guard content["schema_version"]?.stringValue == "dal.timeline/1.0", let text = content["text"]?.stringValue else { return nil }
        let task = content["task_id"]?.stringValue
        let haltedCommand = content["kind"]?.stringValue == "command.delivery_halted"
            && !(content["command_id"]?.stringValue ?? "").isEmpty
        guard !(task ?? "").isEmpty || haltedCommand else { return nil }
        authorizationRequired = content["authorization"]?.objectValue?["schema_version"]?.stringValue == "dal.authorization-entry/1.0"
        taskID = task; self.text = text; phase = content["phase"]?.stringValue; status = content["status"]?.stringValue
        artifactID = content["artifact"]?.objectValue?["artifact_id"]?.stringValue
    }
}

public struct DevelopmentArtifactPage: Decodable, Sendable {
    public let artifactID: String
    public let revision: Int
    public let bodySHA256: String
    public let offset: Int
    public let totalBytes: Int
    public let text: String
    public let nextOffset: Int?
    public let complete: Bool
    enum CodingKeys: String, CodingKey {
        case artifactID = "artifact_id", bodySHA256 = "body_sha256", totalBytes = "total_bytes", nextOffset = "next_offset"
        case revision, offset, text, complete
    }
}

public struct DevelopmentDocument: Sendable {
    private(set) public var text = ""
    private var bytes = Data()
    private var binding: String?
    private(set) public var complete = false
    public init() {}
    public mutating func append(_ page: DevelopmentArtifactPage) throws {
        let key = "\(page.artifactID):\(page.revision):\(page.bodySHA256):\(page.totalBytes)"
        guard !complete, page.totalBytes <= 2 * 1024 * 1024, page.offset == bytes.count,
              binding == nil || binding == key else { throw AgentClientError.malformedResponse }
        let data = Data(page.text.utf8)
        guard !data.isEmpty || page.totalBytes == 0, data.count <= 65536, bytes.count + data.count <= page.totalBytes,
              page.complete == (page.nextOffset == nil), page.complete || page.nextOffset == bytes.count + data.count else { throw AgentClientError.malformedResponse }
        var next = bytes; next.append(data)
        if page.complete {
            let hash = SHA256.hash(data: next).map { String(format: "%02x", $0) }.joined()
            guard next.count == page.totalBytes, hash == page.bodySHA256 else { throw AgentClientError.malformedResponse }
        }
        bytes = next; binding = key; complete = page.complete
        // Never label or render a partial document as complete.
        if complete { text = String(decoding: bytes, as: UTF8.self) }
    }
}

public struct DevelopmentRole: Decodable, Sendable {
    public let runtimeRef: String
    public let providerRef: String
    public let model: String
    public let reasoning: String
    public let placementRef: String
    public let permission: String
    public let billing: String
    enum CodingKeys: String, CodingKey {
        case runtimeRef = "runtime_ref", providerRef = "provider_ref", placementRef = "placement_ref"
        case model,reasoning,permission,billing
    }
}

public struct DevelopmentRoles: Decodable, Sendable {
    public let roles: [String: DevelopmentRole]
    public let available: Bool
    public let reason: String?
    public let source: String?
    public let runningSnapshots: [DevelopmentRunningSnapshot]?
    enum CodingKeys: String, CodingKey { case roles,available,reason,source; case runningSnapshots = "running_snapshots" }
}

public struct DevelopmentRunningSnapshot: Decodable, Sendable, Identifiable {
    public let stepID: String
    public let snapshotID: String
    public let digest: String
    public let roles: [String: DevelopmentRole]
    public var id: String { stepID }
    enum CodingKeys: String, CodingKey { case stepID = "step_id", snapshotID = "snapshot_id", digest,roles }
}

public struct DevelopmentArtifactReference: Decodable, Sendable, Identifiable {
    public let artifactID: String
    public let kind: String
    public let revision: Int
    public var id: String { artifactID }
    enum CodingKeys: String, CodingKey { case artifactID = "artifact_id", kind,revision }
}

public struct DevelopmentTaskDetail: Decodable, Sendable {
    public let status: String
    public let phase: String
    public let text: String
    public let artifacts: [DevelopmentArtifactReference]
    public let stages: [DevelopmentStage]
    public let pendingDecisions: [DevelopmentPendingDecision]
    enum CodingKeys: String, CodingKey { case status,phase,text,artifacts,stages; case pendingDecisions = "pending_decisions" }
}

public struct DevelopmentStage: Decodable, Sendable, Identifiable {
    public let stageID: String
    public let revision: Int
    public let state: String
    public let stateVersion: Int
    public let baseSHA: String?
    public let headSHA: String?
    public let treeSHA: String?
    public let verificationDigest: String?
    public let reviewDigest: String?
    public let commitDigest: String?
    public var id: String { "\(stageID):\(revision)" }
    enum CodingKeys: String, CodingKey {
        case stageID = "stage_id", stateVersion = "state_version", revision,state
        case baseSHA = "base_sha", headSHA = "head_sha", treeSHA = "tree_sha"
        case verificationDigest = "verification_digest", reviewDigest = "review_digest", commitDigest = "commit_digest"
    }
}

public struct DevelopmentPendingDecision: Decodable, Sendable, Identifiable {
    public let decisionID: String
    public let kind: String
    public let artifactID: String
    public let eventID: String?
    public let expiresAt: String
    public var id: String { decisionID }
    enum CodingKeys: String, CodingKey { case decisionID = "decision_id", artifactID = "artifact_id", eventID = "event_id", expiresAt = "expires_at", kind }
}

public struct DevelopmentReplyContext: Codable, Sendable, Equatable {
    public let eventID: String
    public let token: String
    enum CodingKeys: String, CodingKey { case eventID = "event_id", token }
    public init(eventID: String, token: String) { self.eventID = eventID; self.token = token }
}


public struct DevelopmentNotification: Decodable, Sendable {
    public let items: [DevelopmentNotificationItem]
}

public struct DevelopmentNotificationItem: Decodable, Sendable, Identifiable {
    public let eventID: String
    public let text: String
    public let current: Bool
    public var id: String { eventID }
    enum CodingKeys: String, CodingKey { case eventID = "event_id", text,current }
}
