import SwiftUI
import UIKit

struct CameraInput: UIViewControllerRepresentable {
    let finished: (Data?) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(finished: finished) }
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.cameraCaptureMode = .photo
        picker.delegate = context.coordinator
        return picker
    }
    func updateUIViewController(_ controller: UIImagePickerController, context: Context) {}

    final class Coordinator: NSObject, UINavigationControllerDelegate, UIImagePickerControllerDelegate {
        let finished: (Data?) -> Void
        init(finished: @escaping (Data?) -> Void) { self.finished = finished }
        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { finished(nil) }
        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            finished((info[.originalImage] as? UIImage)?.jpegData(compressionQuality: 1))
        }
    }
}

struct TimelinePhoto: View {
    let mediaID: String
    let model: ChatModel
    @State private var photo: UIImage?
    @State private var unavailable = false

    var body: some View {
        Group {
            if let photo {
                Image(uiImage: photo).resizable().scaledToFit()
                    .frame(maxWidth: 240, maxHeight: 240)
                    .accessibilityLabel("消息中的图片")
            } else if unavailable {
                Label("图片已删除或暂不可用", systemImage: "photo.badge.exclamationmark")
            } else {
                ProgressView("读取图片")
            }
        }
        .task(id: mediaID) {
            photo = nil
            unavailable = false
            do {
                let data = try await model.readPhoto(mediaID)
                guard !Task.isCancelled else { return }
                photo = UIImage(data: data)
                unavailable = photo == nil
            } catch {
                if !Task.isCancelled { unavailable = true }
            }
        }
        .onDisappear { photo = nil }
    }
}
