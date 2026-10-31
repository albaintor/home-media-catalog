"""Version the mobile data model independently of the REST route and server release.

Contract 1 describes the DTOs shipped by Home Media Catalog 1.0. Additive API
changes do not require a new contract. A breaking change must increment the
contract, keep /api/v1/status and /api/v1/discovery readable by older clients,
and increase MIN_MOBILE_CONTRACT only when compatibility has actually ended.

For a migration window, support several contracts (min <= max); otherwise
force the app to show an update-required screen before reading the catalogue.
"""

# Public schema/DTO revision, independent of the backwards-compatibility
# contract negotiated by iOS. Increment on every data-model change; the
# server's version identifier includes the model revision automatically.
DATA_MODEL_VERSION = 7

# Contract 2 requires the bounded rich-detail batch endpoint used by iOS.
# Keep contract 1 available for older installed app versions.
MIN_MOBILE_CONTRACT = 1
MAX_MOBILE_CONTRACT = 2


def mobile_compatibility() -> dict[str, int]:
    if MIN_MOBILE_CONTRACT < 1 or MAX_MOBILE_CONTRACT < MIN_MOBILE_CONTRACT:
        raise ValueError("Invalid server/mobile API contract range")
    return {"min_contract": MIN_MOBILE_CONTRACT, "max_contract": MAX_MOBILE_CONTRACT}
