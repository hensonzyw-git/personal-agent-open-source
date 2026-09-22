import Foundation
import Testing
@testable import PersonalAgentKit

@Suite("Task-selected development actions")
struct DevelopmentActionTests {
    @Test func serializesSelectedTaskAndPreservesSupplement() throws {
        let command = try DevelopmentTaskAction.supplement.command(taskID: "synthetic-task", text: "只存储\n月报后置")
        #expect(command == "补充需求 synthetic-task：只存储\n月报后置")
        #expect(DevelopmentTaskAction.displayText(command) == "补充需求：只存储\n月报后置")
        #expect(try DevelopmentTaskAction.cancel.command(taskID: "synthetic-task") == "取消开发 synthetic-task")
    }
    @Test func rejectsAmbiguousOrOversizedInput() {
        for id in ["", "task:other", "task\nother", "task other", String(repeating: "a", count: 129)] {
            #expect(throws: (any Error).self) { try DevelopmentTaskAction.cancel.command(taskID: id) }
        }
        #expect(throws: (any Error).self) { try DevelopmentTaskAction.supplement.command(taskID: "task", text: "  ") }
        #expect(throws: (any Error).self) { try DevelopmentTaskAction.supplement.command(taskID: "task", text: String(repeating: "健", count: 11000)) }
        #expect(throws: (any Error).self) { try DevelopmentTaskAction.cancel.command(taskID: "task", text: "extra") }
    }
    @Test func ordinaryConversationAndQuotedCommandsAreUnchanged() {
        for text in ["聊别的话题", "他说取消开发 synthetic-task", "不要取消开发 synthetic-task", "补充需求只是一个例子"] {
            #expect(DevelopmentTaskAction.displayText(text) == text)
        }
        #expect(DevelopmentTaskAction.displayText("取消开发 synthetic-task") == "取消开发（所选任务）")
    }
}
