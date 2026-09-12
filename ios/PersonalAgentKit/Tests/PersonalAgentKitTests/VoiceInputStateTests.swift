import Testing

@testable import PersonalAgentKit

@Suite("On-device voice input state")
struct VoiceInputStateTests {
    @Test("Apple's sole preferred alternative is accepted as final text")
    func solePreferredAlternative() {
        #expect(VoiceInputStateMachine.soleFinalCandidate([" 今天测试语音。 "]) == "今天测试语音。")
    }

    @Test("empty or multiple final candidates are still refused",
          arguments: [[], [""], [" \n "], ["候选一", "候选二"]] as [[String]])
    func refusesAmbiguousOrEmpty(_ alternatives: [String]) {
        #expect(VoiceInputStateMachine.soleFinalCandidate(alternatives) == nil)
    }

    @Test("cancel or timeout during preparation cannot start a late recording")
    func preparationInvalidation() {
        for cancel in [true, false] {
            var state = VoiceInputStateMachine()
            let old = state.requestPermission()!
            _ = state.permissionResult(true, generation: old)
            if cancel { state.cancel() }
            else { _ = state.fail(.timedOut, generation: old) }
            let beforeNext = state.beganRecording(generation: old)
            #expect(!beforeNext)
            let next = state.requestPermission()!
            _ = state.permissionResult(true, generation: next)
            let stale = state.beganRecording(generation: old)
            let current = state.beganRecording(generation: next)
            #expect(!stale)
            #expect(current)
        }
    }

    @Test("a finalized prefix plus volatile tail never becomes draft")
    func partialTailRefused() {
        var state = VoiceInputStateMachine()
        let token = state.requestPermission()!
        _ = state.permissionResult(true, generation: token)
        _ = state.beganRecording(generation: token)
        _ = state.beginFinalizing(generation: token)
        let result = state.acceptFinalText("final prefix", generation: token, hasVolatileTail: true)
        #expect(result == nil)
        #expect(state.phase == .empty)
    }

    @Test("permission refusal is recorded as denied")
    func deniedPermission() {
        var state = VoiceInputStateMachine()
        let token = state.requestPermission()!
        _ = state.permissionResult(false, generation: token)
        #expect(state.phase == .denied)
    }
    @Test("a cancelled recording rejects its late transcript")
    func lateTranscriptIsIgnored() {
        var state = VoiceInputStateMachine()
        let old = state.requestPermission()!
        let permissionAccepted = state.permissionResult(true, generation: old)
        #expect(permissionAccepted)
        let recordingBegan = state.beganRecording(generation: old)
        #expect(recordingBegan)
        state.cancel()
        let staleFinalization = state.beginFinalizing(generation: old)
        #expect(!staleFinalization)
        let staleText = state.acceptFinalText("late words", generation: old)
        #expect(staleText == nil)
        #expect(state.phase == .idle)
    }

    @Test("empty final speech is never editable draft text")
    func emptySpeechIsTerminal() {
        var state = VoiceInputStateMachine()
        let generation = state.requestPermission()!
        let permissionAccepted = state.permissionResult(true, generation: generation)
        #expect(permissionAccepted)
        let recordingBegan = state.beganRecording(generation: generation)
        #expect(recordingBegan)
        let finalizationBegan = state.beginFinalizing(generation: generation)
        #expect(finalizationBegan)
        let finalText = state.acceptFinalText("  ", generation: generation)
        #expect(finalText == nil)
        #expect(state.phase == .empty)
    }
}
