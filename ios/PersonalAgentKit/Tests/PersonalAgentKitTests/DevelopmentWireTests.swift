import Foundation
import Testing
import CryptoKit
@testable import PersonalAgentKit

@Suite("Development Timeline contracts")
struct DevelopmentWireTests {
    @Test func haltedCommandDisplaysBeforeTaskIdentityIsKnown() throws {
        let data = try JSONSerialization.data(withJSONObject: ["schema_version":"dal.timeline/1.0", "kind":"command.delivery_halted", "command_id":"synthetic-command", "text":"远端结果仍未知"])
        var content = try JSONDecoder().decode([String:JSONValue].self, from:data)
        let update = try #require(DevelopmentUpdate(content:content))
        #expect(update.taskID == nil && update.text == "远端结果仍未知")
        content.removeValue(forKey:"command_id")
        #expect(DevelopmentUpdate(content:content) == nil)
    }
    @Test func unknownStateStaysUnknown() {
        #expect(DevelopmentState.label(phase: "future", status: "active").contains("未知"))
        #expect(DevelopmentState.label(phase: "accepted", status: "completed") == "已验收")
    }
    @Test func incompleteOrCorruptedDocumentCannotLookComplete() throws {
        let content = "合成 PRD"; let sha = SHA256.hash(data: Data(content.utf8)).map { String(format:"%02x",$0) }.joined()
        let data = try JSONSerialization.data(withJSONObject: ["artifact_id":"a","revision":1,"body_sha256":sha,"offset":0,"total_bytes":content.utf8.count,"text":content,"next_offset":NSNull(),"complete":true])
        var document = DevelopmentDocument()
        try document.append(JSONDecoder().decode(DevelopmentArtifactPage.self,from:data))
        #expect(document.complete && document.text == content)
        let corrupted = try JSONSerialization.data(withJSONObject: ["artifact_id":"a","revision":1,"body_sha256":String(repeating:"0",count:64),"offset":0,"total_bytes":content.utf8.count,"text":content,"next_offset":NSNull(),"complete":true])
        var refused = DevelopmentDocument()
        #expect(throws: (any Error).self) { try refused.append(JSONDecoder().decode(DevelopmentArtifactPage.self,from:corrupted)) }
        #expect(!refused.complete && refused.text.isEmpty)
    }
    @Test func timelineDevelopmentEventRemainsVisible() throws {
        let data = Data("{\"schema_version\":\"dal.timeline/1.0\",\"task_id\":\"task\",\"text\":\"等待审核\"}".utf8)
        let content = try JSONDecoder().decode([String:JSONValue].self,from:data)
        #expect(DevelopmentUpdate(content:content)?.text == "等待审核")
    }
}

@Suite("Shared development wire vectors")
struct SharedDevelopmentWireTests {
    @Test func sharedDocumentAndReplyContextSurviveRecovery() throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { root.deleteLastPathComponent() }
        let data = try Data(contentsOf: root.appendingPathComponent("tests/fixtures/dal_timeline.synthetic.json"))
        let vector = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let decoder = JSONDecoder()
        let page = try decoder.decode(DevelopmentArtifactPage.self, from: JSONSerialization.data(withJSONObject: vector["document"]!))
        var document = DevelopmentDocument()
        try document.append(page)
        #expect(document.complete)
        let reply = try decoder.decode(DevelopmentReplyContext.self, from: JSONSerialization.data(withJSONObject: vector["reply_context"]!))
        let pending = ChatTimeline.PendingSend(idempotencyKey: "synthetic-key", conversationID: "synthetic-timeline", text: "通过",
            clarificationOf: nil, dalReplyContext: reply)
        #expect(try decoder.decode(ChatTimeline.PendingSend.self, from: JSONEncoder().encode(pending)) == pending)
        let content = try decoder.decode([String: JSONValue].self, from: JSONSerialization.data(withJSONObject: vector["update"]!))
        #expect(DevelopmentUpdate(content: content)?.artifactID == page.artifactID)
    }
}

@Suite("Commit review accounting")
struct CommitReviewAccountingTests {
    @Test func oldStageKeepsUnknownReviewMetadata() throws {
        let data = Data(#"{"stage_id":"synthetic","revision":1,"state":"reviewing","state_version":4}"#.utf8)
        let stage = try JSONDecoder().decode(DevelopmentStage.self, from: data)
        #expect(stage.commitSubject == nil && stage.reviewSummary == nil)
    }
    @Test func apiRequestsAreDistinctFromReviewCounts() throws {
        let data = Data(#"{"stage_id":"synthetic","revision":1,"state":"fixing","state_version":4,"commit_subject":"One complete feature","review_summary":{"unit_id":"synthetic:1","initial_reviews":1,"incremental_reviews":2,"execution_attempts":4,"incomplete_attempts":1,"provider_requests":null}}"#.utf8)
        let stage = try JSONDecoder().decode(DevelopmentStage.self, from: data)
        #expect(stage.reviewSummary?.initialReviews == 1)
        #expect(stage.reviewSummary?.incrementalReviews == 2)
        #expect(stage.reviewSummary?.providerRequests == nil)
        #expect(stage.commitSubject == "One complete feature")
    }
}
