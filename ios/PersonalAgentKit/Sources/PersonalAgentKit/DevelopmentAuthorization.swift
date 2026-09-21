import Foundation
import CryptoKit

private func authObject(_ value: JSONValue?, keys: Set<String>) throws -> [String: JSONValue] {
    guard case .object(let body) = value, Set(body.keys) == keys else { throw AgentClientError.malformedResponse }
    return body
}
private func authString(_ body: [String: JSONValue], _ key: String) throws -> String {
    guard case .string(let value) = body[key], !value.isEmpty else { throw AgentClientError.malformedResponse }
    return value
}
private func authInteger(_ body: [String: JSONValue], _ key: String, min: Int = 0) throws -> Int {
    guard case .number(let n) = body[key], n.isFinite, n.rounded() == n, n >= Double(min), n <= Double(Int32.max) else { throw AgentClientError.malformedResponse }
    return Int(n)
}
private func authBool(_ body: [String: JSONValue], _ key: String) throws -> Bool {
    guard case .bool(let b) = body[key] else { throw AgentClientError.malformedResponse }; return b
}
public func developmentAuthorizationDigest(_ body: [String: JSONValue]) throws -> String {
    let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return SHA256.hash(data: try encoder.encode(body)).map { String(format: "%02x", $0) }.joined()
}

public struct DevelopmentCommandStatus: Decodable, Sendable {
    public let commandID: String
    public let status: String
    public let receipt: [String: JSONValue]?
    public let halted: Bool
    public let attempted: Bool
    public let error: String?
    public var terminal: Bool { ["accepted", "refused", "cancelled"].contains(status) }
    public var message: String {
        if status == "accepted" { return "操作已接纳，正在核对任务状态。" }
        if status == "refused" { return "操作未被接纳，请重新核对授权范围。" }
        if status == "cancelled" { return "此操作未派发，已取消；开发任务未因此取消。" }
        if halted { return attempted ? "投递已停止，远端结果待核实。" : "投递已停止，尚未派发。" }
        return attempted ? "已提交，结果待确认。" : "等待提交。"
    }
    public init(from decoder: Decoder) throws {
        let b = try [String: JSONValue](from: decoder)
        guard Set(b.keys) == ["schema_version", "command_id", "status", "receipt", "delivery_halted", "delivery_error", "dispatch_attempted"],
              b["schema_version"] == .string("dal.command-status/1.0") else { throw AgentClientError.malformedResponse }
        commandID = try authString(b, "command_id"); status = try authString(b, "status")
        halted = try authBool(b, "delivery_halted"); attempted = try authBool(b, "dispatch_attempted")
        switch b["delivery_error"] { case .null: error = nil; case .string(let e): error = e; default: throw AgentClientError.malformedResponse }
        guard error == nil || ["INPUT_INTEGRITY_FAILED", "IDENTITY_REVOKED", "RETRY_EXHAUSTED", "RESPONSE_INVALID"].contains(error!), halted == (error != nil) else { throw AgentClientError.malformedResponse }
        switch b["receipt"] { case .null: receipt = nil; case .object(let r): receipt = r; default: throw AgentClientError.malformedResponse }
        switch status {
        case "queued": guard !attempted, receipt == nil, error == nil || error == "INPUT_INTEGRITY_FAILED" else { throw AgentClientError.malformedResponse }
        case "cancelled": guard !attempted, receipt == nil, error == "IDENTITY_REVOKED" else { throw AgentClientError.malformedResponse }
        case "delivery_unknown": guard attempted, receipt == nil else { throw AgentClientError.malformedResponse }
        case "accepted", "refused": guard attempted, !halted, receipt?["command_id"] == .string(commandID), receipt?["status"] == .string(status) else { throw AgentClientError.malformedResponse }
        default: throw AgentClientError.malformedResponse
        }
    }
}

public struct DevelopmentAuthorizationProject: Sendable, Identifiable {
    public let value: [String: JSONValue]
    public let id: String
    public let name: String
    public let actions: [String]
    public let maxSeconds: Int
    public let maxValidity: Int
    public let revision: Int
    public let digest: String
    init(_ raw: JSONValue) throws {
        value = try authObject(raw, keys: ["project_id","revision","display_name","kind","root","remote_repository","allowed_actions","registration_policies","max_budget_seconds","max_validity_seconds","worker_id","worker_configuration_digest","directory_identity_digest","base_sha","base_branch","budget_policy_ref","template_digest"])
        id = try authString(value, "project_id"); name = try authString(value, "display_name")
        revision = try authInteger(value, "revision", min: 1); digest = try authString(value, "template_digest")
        maxSeconds = try authInteger(value, "max_budget_seconds", min: 1); maxValidity = try authInteger(value, "max_validity_seconds", min: 1)
        guard case .array(let list) = value["allowed_actions"] else { throw AgentClientError.malformedResponse }
        actions = try list.map { item in guard case .string(let a) = item, ["read","write","create","local_init","remote_issue","push","pr"].contains(a) else { throw AgentClientError.malformedResponse }; return a }
        guard Set(actions).count == actions.count, actions.contains("read"), maxSeconds <= 86400, maxValidity <= 31536000, actions.contains("push") == actions.contains("pr") else { throw AgentClientError.malformedResponse }
    }
}

