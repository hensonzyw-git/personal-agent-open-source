import Foundation

/// A deliberate task selection supplies the opaque identifier. Users enter only
/// their supplemental words; the existing server recovery gate stays authoritative.
public enum DevelopmentTaskAction: String, Sendable {
    case supplement = "补充需求"
    case resume = "继续开发"
    case pause = "暂停开发"
    case cancel = "取消开发"
    case refresh = "刷新待审"

    public func command(taskID: String, text: String = "") throws -> String {
        guard taskID.range(of: #"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\z"#, options: .regularExpression) != nil else {
            throw DevelopmentActionError.invalidInput
        }
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard self == .supplement ? !body.isEmpty : body.isEmpty else { throw DevelopmentActionError.invalidInput }
        let command = rawValue + " " + taskID + (self == .supplement ? "：" + body : "")
        guard command.utf8.count <= 32768 else { throw DevelopmentActionError.invalidInput }
        return command
    }

    /// Presentation only. The unchanged original remains in the sealed event.
    public static func displayText(_ text: String) -> String {
        let pattern = #"\A(补充需求|继续开发|暂停开发|取消开发|刷新待审)\s+([A-Za-z0-9][A-Za-z0-9_.-]{0,127})(?:[：:]\s*(\S[\s\S]*))?\z"#
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let actionRange = Range(match.range(at: 1), in: text),
              let action = Self(rawValue: String(text[actionRange])) else { return text }
        let body = Range(match.range(at: 3), in: text).map { String(text[$0]) }
        guard (action == .supplement) == (body != nil) else { return text }
        return body.map { action.rawValue + "：" + $0 } ?? (action.rawValue + "（所选任务）")
    }
}

public enum DevelopmentActionError: LocalizedError {
    case invalidInput
    case busy
    public var errorDescription: String? {
        switch self {
        case .invalidInput: return "补充内容为空、过长或任务信息无效，请检查后重试。"
        case .busy: return "请先等待当前消息发送完成或处理尚未确认的发送，再操作开发任务。"
        }
    }
}
