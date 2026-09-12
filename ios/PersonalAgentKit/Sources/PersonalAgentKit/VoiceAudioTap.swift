import AVFAudio
import Speech

/// The audio framework calls this on its own queue, never the UI executor.
@available(macOS 26.0, iOS 26.0, *)
public enum VoiceAudioTap {
    public nonisolated static func make(
        continuation: AsyncStream<AnalyzerInput>.Continuation,
        converter: VoiceAudioConverter,
        onFailure: @escaping @Sendable () -> Void = {}
    ) -> @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void {
        { buffer, _ in
            // Capture this recording's continuation, not the mutable UI owner.
            // A late callback after finish is discarded by the finished stream.
            do {
                let audio = try converter.convert(buffer)
                if audio.frameLength > 0 {
                    continuation.yield(AnalyzerInput(buffer: audio))
                }
            } catch {
                continuation.finish()
                onFailure()
            }
        }
    }
}
