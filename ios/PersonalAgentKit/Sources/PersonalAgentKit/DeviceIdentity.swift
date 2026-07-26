import CryptoKit
import Foundation

/// The device's signing identity: a P-256 key whose private half never leaves
/// the hardware.
///
/// It is a protocol for one reason only -- the tests need a key they can create
/// without a Secure Enclave. It is deliberately *not* a seam for choosing a
/// weaker key at runtime: the software implementation below is compiled out
/// entirely on a real device.
public protocol DeviceIdentity: Sendable {
    /// The enrolled public key: ANSI X9.63 uncompressed point, `0x04 || X || Y`.
    var publicKeyX963: Data { get }
    /// A JOSE ES256 signature: 64 bytes of `R || S`.
    func signature(for message: Data) throws -> Data
}

extension DeviceIdentity {
    public var publicKeyB64U: String {
        DeviceWireContract.base64URLEncode(publicKeyX963)
    }

    public var thumbprint: String? {
        try? DeviceWireContract.thumbprint(publicKeyX963: publicKeyX963)
    }
}

public enum DeviceIdentityError: Error {
    /// The Secure Enclave is unavailable and no fallback exists on this build.
    case secureEnclaveUnavailable
    case keyCreationFailed(String)
    case signingFailed(String)
    /// A stored key blob could not be reopened; the device must re-enroll.
    case storedKeyUnusable
}

/// A Secure Enclave key, per design 4.1 step 2.
///
/// The private key is generated inside the Enclave and is not extractable. What
/// is persisted in the Keychain is CryptoKit's opaque, device-bound blob, stored
/// `WhenUnlockedThisDeviceOnly`, and the key itself is created with
/// `.privateKeyUsage` -- Phase 1 deliberately does not demand biometrics for a
/// routine token refresh.
public struct SecureEnclaveDeviceIdentity: DeviceIdentity {
    private let key: SecureEnclave.P256.Signing.PrivateKey

    public var publicKeyX963: Data { key.publicKey.x963Representation }

    /// The blob to persist. It is not key material: it cannot be used on any
    /// other device, and it is still stored in the Keychain rather than a file.
    public var persistentRepresentation: Data { key.dataRepresentation }

    public static var isAvailable: Bool { SecureEnclave.isAvailable }

    public init() throws {
        guard SecureEnclave.isAvailable else {
            throw DeviceIdentityError.secureEnclaveUnavailable
        }
        var accessError: Unmanaged<CFError>?
        guard
            let access = SecAccessControlCreateWithFlags(
                nil,
                kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                .privateKeyUsage,
                &accessError
            )
        else {
            throw DeviceIdentityError.keyCreationFailed(
                "SecAccessControlCreateWithFlags failed"
            )
        }
        do {
            key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        } catch {
            throw DeviceIdentityError.keyCreationFailed(String(describing: error))
        }
    }

    public init(persistentRepresentation: Data) throws {
        guard SecureEnclave.isAvailable else {
            throw DeviceIdentityError.secureEnclaveUnavailable
        }
        do {
            key = try SecureEnclave.P256.Signing.PrivateKey(
                dataRepresentation: persistentRepresentation
            )
        } catch {
            throw DeviceIdentityError.storedKeyUnusable
        }
    }

    public func signature(for message: Data) throws -> Data {
        do {
            // CryptoKit hands back `R || S` directly, which is what the wire
            // wants; the DER conversion in the contract exists for the frozen
            // vectors and for Security.framework callers.
            return try key.signature(for: message).rawRepresentation
        } catch {
            throw DeviceIdentityError.signingFailed(String(describing: error))
        }
    }
}

#if targetEnvironment(simulator) || os(macOS)
/// A software P-256 key, for the Simulator and for `swift test` only.
///
/// It is inside `#if targetEnvironment(simulator) || os(macOS)` on purpose:
/// design 4.2.1 says a test key must never reach production, and the cheapest way
/// to guarantee that is for the code not to exist in a device build. The app
/// shows which identity is in use, so a software key can never be mistaken for
/// an Enclave one.
public struct SoftwareDeviceIdentity: DeviceIdentity {
    private let key: P256.Signing.PrivateKey

    public var publicKeyX963: Data { key.publicKey.x963Representation }
    public var persistentRepresentation: Data { key.rawRepresentation }

    public init() {
        key = P256.Signing.PrivateKey()
    }

    public init(persistentRepresentation: Data) throws {
        do {
            key = try P256.Signing.PrivateKey(rawRepresentation: persistentRepresentation)
        } catch {
            throw DeviceIdentityError.storedKeyUnusable
        }
    }

    public func signature(for message: Data) throws -> Data {
        do {
            return try key.signature(for: message).rawRepresentation
        } catch {
            throw DeviceIdentityError.signingFailed(String(describing: error))
        }
    }
}
#endif

/// Which kind of key this build produced, so the status screen can say so.
public enum DeviceKeyKind: String, Sendable {
    case secureEnclave = "Secure Enclave"
    case software = "software (simulator only)"
}

/// Creates or reopens the device identity, and reports which kind it is.
///
/// On a real iPhone the software branch does not exist, so an unavailable Enclave
/// is an error rather than a silent downgrade.
public struct DeviceIdentityFactory: Sendable {
    public init() {}

    public func create() throws -> (identity: any DeviceIdentity, kind: DeviceKeyKind, blob: Data) {
        if SecureEnclaveDeviceIdentity.isAvailable {
            let identity = try SecureEnclaveDeviceIdentity()
            return (identity, .secureEnclave, identity.persistentRepresentation)
        }
        #if targetEnvironment(simulator) || os(macOS)
        let identity = SoftwareDeviceIdentity()
        return (identity, .software, identity.persistentRepresentation)
        #else
        throw DeviceIdentityError.secureEnclaveUnavailable
        #endif
    }

    public func reopen(blob: Data, kind: DeviceKeyKind) throws -> any DeviceIdentity {
        switch kind {
        case .secureEnclave:
            return try SecureEnclaveDeviceIdentity(persistentRepresentation: blob)
        case .software:
            #if targetEnvironment(simulator) || os(macOS)
            return try SoftwareDeviceIdentity(persistentRepresentation: blob)
            #else
            // A device build must not reopen a software key even if one somehow
            // ended up in its Keychain.
            throw DeviceIdentityError.secureEnclaveUnavailable
            #endif
        }
    }
}
