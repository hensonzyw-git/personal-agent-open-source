import AVFAudio
import Observation
import Speech

/// Local-only, hold-to-talk transcription. Audio buffers are passed directly to
/// Apple's on-device SpeechAnalyzer and are never added to the chat request or
/// written to disk. The composed text remains a normal, user-editable draft.
@MainActor
@Observable
final class VoiceInput {
    var isRecording = false
    var transcript = ""
    var errorMessage: String?

    private let engine = AVAudioEngine()
    private var continuation: AsyncStream<AnalyzerInput>.Continuation?
    private var analyzer: SpeechAnalyzer?
    private var resultTask: Task<Void, Never>?
    private var analysisTask: Task<Void, Never>?

    func start() async {
        guard !isRecording else { return }
        errorMessage = nil
        transcript = ""
        guard await microphoneAllowed() else {
            errorMessage = "未获得麦克风权限，无法开始语音输入。"
            return
        }
        do {
            let transcriber = SpeechTranscriber(locale: .current, preset: .progressiveTranscription)
            let modules: [any SpeechModule] = [transcriber]
            // Installation is explicit user-initiated (the hold gesture) and
            // only concerns Apple's local language asset. We never fall back to
            // cloud recognition when it is unsupported or unavailable.
            if await AssetInventory.status(forModules: modules) != .installed,
               let request = try await AssetInventory.assetInstallationRequest(supporting: modules) {
                try await request.downloadAndInstall()
            }

            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement)
            try session.setActive(true)
            let input = engine.inputNode
            let format = input.outputFormat(forBus: 0)
            let analyzer = SpeechAnalyzer(modules: modules)
            try await analyzer.prepareToAnalyze(in: format)
            let pair = AsyncStream<AnalyzerInput>.makeStream()
            continuation = pair.continuation
            self.analyzer = analyzer
            input.installTap(onBus: 0, bufferSize: 1_024, format: format) { [weak self] buffer, _ in
                self?.continuation?.yield(AnalyzerInput(buffer: buffer))
            }
            try engine.start()
            isRecording = true
            analysisTask = Task { [weak self] in
                do {
                    try await analyzer.start(inputSequence: pair.stream)
                } catch is CancellationError {
                    return
                } catch {
                    await self?.fail("本机语音识别启动失败，请改为手动输入。")
                }
            }
            resultTask = Task { [weak self] in
                do {
                    for try await result in transcriber.results {
                        await self?.accept(String(result.text.characters))
                    }
                } catch is CancellationError {
                    return
                } catch {
                    await self?.fail("语音转写中断，已保留当前草稿。")
                }
            }
        } catch {
            cancel()
            errorMessage = "本机语音包不可用，请联网下载系统语言包后重试，或手动输入。"
        }
    }

    /// End input and wait for the analyzer's final result. A release is not an
    /// authorization to send partial speech: the caller receives only the final,
    /// still-editable transcript.
    func finish() async -> String {
        guard isRecording || analyzer != nil else { return "" }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        continuation?.finish()
        continuation = nil
        if let analyzer {
            try? await analyzer.finalizeAndFinishThroughEndOfInput()
        }
        await analysisTask?.value
        await resultTask?.value
        analyzer = nil
        analysisTask = nil
        resultTask = nil
        isRecording = false
        return consumeTranscript()
    }

    func cancel() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        continuation?.finish()
        continuation = nil
        analysisTask?.cancel()
        resultTask?.cancel()
        analyzer = nil
        analysisTask = nil
        resultTask = nil
        isRecording = false
        transcript = ""
    }

    func consumeTranscript() -> String {
        defer { transcript = "" }
        return transcript.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func accept(_ text: String) {
        transcript = text
    }

    private func fail(_ message: String) {
        guard isRecording else { return }
        errorMessage = message
        cancel()
    }

    private func microphoneAllowed() async -> Bool {
        await withCheckedContinuation { continuation in
            AVAudioSession.sharedInstance().requestRecordPermission { allowed in
                continuation.resume(returning: allowed)
            }
        }
    }
}
