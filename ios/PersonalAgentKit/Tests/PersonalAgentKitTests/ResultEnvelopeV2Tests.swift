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
                XCTAssertEqual(envelope.analysisNodes?.first?.differenceDecimal, "20")
                XCTAssertEqual(envelope.analysisNodes?.first?.current?.valueDecimal, "120")
                XCTAssertEqual(envelope.analysisNodes?.first?.baseline?.valueDecimal, "100")
                XCTAssertEqual(envelope.analysisNodes?.first?.text, envelope.text)
            }
        }
    }
    func testRejectUnknownVersion() {
        let data = Data(#"{"version":3,"kind":"conversation","task_status":"completed","text":"x","evidence":[]}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(ResultEnvelope.self, from: data))
    }

    func testBusinessHistoryKeepsReceiptAndDuplicateDecision() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("src/personal_agent/api/vectors/result_envelope_v2.json"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let cases = try XCTUnwrap(object["history_cases"] as? [[String: Any]])
        for item in cases {
            let bytes = try JSONSerialization.data(withJSONObject: XCTUnwrap(item["content"]))
            let content = try JSONDecoder().decode([String: JSONValue].self, from: bytes)
            let event = TimelineEvent(eventID: "synthetic", eventType: "operation_result",
                                      operationID: "synthetic", createdAt: "2026-09-14", content: content)
            guard case .operationResult(let outcome, _, let tool) = event.kind else {
                XCTFail("Business history did not produce a receipt"); continue
            }
            XCTAssertEqual(tool, .known("finance.log_income"))
            switch outcome {
            case .recorded(let recordID, let tool, _):
                XCTAssertEqual(item["outcome"] as? String, "recorded")
                XCTAssertEqual(recordID, "synthetic-record")
                XCTAssertEqual(tool, "finance.log_income")
            case .needsDuplicateDecision(let checkID, _):
                XCTAssertEqual(item["outcome"] as? String, "needsDuplicateDecision")
                XCTAssertEqual(checkID, "dup-history")
            default:
                XCTFail("Business history lost its actionable receipt: \(outcome)")
            }
        }
    }
    func testMalformedEnvelopeDoesNotLoseReceiptOrTurnIntoRecordedSuccess() throws {
        for bad in ["[]", "42", "{}", #"{"version":3,"kind":"action","task_status":"completed","text":"x","evidence":[]}"#] {
            let raw = """
            {"operation_id":"synthetic","state":"succeeded","cancel_requested":false,"client_detached":false,
             "tool":"finance.log_income","record_id":"synthetic-record","result_envelope":\(bad)}
            """
            let receipt = try JSONDecoder().decode(OperationReceipt.self, from: Data(raw.utf8))
            XCTAssertEqual(receipt.operationID, "synthetic")
            XCTAssertEqual(receipt.resultEnvelope, .unavailable)
            guard case .answeredV2(let envelope) = receipt.outcome else {
                XCTFail("Malformed v2 envelope became business success"); continue
            }
            XCTAssertEqual(envelope.kind, "limitation")
            let content = try JSONDecoder().decode([String: JSONValue].self, from: Data(raw.utf8))
            let event = TimelineEvent(eventID: "bad", eventType: "operation_result",
                                      operationID: "synthetic", createdAt: "2026-09-14", content: content)
            guard case .operationResult(let outcome, _, _) = event.kind,
                  case .answeredV2(let history) = outcome else {
                XCTFail("Malformed history lost its limitation"); continue
            }
            XCTAssertEqual(history, .unavailable)
        }
    }

}
