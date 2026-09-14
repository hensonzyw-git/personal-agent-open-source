import XCTest
@testable import PersonalAgentKit

final class ResultEnvelopeV2Tests: XCTestCase {
    func testSharedServerVectors() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("src/personal_agent/api/vectors/result_envelope_v2.json"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let cases = try XCTUnwrap(object["cases"] as? [[String: Any]])
        for item in cases {
            let bytes = try JSONSerialization.data(withJSONObject: XCTUnwrap(item["response"]))
            let envelope = try JSONDecoder().decode(ResultEnvelope.self, from: bytes)
            XCTAssertEqual(envelope.version, 2)
            if item["name"] as? String == "comparison" {
                XCTAssertTrue(envelope.text.contains("增加 20 元"))
                XCTAssertEqual(envelope.analysisNodes?.first?.text, envelope.text)
            }
        }
    }
    func testRejectUnknownVersion() {
        let data = Data(#"{"version":3,"kind":"conversation","task_status":"completed","text":"x","evidence":[]}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(ResultEnvelope.self, from: data))
    }
}
