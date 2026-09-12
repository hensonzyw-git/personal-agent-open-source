import AVFAudio
import Speech
import Foundation

/// The audio framework calls this on its own queue, never the UI executor.
@available(macOS 26.0, iOS 26.0, *)
public enum VoiceAudioTap {
    /// Serializes buffer publication with drain/termination, so finish cannot
    /// overtake the yield of a callback that already converted its buffer.
    public final class Session: @unchecked Sendable {
        private let lock = NSLock()
        private let continuation: AsyncStream<AnalyzerInput>.Continuation
        private let converter: VoiceAudioConverter
        private var finished = false
        public init(continuation: AsyncStream<AnalyzerInput>.Continuation, converter: VoiceAudioConverter) {
            self.continuation = continuation
            self.converter = converter
        }
        fileprivate func receive(_ buffer: AVAudioPCMBuffer) throws {
            lock.lock()
            defer { lock.unlock() }
            guard !finished else { return }
            let audio = try converter.convert(buffer)
            if audio.frameLength > 0 { continuation.yield(AnalyzerInput(buffer: audio)) }
        }
        public func finish() throws {
            lock.lock()
            defer { lock.unlock() }
            guard !finished else { return }
            finished = true
            defer { continuation.finish() }
            for audio in try converter.finish() { continuation.yield(AnalyzerInput(buffer: audio)) }
        }
        public func cancel() {
            lock.lock()
            defer { lock.unlock() }
            finished = true
            continuation.finish()
        }
    }

    public nonisolated static func make(
        continuation: AsyncStream<AnalyzerInput>.Continuation,
        converter: VoiceAudioConverter,
        onFailure: @escaping @Sendable () -> Void = {}
    ) -> @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void {
        make(session: Session(continuation: continuation, converter: converter), onFailure: onFailure)
    }

    public nonisolated static func make(
        session: Session,
        onFailure: @escaping @Sendable () -> Void = {}
    ) -> @Sendable (AVAudioPCMBuffer, AVAudioTime) -> Void {
        { buffer, _ in
            // Capture this recording's continuation, not the mutable UI owner.
            // A late callback after finish is discarded by the finished stream.
            do {
                try session.receive(buffer)
            } catch {
                session.cancel()
                onFailure()
            }
        }
    }
}
