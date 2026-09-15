import Foundation
import Testing
@testable import PersonalAgentKit

@Suite("Trip query shared wire contract")
struct TripQueryTests {
    func fixture() throws -> Data {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../tests/fixtures/trip_query.synthetic.json").standardizedFileURL
        return try Data(contentsOf: url)
    }
    @Test func syntheticSummary() throws {
        let result = try JSONDecoder().decode(FinanceQueryResult.self, from: fixture())
        #expect(result.view == .byTrip)
        #expect(result.byTrip.map(\.tripTag) == ["东京02", "东京01", nil])
        #expect(result.amount == "285.00")
        #expect(result.coverage?.scopeCoverage == "complete")
        #expect(result.coverage?.assignmentComplete == false)
    }
    @Test(arguments: ["total", "count", "duplicate", "coverage", "unknown"])
    func refusesCorruption(_ mutation: String) throws {
        var object = try #require(JSONSerialization.jsonObject(with: fixture()) as? [String: Any])
        if mutation == "unknown" { object["secret"] = "untrusted" }
        if mutation == "total" { object["personal_spend_total_cny"] = "99.00" }
        if mutation == "count" { object["record_count"] = 99 }
        if mutation == "duplicate" {
            var buckets = try #require(object["by_trip"] as? [[String: Any]])
            buckets.append(buckets[0]); object["by_trip"] = buckets
        }
        if mutation == "coverage" {
            var coverage = try #require(object["coverage"] as? [String: Any])
            coverage["scan_complete"] = false; object["coverage"] = coverage
        }
        let data = try JSONSerialization.data(withJSONObject: object)
        #expect(throws: DecodingError.self) { try JSONDecoder().decode(FinanceQueryResult.self, from: data) }
    }
}
