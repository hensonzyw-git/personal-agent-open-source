import Foundation

/// The local-ASR lifecycle. It deliberately has more terminal states than a
/// boolean recording flag: an interrupted or empty recording must not be made
/// to look like editable user text, and a late callback must never overwrite a
/// newer draft.
public enum VoiceInputPhase: Sendable, Equatable {
    case idle, permission, preparing, recording, finalizing, editable
    case denied, unsupported, empty, ambiguous, lowConfidence, interrupted, timedOut
}

/// A UI-agnostic state machine for on-device transcription. `generation` is
/// minted for each hold gesture; result callbacks must present it back, which
/// makes a callback from a cancelled recording harmless by construction.
public struct VoiceInputStateMachine: Sendable {
    public private(set) var phase: VoiceInputPhase = .idle
    public private(set) var generation = 0

    public init() {}

    @discardableResult
    public mutating func requestPermission() -> Int? {
        guard phase == .idle || isTerminal else { return nil }
        generation += 1
        phase = .permission
        return generation
    }

    public mutating func permissionResult(_ granted: Bool, generation: Int) -> Bool {
        guard matches(generation), phase == .permission else { return false }
        phase = granted ? .preparing : .denied
        return granted
    }

    public mutating func beganRecording(generation: Int) -> Bool {
        guard matches(generation), phase == .preparing else { return false }
        phase = .recording
        return true
    }

    public mutating func beginFinalizing(generation: Int) -> Bool {
        guard matches(generation), phase == .recording else { return false }
        phase = .finalizing
        return true
    }

    public mutating func acceptFinalText(
        _ text: String, generation: Int, hasVolatileTail: Bool = false
    ) -> String? {
        guard matches(generation), phase == .finalizing else { return nil }
        let trimmed = hasVolatileTail ? "" : text.trimmingCharacters(in: .whitespacesAndNewlines)
        phase = trimmed.isEmpty ? .empty : .editable
        return trimmed.isEmpty ? nil : trimmed
    }

    public mutating func fail(_ terminal: VoiceInputPhase, generation: Int) -> Bool {
        guard matches(generation), terminal != .idle, terminal != .permission,
              terminal != .preparing, terminal != .recording, terminal != .finalizing,
              terminal != .editable
        else { return false }
        phase = terminal
        return true
    }

    public mutating func cancel() {
        generation += 1
        phase = .idle
    }

    public mutating func resetEditable() {
        guard phase == .editable else { return }
        phase = .idle
    }

    private var isTerminal: Bool {
        switch phase {
        case .denied, .unsupported, .empty, .ambiguous, .lowConfidence, .interrupted, .timedOut:
            return true
        default:
            return false
        }
    }

    private func matches(_ candidate: Int) -> Bool { generation == candidate }
}
