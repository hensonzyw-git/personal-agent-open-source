import Foundation

extension FinanceQueryResult {
    public struct TripBucket: Sendable, Equatable, Decodable {
        public let tripTag: String?
        public let amount: String
        public let recordCount: Int
        private enum CodingKeys: String, CodingKey {
            case tripTag = "trip_tag", amount = "personal_spend_total_cny", recordCount = "record_count"
        }
    }

    public struct TripCoverage: Sendable, Equatable, Decodable {
        public let scanComplete: Bool
        public let scopeCoverage: String
        public let assignmentComplete: Bool
        public let sourceYears: [Int]
        public let unassignedRecordCount: Int
        public var isLimited: Bool { scopeCoverage != "complete" }
        private enum CodingKeys: String, CodingKey {
            case scanComplete = "scan_complete", scopeCoverage = "scope_coverage"
            case assignmentComplete = "assignment_complete", sourceYears = "source_years"
            case unassignedRecordCount = "unassigned_record_count"
        }
    }

    func validateTripObject(_ raw: [String: JSONValue]) throws {
        guard view == .byTrip || filtersApplied["trip_tag"]?.stringValue != nil else { return }
        func invalid() -> DecodingError {
            .dataCorrupted(.init(codingPath: [], debugDescription: "invalid trip wire shape"))
        }
        var allowed: Set<String> = ["status", "view", "metric", "record_count", "filters_applied", "source_system", "evidence", "coverage"]
        allowed.formUnion(view == .records ? ["records", "next_cursor"] : ["personal_spend_total_cny"])
        if view == .byTrip { allowed.insert("by_trip") }
        guard Set(raw.keys).isSubset(of: allowed), raw["status"]?.stringValue == "ok",
              raw["metric"]?.stringValue == "personal_spend_total_cny", sourceSystem == "feishu_bitable",
              let evidence = raw["evidence"]?.objectValue,
              evidence["parser_version"]?.stringValue == "trip-query-v1",
              evidence["result_checksum"]?.stringValue?.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              evidence["matched_count"] == .number(Double(recordCount)),
              let cov = raw["coverage"]?.objectValue,
              Set(cov.keys) == Set(["scan_complete", "scope_coverage", "assignment_complete", "source_years", "unassigned_record_count"])
        else { throw invalid() }
        if filtersApplied["date_range"]?.objectValue == nil, coverage?.scopeCoverage != "unknown" { throw invalid() }
        if let buckets = raw["by_trip"]?.arrayValue {
            for bucket in buckets {
                guard let object = bucket.objectValue, Set(object.keys) == Set(["trip_tag", "personal_spend_total_cny", "record_count"]) else { throw invalid() }
            }
        }
    }

    func validateTrip() throws {
        guard view == .byTrip || filtersApplied["trip_tag"]?.stringValue != nil else { return }
        func invalid() -> DecodingError {
            .dataCorrupted(.init(codingPath: [], debugDescription: "invalid trip query result"))
        }
        guard let coverage, coverage.scanComplete,
              ["complete", "limited", "unknown"].contains(coverage.scopeCoverage),
              !coverage.sourceYears.isEmpty,
              Set(coverage.sourceYears).count == coverage.sourceYears.count,
              coverage.sourceYears.allSatisfy({ (2000...2100).contains($0) }),
              coverage.unassignedRecordCount >= 0,
              coverage.assignmentComplete == (coverage.unassignedRecordCount == 0),
              recordCount >= 0 else { throw invalid() }
        func decimal(_ text: String?) -> Decimal? {
            guard let text, text.range(of: #"^-?(0|[1-9][0-9]*)\.[0-9]{2}$"#, options: .regularExpression) != nil else { return nil }
            return Decimal(string: text, locale: Locale(identifier: "en_US_POSIX"))
        }
        guard view == .records || decimal(amount) != nil else { throw invalid() }
        guard view == .byTrip else { return }
        guard byTrip.count <= 1000, Set(byTrip.map(\.tripTag)).count == byTrip.count else { throw invalid() }
        var sum = Decimal(0)
        var count = 0
        for bucket in byTrip {
            guard bucket.recordCount > 0, let value = decimal(bucket.amount) else { throw invalid() }
            if let tag = bucket.tripTag {
                guard !tag.isEmpty, tag.count <= 256, !tag.contains("#"), tag == tag.trimmingCharacters(in: .whitespacesAndNewlines), tag.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) }) else { throw invalid() }
            }
            if let exact = filtersApplied["trip_tag"]?.stringValue, bucket.tripTag != exact { throw invalid() }
            let (newCount, overflow) = count.addingReportingOverflow(bucket.recordCount)
            guard !overflow else { throw invalid() }
            sum += value
            count = newCount
        }
        guard sum == decimal(amount), count == recordCount else { throw invalid() }
        if filtersApplied["trip_tag"]?.stringValue == nil {
            guard byTrip.filter({ $0.tripTag == nil }).reduce(0, { $0 + $1.recordCount }) == coverage.unassignedRecordCount else { throw invalid() }
        }
    }
}
