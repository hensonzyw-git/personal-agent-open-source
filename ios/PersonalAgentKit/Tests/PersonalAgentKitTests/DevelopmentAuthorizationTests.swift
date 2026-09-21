import Foundation
import Testing
@testable import PersonalAgentKit

@Suite("Phone project authorization boundaries")
struct DevelopmentAuthorizationTests {
    private func decode(_ body: [String: Any]) throws -> DevelopmentCommandStatus {
        try JSONDecoder().decode(DevelopmentCommandStatus.self, from: JSONSerialization.data(withJSONObject: body))
    }
    @Test func fastAPICodesRemainActionableWithoutTrustingFreeText() {
        #expect(AgentClientError.from(status:404,body:Data("{\"detail\":\"COMMAND_NOT_FOUND\"}".utf8)).errorCode == "COMMAND_NOT_FOUND")
        #expect(AgentClientError.from(status:400,body:Data("{\"detail\":\"CONFIRMATION_EXPIRED_NOT_QUEUED\"}".utf8)).errorCode == "CONFIRMATION_EXPIRED_NOT_QUEUED")
        #expect(AgentClientError.from(status:400,body:Data("{\"detail\":\"Some human text\"}".utf8)).errorCode == nil)
    }
    @Test func haltedUnknownNeverBecomesTerminal() throws {
        let result = try decode(["schema_version":"dal.command-status/1.0", "command_id":"synthetic", "status":"delivery_unknown", "receipt":NSNull(), "delivery_halted":true, "delivery_error":"IDENTITY_REVOKED", "dispatch_attempted":true])
        #expect(!result.terminal && result.halted && result.attempted)
    }
    @Test func impossibleStatesAreRejected() throws {
        let base: [String:Any] = ["schema_version":"dal.command-status/1.0", "command_id":"synthetic", "status":"queued", "receipt":NSNull(), "delivery_halted":false, "delivery_error":NSNull(), "dispatch_attempted":false]
        for changes: [String:Any] in [["status":"future"], ["dispatch_attempted":true], ["delivery_halted":true], ["extra":"value"], ["status":"accepted"], ["status":"cancelled"]] {
            #expect(throws: (any Error).self) { try decode(base.merging(changes) { _, new in new }) }
        }
    }
    @Test func contextHasExactlyOneOutcomeAndNoUnknownFields() throws {
        for body: [String:Any] in [
            ["proposal_id":"p","token":NSNull(),"command_id":NSNull()],
            ["proposal_id":"p","token":"t","command_id":"c"],
            ["proposal_id":"p","token":"t","command_id":NSNull(),"extra":true]
        ] {
            #expect(throws: (any Error).self) { try JSONDecoder().decode(DevelopmentAuthorizationContext.self, from: JSONSerialization.data(withJSONObject:body)) }
        }
    }
    @Test func sharedWireVectorsMatchServer() throws {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { root.deleteLastPathComponent() }
        let data = try Data(contentsOf: root.appendingPathComponent("tests/fixtures/dal_phone_authorization.synthetic.json"))
        let fixture = try JSONSerialization.jsonObject(with:data) as! [String:Any]
        let body = try JSONDecoder().decode([String:JSONValue].self, from: JSONSerialization.data(withJSONObject:fixture["canonical_body"]!))
        #expect(try developmentAuthorizationDigest(body) == fixture["canonical_sha256"] as? String)
        for item in fixture["command_states"] as! [[String:Any]] {
            let state = try decode(item["wire"] as! [String:Any])
            #expect(state.terminal == item["terminal"] as! Bool)
        }
    }
}
