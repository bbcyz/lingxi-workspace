"""Optional activity modules, discovered by their public manifests."""
import sys

from features.checkin import flash
from features.monthly_coupon import auth
from workspace import feature_runtime, http_transport
from workspace.accounts import credentials, login, storage

# Format-1 activity ZIPs import these host libraries by their original names.
# Bind the same objects so plugins share exceptions, credentials and feature gates.
for _name, _module in {
    'checkin_flash': flash, 'monthly_coupon_auth': auth,
    'feature_runtime': feature_runtime, 'http_transport': http_transport,
    'credential_cache': credentials, 'app_login': login, 'login_storage': storage,
}.items():
    sys.modules.setdefault(_name, _module)
