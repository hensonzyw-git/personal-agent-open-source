import Foundation

/// What this build can implement, declared on every request it makes.
///
/// The server gates calendar issuance and delivery on this header (design
/// §2.5, R1-F1): `calendar.create_event` requires wire v2, and a client that
/// cannot implement the action it is handed must never be handed one. The
/// gate is what stops an older build — which would ignore `calendar_identifier`
/// and fall back to its own default calendar — from writing an event into the
/// wrong calendar, so **this header is a precondition for calendar issuance,
/// not a nicety**: without it the server reads the caller as v1 and refuses to
/// issue.
///
/// Version 3 adds the mirror's per-window sync epoch to the v2 fields:
///
/// - the plural `device_actions` list (a single action is a list of one), never
///   the singular historical field;
/// - the calendar identity, timezone and all-day date fields an action carries.
///
/// It is sent on **every** request the client makes, not on a list of
/// endpoints the caller has to keep in step with the server. Which requests the
/// server chooses to gate on is the server's business; a client that declared
/// its version only where it expected to be asked would be wrong the first time
/// a new gated call shipped. The value is a claim about this build, and it is
/// true of every request this build sends.
///
/// `DeviceWireContract` is the *other* wire contract — the auth signature the
/// two sides exchange — and it is deliberately kept apart from this one: they
/// are versioned, reviewed and broken independently.
public enum ClientWireVersion {
    /// The IR's `CLIENT_WIRE_VERSION_HEADER`. Pinned literally here because the
    /// constant lives in Python; the server reads the same name.
    public static let header = "X-Client-Wire-Version"
    /// The version this build implements, and therefore declares.
    public static let version = 4
    /// What travels on the wire. A string, because that is what a header is.
    public static let value = String(version)
}
