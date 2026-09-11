import CryptoKit
import PersonalAgentKit
import UIKit

/// The client-side half of FR-PHOTO-03.
///
/// Rendering into a fresh bitmap corrects orientation and strips EXIF/XMP: the
/// resulting JPEG is produced from pixels, not copied from the photo-library
/// resource.  The last `UIImage(data:)` is deliberate read-back decode
/// validation for option 1 -- the server only performs a bounded header probe,
/// so the producer must prove that the bytes it just encoded are decodable.
struct PreparedPhoto: Sendable {
    let data: Data
    let width: Int
    let height: Int
    let sha256: String
}

/// The only place original image bytes survive a process death. The file is
/// app-private, excluded from backups, and protected while the device is locked.
enum PhotoStaging {
    static func write(_ photo: PreparedPhoto) throws -> URL {
        let root = try directory()
        let url = root.appendingPathComponent(UUID().uuidString).appendingPathExtension("jpg")
        try photo.data.write(to: url, options: [.atomic, .completeFileProtection])
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var mutableURL = url
        try mutableURL.setResourceValues(values)
        return url
    }

    static func read(_ path: String) throws -> Data {
        try Data(contentsOf: URL(fileURLWithPath: path), options: .mappedIfSafe)
    }

    static func remove(_ path: String) {
        try? FileManager.default.removeItem(at: URL(fileURLWithPath: path))
    }

    private static func directory() throws -> URL {
        let root = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appendingPathComponent("MediaDrafts", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var mutableRoot = root
        try mutableRoot.setResourceValues(values)
        return root
    }
}

enum PhotoPreparationError: Error {
    case unreadable
    case invalidLimits
    case cannotFit
    case roundTripFailed
}

enum PhotoPreparation {
    static func prepare(
        _ source: Data,
        capability: Capabilities.ImageInputCapability
    ) throws -> PreparedPhoto {
        guard capability.enabled,
              let maxBytes = capability.maxContentBytes,
              let maxDimension = capability.maxDimension,
              maxBytes > 0, maxDimension > 0,
              capability.allowedMIMEs.contains("image/jpeg"),
              let image = UIImage(data: source), image.size.width > 0, image.size.height > 0
        else { throw PhotoPreparationError.invalidLimits }

        let longest = max(image.size.width, image.size.height)
        let initialScale = min(1, CGFloat(maxDimension) / longest)
        // Ten rounds have a deterministic lower bound and never silently send
        // a large original merely because a particular JPEG was difficult to
        // compress.  We decrease both dimensions and quality, never crop.
        for step in 0..<10 {
            let scale = initialScale * pow(0.82, CGFloat(step))
            let target = CGSize(
                width: max(1, floor(image.size.width * scale)),
                height: max(1, floor(image.size.height * scale))
            )
            let format = UIGraphicsImageRendererFormat()
            format.scale = 1
            let renderer = UIGraphicsImageRenderer(size: target, format: format)
            let flattened = renderer.image { _ in image.draw(in: CGRect(origin: .zero, size: target)) }
            let quality = max(0.45, 0.88 - CGFloat(step) * 0.05)
            guard let encoded = flattened.jpegData(compressionQuality: quality) else { continue }
            guard encoded.count <= maxBytes else { continue }
            guard let decoded = UIImage(data: encoded),
                  Int(decoded.size.width.rounded()) == Int(target.width),
                  Int(decoded.size.height.rounded()) == Int(target.height)
            else { throw PhotoPreparationError.roundTripFailed }
            return PreparedPhoto(
                data: encoded,
                width: Int(target.width),
                height: Int(target.height),
                sha256: SHA256.hash(data: encoded).map { String(format: "%02x", $0) }.joined()
            )
        }
        throw PhotoPreparationError.cannotFit
    }
}