public struct DevelopmentAuthorizationProposal: Sendable {
    public let value: [String: JSONValue]
    public let id: String
    public let digest: String
    public let scope: [String: JSONValue]
    public let status: String
    init(_ raw: JSONValue) throws {
        value = try authObject(raw, keys: ["proposal_id","revision","binding","binding_digest","scope","expires_at","status"])
        id = try authString(value, "proposal_id"); digest = try authString(value, "binding_digest"); status = try authString(value, "status")
        let binding = try authObject(value["binding"], keys: ["schema","request_id","workflow_id","request_version","workflow_version","gate_version","gate_epoch","authorization_generation","operation","expected_grant","proposal_id","revision","project_id","template_revision","template_digest","scope_digest","worker_configuration_digest","policy_digest","expires_at"])
        scope = try authObject(value["scope"], keys: ["project_id","subject","root","kind","display_name","actions","budget_seconds","expires_at","registration_policy","remote_repository","base_sha","base_branch","branch","template_digest","budget_policy_ref"])
        guard ["pending","granted","superseded","expired"].contains(status), binding["proposal_id"] == .string(id),
              binding["schema"] == .string("dal.project-authorization-binding/1.0"),
              try developmentAuthorizationDigest(binding) == digest,
              try developmentAuthorizationDigest(scope) == authString(binding, "scope_digest") else { throw AgentClientError.malformedResponse }
    }
}

public struct DevelopmentAuthorizationSnapshot: Decodable, Sendable {
    public let requestID: String
    public let expected: [String: JSONValue]
    public let projects: [DevelopmentAuthorizationProject]
    public let proposal: DevelopmentAuthorizationProposal?
    public let grants: [[String: JSONValue]]
    public let nextCursor: String?
    public let snapshot: String
    public let total: Int
    public init(from decoder: Decoder) throws {
        let b = try [String: JSONValue](from: decoder)
        guard Set(b.keys) == ["schema_version","request_id","workflow_id","expected","current_proposal","grants","candidates","next_cursor","complete","total","snapshot","status","phase","reason"], b["schema_version"] == .string("dal.project-authorization/1.0") else { throw AgentClientError.malformedResponse }
        requestID = try authString(b, "request_id"); snapshot = try authString(b, "snapshot"); total = try authInteger(b, "total")
        expected = try authObject(b["expected"], keys: ["request_version","workflow_version","gate_version","gate_epoch","generation"])
        for key in expected.keys { _ = try authInteger(expected, key, min: key == "generation" ? 0 : 1) }
        guard case .array(let rows) = b["candidates"], case .array(let g) = b["grants"] else { throw AgentClientError.malformedResponse }
        projects = try rows.map(DevelopmentAuthorizationProject.init)
        grants = try g.map {
            let grant = try authObject($0, keys: ["grant_id","version","digest","expires_at","revoked","source","scope"])
            _ = try authString(grant, "grant_id"); _ = try authString(grant, "digest")
            _ = try authInteger(grant, "version", min: 1); _ = try authBool(grant, "revoked")
            let scope = try authObject(grant["scope"], keys: ["project_id","actions","budget_seconds","expires_at"])
            _ = try authString(scope, "project_id"); _ = try authString(scope, "expires_at")
            guard try authInteger(scope, "budget_seconds", min: 1) <= 86400,
                  case .array(let actions) = scope["actions"], !actions.isEmpty,
                  actions.allSatisfy({ if case .string(let a) = $0 { return ["read","write","create","local_init","remote_issue","push","pr"].contains(a) }; return false }) else { throw AgentClientError.malformedResponse }
            return grant
        }
        if b["current_proposal"] == .null { proposal = nil } else { proposal = try DevelopmentAuthorizationProposal(b["current_proposal"]!) }
        switch b["next_cursor"] { case .null: nextCursor = nil; case .string(let c): nextCursor = c; default: throw AgentClientError.malformedResponse }
        guard try authBool(b, "complete") == (nextCursor == nil) else { throw AgentClientError.malformedResponse }
    }
}

public struct DevelopmentAuthorizationContext: Decodable, Sendable {
    public let proposalID: String
    public let token: String?
    public let commandID: String?
    public init(from decoder: Decoder) throws {
        let b = try [String: JSONValue](from: decoder)
        guard Set(b.keys) == ["proposal_id", "token", "command_id"] else { throw AgentClientError.malformedResponse }
        proposalID = try authString(b, "proposal_id")
        switch b["token"] { case .null: token = nil; case .string(let v) where !v.isEmpty: token = v; default: throw AgentClientError.malformedResponse }
        switch b["command_id"] { case .null: commandID = nil; case .string(let v) where !v.isEmpty: commandID = v; default: throw AgentClientError.malformedResponse }
        guard (token == nil) != (commandID == nil) else { throw AgentClientError.malformedResponse }
    }
}

public struct PendingDevelopmentAuthorization: Codable, Sendable {
    public let commandID: String
    public let target: String
    public let kind: String
    public let body: [String: JSONValue]
    public let requestID: String
    public let deviceID: String
    public let baseURL: String
    public let keyThumbprint: String
}
