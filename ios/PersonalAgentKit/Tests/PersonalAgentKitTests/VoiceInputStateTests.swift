import Testing

@testable import PersonalAgentKit

@Suite("On-device voice input state")
struct VoiceInputStateTests {
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
