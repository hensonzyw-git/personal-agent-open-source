import AVFAudio
import Speech

/// The audio framework calls this on its own queue, never the UI executor.
@available(macOS 26.0, iOS 26.0, *)
public enum VoiceAudioTap {
    public nonisolated static func make(
        continuation: AsyncStream<AnalyzerInput>.Continuation
    ) -> @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void {
        { buffer, _ in
            // Capture this recording's continuation, not the mutable UI owner.
            // A late callback after finish is discarded by the finished stream.
            continuation.yield(AnalyzerInput(buffer: buffer))
        }
    }
}
