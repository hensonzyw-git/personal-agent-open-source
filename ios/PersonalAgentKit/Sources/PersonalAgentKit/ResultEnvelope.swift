import Foundation

/// Server-rendered facts. No arithmetic or inference of write success on iOS.
public struct ResultEnvelope: Decodable, Sendable, Equatable {
    public let version: Int
    public let kind: String
    public let taskStatus: String
    public let text: String
    public let coverage: String?
    public let analysisNodes: [AnalysisNode]?
    public let commentary: String?
    public let evidence: [Evidence]

    public struct Source: Decodable, Sendable, Equatable {
        public let title: String
        public let url: String
        public let contentPresent: Bool?
        enum CodingKeys: String, CodingKey {
            case title, url
            case contentPresent = "content_present"
        }
        public var publicURL: URL? {
            guard let u = URL(string: url), ["https", "http"].contains(u.scheme), u.user == nil, u.password == nil else { return nil }
            return u
        }
    }
    public struct AnalysisNode: Decodable, Sendable, Equatable {
        public let kind: String
        public let text: String
        public let sources: [Source]?
        public let differenceDecimal: String?
        public let current: Metric?
        public let baseline: Metric?
        enum CodingKeys: String, CodingKey {
            case kind, text, sources, current, baseline
            case differenceDecimal = "difference_decimal"
        }
    }
    public struct Metric: Decodable, Sendable, Equatable {
        public let valueDecimal: String
        public let unit: String
        enum CodingKeys: String, CodingKey {
            case unit
            case valueDecimal = "value_decimal"
        }
    }
    public struct Evidence: Decodable, Sendable, Equatable {
        public let kind: String
        public let title: String?
        public let url: String?
        public let queryResult: FinanceQueryResult?
        public let calendarQueryResult: CalendarQueryResult?
        public let tool: String?
        enum CodingKeys: String, CodingKey { case kind, title, url, tool; case queryResult = "query_result" }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            kind = try c.decode(String.self, forKey: .kind)
            title = try c.decodeIfPresent(String.self, forKey: .title)
            url = try c.decodeIfPresent(String.self, forKey: .url)
            tool = try c.decodeIfPresent(String.self, forKey: .tool)
            if tool?.hasPrefix("calendar.") == true {
                calendarQueryResult = try c.decodeIfPresent(CalendarQueryResult.self, forKey: .queryResult)
                queryResult = nil
            } else {
                queryResult = try c.decodeIfPresent(FinanceQueryResult.self, forKey: .queryResult)
                calendarQueryResult = nil
            }
        }
    }
    enum CodingKeys: String, CodingKey {
        case version, kind, text, coverage, commentary, evidence
        case taskStatus = "task_status"
        case analysisNodes = "analysis_nodes"
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(Int.self, forKey: .version)
        kind = try c.decode(String.self, forKey: .kind)
        taskStatus = try c.decode(String.self, forKey: .taskStatus)
        text = try c.decode(String.self, forKey: .text)
        coverage = try c.decodeIfPresent(String.self, forKey: .coverage)
        commentary = try c.decodeIfPresent(String.self, forKey: .commentary)
        analysisNodes = try c.decodeIfPresent([AnalysisNode].self, forKey: .analysisNodes)
        evidence = try c.decode([Evidence].self, forKey: .evidence)
        guard version == 2, ["conversation","query","analysis","action","clarification","limitation"].contains(kind),
              (analysisNodes?.count ?? 0) <= 16 else {
            throw DecodingError.dataCorruptedError(forKey: .version, in: c, debugDescription: "Unsupported result envelope")
        }
    }
}
